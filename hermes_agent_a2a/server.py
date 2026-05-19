"""A2A HTTP server — runs in a background thread, no asyncio.

Handles inbound A2A JSON-RPC requests. Messages are queued and picked up
by the pre_llm_call hook; responses are captured by post_llm_call and
returned to the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Event, Lock
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse
import urllib.request
import urllib.error

from .security import RateLimiter, audit, filter_outbound, sanitize_inbound
from .a2a_spec.tasks import build_error_response
from .a2a_spec.push import (
    CreateTaskPushNotificationConfigRequest,
    CreateTaskPushNotificationConfigResponse,
    GetTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigResponse,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    DeleteTaskPushNotificationConfigRequest,
    DeleteTaskPushNotificationConfigResponse,
    TaskPushNotificationConfig,
    AuthenticationInfo,
)
from .push_delivery import (
    create_push_config,
    get_push_config,
    list_push_configs,
    delete_push_config,
)


def _validate_webhook_host(host: str) -> str:
    """Validate A2A_WEBHOOK_HOST to prevent SSRF in webhook delivery.

    Rejects loopback/private addresses to block abuse via environment variable
    injection. The webhook_url is built from these values and sent with an HMAC
    signature, so redirecting it to an attacker-controlled host would leak the
    signed payload.
    """
    # urlparse requires a scheme; prefix to extract the host component safely.
    parsed = urlparse(f"http://{host}")
    netloc = parsed.netloc.split(":")[0]
    if netloc in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise ValueError(
            f"A2A_WEBHOOK_HOST ({host}) resolves to a loopback address; "
            "refusing to deliver signed webhook to an internal endpoint from this path"
        )
    return host

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
_TASK_CACHE_MAX = 1000
_MAX_PENDING = 10
_RESPONSE_TIMEOUT = int(os.getenv("A2A_RESPONSE_TIMEOUT", "120"))  # seconds to wait for agent response

try:
    from hermes_cli import __version__ as HERMES_VERSION
except Exception:
    HERMES_VERSION = "0.0.0"
    logger.warning("[A2A] Failed to import hermes_cli.__version, using fallback '0.0.0'. hermes_cli may be misinstalled.")


class _PendingTask:
    __slots__ = ("task_id", "text", "metadata", "response", "ready", "created_at", "context_id")

    def __init__(self, task_id: str, text: str, metadata: dict, context_id: Optional[str] = None):
        self.task_id = task_id
        self.text = text
        self.metadata = metadata
        self.response: Optional[str] = None
        self.ready = Event()
        self.created_at = time.time()
        self.context_id = context_id


class TaskQueue:
    """Thread-safe queue for pending A2A tasks with full state machine.

    States:
      submitted     — task received, not yet processing
      working       — actively being processed
      auth_required — waiting for authentication
      authenticated — auth confirmed, waiting to become working
      completed     — task done, response available
      failed        — task failed
      canceled      — task canceled
      rejected      — task rejected (auth or policy)

    Valid transitions:
      submitted       → working
      auth_required   → authenticated, rejected
      authenticated    → working
      working          → completed, failed, canceled
    """

    # State machine definition: from_state → set of allowed to_states
    _TRANSITIONS: dict[str, set[str]] = {
        "submitted":     {"working", "completed", "failed", "canceled"},
        "working":       {"completed", "failed", "canceled"},
        "auth_required": {"authenticated", "rejected"},
        "authenticated": {"working", "failed"},
        "completed":     set(),
        "failed":        set(),
        "canceled":      set(),
        "rejected":      set(),
    }

    def __init__(self):
        self._pending: OrderedDict[str, _PendingTask] = OrderedDict()
        self._completed: OrderedDict[str, _PendingTask] = OrderedDict()
        self._processing: set[str] = set()
        self._lock = Lock()
        # Atomic counters — eliminate re-entrancy risk in _get_queue_depth
        self._enqueue_count = 0
        self._complete_count = 0
        self._cancel_count = 0
        # State machine: task_id → current state
        self._states: dict[str, str] = {}

    def _set_state(self, task_id: str, state: str) -> None:
        """Set task state, creating it on first access."""
        self._states[task_id] = state

    def set_auth_required(self, task_id: str, metadata: dict) -> None:
        """Place a task in auth_required state without queuing it."""
        with self._lock:
            self._states[task_id] = "auth_required"

    def set_authenticated(self, task_id: str, metadata: dict) -> None:
        """Mark a task as authenticated (from auth_required)."""
        with self._lock:
            self._states[task_id] = "authenticated"

    def set_rejected(self, task_id: str, metadata: dict) -> None:
        """Mark a task as rejected."""
        with self._lock:
            self._states[task_id] = "rejected"

    def transition(self, task_id: str, to_state: str, return_error: bool = False) -> bool | tuple[bool, int | None]:
        """Attempt a state transition.

        Args:
            task_id: The task to transition.
            to_state: The target state.
            return_error: If True, return (success, error_code) on failure.

        Returns:
            True if transition succeeded.
            If return_error=True, returns (False, error_code) on failure.
            If return_error=False, returns False on failure.
        """
        with self._lock:
            from_state = self._states.get(task_id, "submitted")
            allowed = self._TRANSITIONS.get(from_state, set())
            if to_state in allowed:
                self._states[task_id] = to_state
                if return_error:
                    return True, None
                return True
            if return_error:
                return False, -38003  # Invalid state transition
            return False

    def pending_count(self) -> int:
        # Counter-based — no re-entrancy risk, no traversal of singleton
        with self._lock:
            return max(0, self._enqueue_count - self._complete_count - self._cancel_count)

    def enqueue(self, task_id: str, text: str, metadata: dict, context_id: Optional[str] = None) -> _PendingTask | None:
        task = _PendingTask(task_id, text, metadata, context_id=context_id)
        with self._lock:
            if len(self._pending) >= _MAX_PENDING:
                return None
            if task_id in self._pending:
                return None
            self._pending[task_id] = task
            self._states[task_id] = "submitted"
            # Only evict tasks that are not currently being processed to avoid race
            evicted = 0
            while len(self._pending) > _TASK_CACHE_MAX:
                for tid, old_task in list(self._pending.items()):
                    if tid not in self._processing:
                        self._pending.pop(tid)
                        old_task.response = "(dropped — queue overflow)"
                        old_task.ready.set()
                        evicted += 1
                        break
                else:
                    # All pending tasks are being processed, stop evicting
                    break
            # Increment counter for successfully enqueued (non-evicted) tasks only
            self._enqueue_count += 1 - evicted
        # Record task received metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_received()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_received): %s", exc)
        return task

    def drain_pending(self, exclude: set[str] | None = None) -> list[_PendingTask]:
        with self._lock:
            skip = set(exclude or ()) | self._processing
            return [t for t in self._pending.values() if t.task_id not in skip]

    def requeue_tasks(self, tasks: list[_PendingTask]) -> None:
        """Re-queue tasks that were drained but not processed."""
        with self._lock:
            for task in tasks:
                if task.task_id not in self._pending and task.task_id not in self._processing:
                    self._pending[task.task_id] = task
                    self._enqueue_count += 1

    def mark_processing(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._pending:
                self._processing.add(task_id)

    def complete(self, task_id: str, response: str) -> None:
        with self._lock:
            self._processing.discard(task_id)
            task = self._pending.pop(task_id, None)
            if task:
                task.response = response
                task.ready.set()
                self._completed[task_id] = task
                self._complete_count += 1
                self._states[task_id] = "completed"
                while len(self._completed) > _TASK_CACHE_MAX:
                    self._completed.popitem(last=False)
        # Record task completed metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_completed()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_completed): %s", exc)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            self._processing.discard(task_id)
            task = self._pending.pop(task_id, None)
            if task:
                task.response = "(canceled)"
                task.ready.set()
                self._completed[task_id] = task
                self._cancel_count += 1
                self._states[task_id] = "canceled"
        # Record task canceled metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_canceled()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_canceled): %s", exc)

    def get_status(self, task_id: str) -> dict:
        with self._lock:
            if task_id in self._pending:
                return {"state": self._states.get(task_id, "working")}
            task = self._completed.get(task_id)
            if task:
                if task.response == "(canceled)":
                    return {"state": "canceled"}
                if task_id in self._states:
                    return {"state": self._states[task_id]}
                return {"state": "completed", "response": filter_outbound(task.response)}
            # Check pre-queue auth/rejected states
            if task_id in self._states:
                return {"state": self._states[task_id]}
        return {"state": "unknown"}

    def get_task_metadata(self, task_id: str) -> dict:
        """Get metadata for a task by ID (public API for hooks)."""
        with self._lock:
            task = self._pending.get(task_id) or self._completed.get(task_id)
            return getattr(task, "metadata", {}) if task else {}

    def get_all_task_metadata(self) -> dict[str, dict]:
        """Get metadata for all pending and completed tasks (public API for hooks)."""
        with self._lock:
            result = {}
            for task in list(self._pending.values()) + list(self._completed.values()):
                result[task.task_id] = getattr(task, "metadata", {})
            return result

    def get_processing_tasks(self) -> list[str]:
        """Get list of currently processing task IDs (public API for hooks)."""
        with self._lock:
            return list(self._processing)

    def find_task_by_id(self, task_id: str) -> Optional[_PendingTask]:
        """Find a task by ID across pending and completed (public API for hooks)."""
        with self._lock:
            task = self._pending.get(task_id)
            if task is not None:
                return task
            return self._completed.get(task_id)


def get_runtime_state_dict() -> dict:
    """Expose the process-wide runtime state to the plugin loader as a dict.

    Named explicitly as _dict to avoid shadowing runtime_state.get_runtime_state()
    which returns the A2ARuntimeState singleton.
    """
    from .runtime_state import get_runtime_state as get_state
    return get_state().to_dict()


def set_runtime_server(server, thread) -> None:
    """Set the runtime server and thread in the singleton state."""
    from .runtime_state import get_runtime_state as get_state
    state = get_state()
    state.set_server(server)
    state.set_thread(thread)
    state.set_owner_module(__name__)


def clear_runtime_server(server) -> None:
    """Clear the runtime server from the singleton state."""
    from .runtime_state import get_runtime_state as get_state
    state = get_state()
    if state.get_server() == server:
        state.set_server(None)
        state.set_thread(None)


# Module-level task queue reference for backward compatibility
def _get_task_queue() -> TaskQueue:
    """Get the task queue from the singleton state."""
    from .runtime_state import get_runtime_state as get_state
    return get_state().get_task_queue()


def _ensure_task_queue() -> TaskQueue:
    """Get the task queue from the singleton state."""
    return _get_task_queue()


def _trigger_webhook(message: str = "", task_id: str = "", mode: str = None, deliver_only: bool = False, retries=None, base_delay=None, on_failure=None, use_direct_a2a: bool = False, target_url: str = "", auth_token: str = ""):
    """Trigger an agent turn via webhook or direct A2A call.

    Args:
        deliver_only: if True, the webhook handler invokes the agent but skips
            Telegram routing (used for peer-originated A2A tasks).
        use_direct_a2a: if True, use direct A2A JSON-RPC call instead of webhook.
        target_url: A2A endpoint URL (required when use_direct_a2a=True).
        auth_token: Bearer token for A2A authentication (optional).
        on_failure: optional callable invoked with (task_id,) if all retries fail.
    """
    # Use direct A2A for modes 1,2,3 (protocol tasks, workers)
    if use_direct_a2a and target_url:
        result = _call_a2a_direct(target_url, message, task_id, auth_token)
        if "error" in result:
            logger.warning("[A2A] Direct A2A call failed: %s", result["error"])
            if on_failure:
                on_failure(task_id)
        return

    # Make retry logic configurable via environment variables
    if retries is None:
        retries = int(os.getenv("A2A_WEBHOOK_RETRIES", "3"))
    if base_delay is None:
        base_delay = float(os.getenv("A2A_WEBHOOK_BACKOFF", "1.0"))

    secret = os.getenv("A2A_WEBHOOK_SECRET", "")
    if not secret:
        if os.getenv("WEBHOOK_SECRET"):
            logger.warning("[A2A] A2A_WEBHOOK_SECRET not set, falling back to WEBHOOK_SECRET. This is not recommended for security.")
            secret = os.getenv("WEBHOOK_SECRET", "")
        if not secret:
            if on_failure:
                on_failure(task_id)
        return

    body_dict = {
        "event_type": "a2a_inbound",
        "text": message,
        "task_id": task_id,
    }
    if mode is not None:
        body_dict["mode"] = mode
    if deliver_only:
        body_dict["deliver_only"] = True
    # SSRF guard: reject loopback hosts before building the webhook URL.
    # Placed after the secret check so a bad env var causes a no-op rather
    # than an unhandled ValueError when no secret is configured.
    webhook_host = os.getenv("A2A_WEBHOOK_HOST", "127.0.0.1")
    try:
        webhook_host = _validate_webhook_host(webhook_host)
    except ValueError:
        logger.warning("[A2A] Webhook SSRF guard rejected A2A_WEBHOOK_HOST=%s", webhook_host)
        if on_failure:
            on_failure(task_id)
        return
    webhook_port = int(os.getenv("WEBHOOK_PORT", "8644"))
    webhook_url = f"http://{webhook_host}:{webhook_port}/webhooks/a2a_trigger"
    body = json.dumps(body_dict, sort_keys=True, ensure_ascii=False).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
        method="POST",
    )
    last_exc = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                logger.debug("[A2A] Webhook trigger: %d", resp.status)
            return
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.debug("[A2A] Webhook trigger failed (attempt %d/%d), retrying in %ds: %s", attempt+1, retries, delay, e)
                time.sleep(delay)
    logger.warning("[A2A] Webhook trigger failed after %d attempts: %s", retries, last_exc)
    if on_failure:
        on_failure(task_id)


def _call_a2a_direct(url: str, message: str, task_id: str, auth_token: str = "", timeout: int = 10) -> dict:
    """Make a direct A2A JSON-RPC call to an agent.

    NOTE: Must use A2A spec format (params.message.role/parts/metadata) via build_task_send_payload.
    The non-spec format (params.task.text) causes "Empty message" errors on recipients.
    Previous revert (f539a9d) was incorrect; spec format is required for compatibility.

    Args:
        url: Target agent's A2A endpoint (e.g., http://127.0.0.1:41808/a2a)
        message: The message to send
        task_id: Unique task identifier
        auth_token: Optional bearer token for authentication
        timeout: HTTP timeout in seconds

    Returns:
        Response dict with 'result' or 'error' key
    """
    from .a2a_spec.tasks import build_task_send_payload
    from_agent = os.getenv("A2A_AGENT_NAME", "hermes-agent")
    payload = build_task_send_payload(
        task_id=task_id,
        message=message,
        sender_name=from_agent,
        intent="consultation",
        expected_action="reply",
    )
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_data = json.loads(resp.read().decode())
            if "result" in response_data:
                return {"result": response_data["result"], "task_id": task_id}
            elif "error" in response_data:
                return {"error": response_data["error"], "task_id": task_id}
            return {"error": "Invalid response", "task_id": task_id}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "task_id": task_id}
    except urllib.error.URLError as e:
        return {"error": f"URL error: {e.reason}", "task_id": task_id}
    except Exception as e:
        return {"error": str(e), "task_id": task_id}


def _urlopen_with_status(req, timeout):
    """Open a URL and return the response object. Used via asyncio.to_thread."""
    return urllib.request.urlopen(req, timeout=timeout)


async def _call_a2a_direct_async(url: str, message: str, task_id: str, auth_token: str = "", timeout: int = 10) -> dict:
    """Async wrapper for _call_a2a_direct — runs blocking I/O in a thread pool."""
    return await asyncio.to_thread(_call_a2a_direct, url, message, task_id, auth_token, timeout)


async def _trigger_webhook_async(message: str = "", task_id: str = "", mode: str = None, deliver_only: bool = False, retries=None, base_delay=None, on_failure=None, use_direct_a2a: bool = False, target_url: str = "", auth_token: str = ""):
    """Async variant of _trigger_webhook — runs blocking I/O in a thread pool.

    Replaces the sync _trigger_webhook when called from an async context to avoid
    blocking the event loop with urllib.request.urlopen calls.
    """
    if use_direct_a2a and target_url:
        result = await _call_a2a_direct_async(target_url, message, task_id, auth_token)
        if "error" in result:
            logger.warning("[A2A] Direct A2A call failed: %s", result["error"])
            if on_failure:
                on_failure(task_id)
        return

    if retries is None:
        retries = int(os.getenv("A2A_WEBHOOK_RETRIES", "3"))
    if base_delay is None:
        base_delay = float(os.getenv("A2A_WEBHOOK_BACKOFF", "1.0"))

    secret = os.getenv("A2A_WEBHOOK_SECRET", "")
    if not secret:
        if os.getenv("WEBHOOK_SECRET"):
            logger.warning("[A2A] A2A_WEBHOOK_SECRET not set, falling back to WEBHOOK_SECRET. This is not recommended for security.")
            secret = os.getenv("WEBHOOK_SECRET", "")
        if not secret:
            if on_failure:
                on_failure(task_id)
            return

    # SSRF guard: reject loopback hosts before building the webhook URL.
    # Placed after the secret check so a bad env var causes a no-op rather
    # than an unhandled ValueError when no secret is configured.
    webhook_host = os.getenv("A2A_WEBHOOK_HOST", "127.0.0.1")
    try:
        webhook_host = _validate_webhook_host(webhook_host)
    except ValueError:
        logger.warning("[A2A] Webhook SSRF guard rejected A2A_WEBHOOK_HOST=%s", webhook_host)
        if on_failure:
            on_failure(task_id)
        return
    webhook_port = int(os.getenv("WEBHOOK_PORT", "8644"))
    webhook_url = f"http://{webhook_host}:{webhook_port}/webhooks/a2a_trigger"
    body_dict = {
        "event_type": "a2a_inbound",
        "text": message,
        "task_id": task_id,
    }
    if mode is not None:
        body_dict["mode"] = mode
    if deliver_only:
        body_dict["deliver_only"] = True
    body = json.dumps(body_dict, sort_keys=True, ensure_ascii=False).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
        method="POST",
    )

    last_exc = None
    for attempt in range(retries):
        try:
            # Run urlopen in a thread so it doesn't block the event loop.
            # We read .status before the context manager exits so we capture
            # the status code after the thread-pool call completes.
            resp = await asyncio.to_thread(_urlopen_with_status, req, 5)
            logger.debug("[A2A] Webhook trigger: %d", resp.status)
            return
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.debug("[A2A] Webhook trigger failed (attempt %d/%d), retrying in %ds: %s", attempt+1, retries, delay, e)
                await asyncio.to_thread(time.sleep, delay)
    logger.warning("[A2A] Webhook trigger failed after %d attempts: %s", retries, last_exc)
    if on_failure:
        on_failure(task_id)


def _start_orphaned_task_watchdog(task_queue: TaskQueue) -> threading.Thread:
    """Periodically mark tasks older than 2 * _RESPONSE_TIMEOUT as failed."""
    def run():
        while True:
            time.sleep(_RESPONSE_TIMEOUT)
            cutoff = time.time() - 2 * _RESPONSE_TIMEOUT
            for task in list(task_queue._pending.values()):
                if task.created_at < cutoff and task.response is None:
                    task.response = "(orphaned — no response)"
                    task.ready.set()
                    logger.warning("[A2A] Task %s orphaned — marked failed after %.0fs", task.task_id, time.time() - task.created_at)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# -------------------------------------------------------------------------
# ListTasks helpers (shared between REST and JSON-RPC)
# -------------------------------------------------------------------------

def _build_task_list_item(task: "_PendingTask", state: str) -> dict:
    """Build a single task item dict for ListTasks responses."""
    item = {
        "id": task.task_id,  # backward-compat alias
        "task_id": task.task_id,
        "context_id": task.context_id or task.task_id,
        "status": {
            "state": state,
        },
        "created_at": task.created_at,
    }
    if task.response:
        item["status"]["message"] = filter_outbound(task.response)
        item["artifacts"] = [
            {"parts": [{"type": "text", "text": filter_outbound(task.response)}], "index": 0}
        ]
    return item


def _build_paginated_task_list(page_size: int = 20, continuation_token: Optional[str] = None) -> dict:
    """Build paginated task list from the task queue.

    Returns a dict with:
      items: list of task items
      hasMore: bool indicating if more pages exist
      nextPageToken: base64-encoded offset for next page (or None)
    """
    import base64

    q = _ensure_task_queue()

    # Collect and sort all tasks by created_at descending
    all_tasks: list[tuple[float, "_PendingTask"]] = []
    with q._lock:
        for task in list(q._pending.values()) + list(q._completed.values()):
            state = q._states.get(task.task_id, "unknown")
            all_tasks.append((task.created_at, task))

    all_tasks.sort(key=lambda x: x[0], reverse=True)

    # Decode continuation token (base64-encoded offset)
    offset = 0
    if continuation_token:
        try:
            offset = int(base64.b64decode(continuation_token).decode())
        except Exception:
            offset = 0
    offset = max(0, offset)

    page_tasks = all_tasks[offset: offset + page_size]
    has_more = len(all_tasks) > offset + page_size
    next_offset = offset + page_size
    next_token = base64.b64encode(str(next_offset).encode()).decode() if has_more else None

    items = []
    with q._lock:
        for created_at, task in page_tasks:
            state = q._states.get(task.task_id, "unknown")
            items.append(_build_task_list_item(task, state))

    return {
        "items": items,
        "hasMore": has_more,
        "nextPageToken": next_token,
    }


class A2ARequestHandler(BaseHTTPRequestHandler):
    """Handles A2A HTTP requests."""

    server: "A2AServer"

    def log_message(self, format, *args):
        logger.debug("A2A HTTP: %s", format % args)

    def _check_hmac_push(self, required: bool = True) -> bool:
        """Check X-HMAC-Key for push REST endpoints.

        For POST/DELETE (required=True): key must be present and match.
        For GET (required=False): key is optional; if present it must match.

        If no hmac_key is configured on the server (via __init__ or A2A_HMAC_KEY
        env var), the check is skipped — backward-compatible when auth is not
        yet configured.
        Returns True if the request is authorised, False otherwise (sends 401).
        """
        hmac_key = self.headers.get("X-HMAC-Key")
        expected_key = getattr(self.server, "hmac_key", None) or os.environ.get("A2A_HMAC_KEY")
        if not expected_key:
            # No key configured — allow through (auth not yet set up)
            return True
        if required:
            if not hmac_key or hmac_key != expected_key:
                self._send_json({"jsonrpc": "2.0", "error": {"code": -32603, "message": "Unauthorized"}}, 401)
                return False
        else:
            # Read-only: optional auth — if key is provided it must be valid
            if hmac_key and hmac_key != expected_key:
                self._send_json({"jsonrpc": "2.0", "error": {"code": -32603, "message": "Unauthorized"}}, 401)
                return False
        return True

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS headers on all A2A responses (browser-accessible agents)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        path = self.path.split("?")[0]
        # Advertise only the methods supported by the target endpoint to avoid
        # browser CORS mismatches (Issue 13).
        if path.startswith("/tasks/") and ":subscribe" in path:
            allowed = "GET, OPTIONS"
        elif path == "/message/stream":
            allowed = "POST, OPTIONS"
        else:
            allowed = "GET, POST, OPTIONS"
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", allowed)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _check_auth(self) -> bool:
        token = self.server.auth_token
        if not token:
            if self.server.require_auth:
                logger.warning(
                    "[A2A] Rejecting request from %s — A2A_REQUIRE_AUTH=true but no A2A_AUTH_TOKEN set",
                    self.client_address[0],
                )
                return False
            remote = self.client_address[0]
            allowed = remote in ("127.0.0.1", "::1")
            if allowed:
                logger.warning(
                    "[A2A] Allowing unauthenticated localhost request from %s — set A2A_AUTH_TOKEN; "
                    "localhost is not isolated in containers/shared namespaces",
                    remote,
                )
            return allowed
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth_header[7:].strip(), token)

    def _send_sse_stream(self, stream_id: str, task_id: str) -> None:
        """Stream SSE events to the client for stream_id.

        Events are pushed by TaskStateChangeHook as the task state changes.
        The stream closes when the task reaches a terminal state or the client
        disconnects.
        """
        from .sse_handler import get_sse_streamer

        streamer = get_sse_streamer()

        # Send initial state event immediately
        q = _ensure_task_queue()
        status = q.get_status(task_id)
        current_state = status.get("state", "unknown")

        def _send_line(line: str) -> bool:
            """Send one SSE-formatted line. Returns False if client disconnected."""
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        # Immediate initial event
        import json
        from .hooks import _event_name_for_state as _event_name
        initial_event_id = str(uuid.uuid4())
        initial = {
            "taskId": task_id,
            "state": current_state,
            "event": _event_name(current_state),
        }
        if not _send_line(f"id: {initial_event_id}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"event: {_event_name(current_state)}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"):
            streamer.close_stream(stream_id)
            return

        # If task is already in a terminal state, close stream immediately
        from .a2a_spec.tasks import is_terminal_state
        if is_terminal_state(current_state):
            streamer.close_stream(stream_id)
            return

        # Stream pending events until terminal state or client disconnect
        terminal_states = {"completed", "failed", "canceled", "rejected"}
        import time as _time
        poll_interval = 0.1  # seconds
        max_wait = float(os.getenv("A2A_SSE_TIMEOUT", "300"))  # 5 min default

        deadline = _time.time() + max_wait
        while _time.time() < deadline:
            lines = streamer.get_pending(stream_id)
            for line in lines:
                if not _send_line(line):
                    streamer.close_stream(stream_id)
                    return

            # Check if stream was closed (e.g., client disconnect)
            if streamer.is_closed(stream_id):
                return

            # Check if task reached terminal state
            status = q.get_status(task_id)
            if status.get("state") in terminal_states:
                # Send final state event and close
                term_state = status["state"]
                term_event_id = str(uuid.uuid4())
                term_event = {"taskId": task_id, "state": term_state, "event": _event_name(term_state)}
                _send_line(f"id: {term_event_id}\n")
                _send_line(f"event: {_event_name(term_state)}\n")
                _send_line(f"data: {json.dumps(term_event, ensure_ascii=False)}\n\n")
                streamer.close_stream(stream_id)
                return

            _time.sleep(poll_interval)

        # Timeout — send error and close
        _send_line('event: error\ndata: {"code": -38000, "message": "SSE stream timed out"}\n\n')
        streamer.close_stream(stream_id)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        # F-B008: GET /tasks/{id}:subscribe  (SubscribeToTask — SSE) — must check before /tasks/{id}
        if path.startswith("/tasks/") and ":subscribe" in path:
            parts = path.split("/tasks/")[1].split(":subscribe")
            if len(parts) == 2:
                self._rest_subscribe_to_task(parts[0])
                return

        # F-B001: GET /tasks/{id}  (GetTask)
        if path.startswith("/tasks/") and path.count("/") == 2:
            task_id = path.split("/tasks/")[1]
            self._rest_get_task(task_id)
            return

        # F-B010: GET /tasks/{id}/pushNotificationConfigs/{config_id}  (GetTaskPushNotificationConfig)
        if "/pushNotificationConfigs/" in path:
            # /tasks/{id}/pushNotificationConfigs/{config_id}  → 4 slashes, segments = ['', 'tasks', '{id}', 'pushNotificationConfigs', '{config_id}']
            segments = path.split("/")
            if len(segments) == 5 and segments[3] == "pushNotificationConfigs":
                self._rest_get_push_config(segments[2], segments[4])
                return

        # F-B010: GET /tasks/{id}/pushNotificationConfigs  (ListTaskPushNotificationConfigs)
        if "/pushNotificationConfigs" in path:
            # /tasks/{id}/pushNotificationConfigs  → 3 slashes, segments = ['', 'tasks', '{id}', 'pushNotificationConfigs']
            segments = path.split("/")
            if len(segments) == 4 and segments[3] == "pushNotificationConfigs":
                task_id = segments[2]
                self._rest_list_push_configs(task_id)
                return

        # F-B011: GET /extendedAgentCard
        if path == "/extendedAgentCard":
            self._rest_get_extended_agent_card()
            return

        # F-B006: GET /tasks  (ListTasks)
        if path == "/tasks":
            self._rest_list_tasks()
            return

        # Built-in endpoints
        if path == "/.well-known/agent.json":
            self._send_json(self.server.build_agent_card())
        elif path == "/health":
            self._send_json({
                "status": "ok",
                "agent": self.server.agent_name,
                "version": HERMES_VERSION,
            })
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        """Handle DELETE requests — REST push notification config delete."""
        path = self.path.split("?")[0]

        # F-B010: DELETE /tasks/{id}/pushNotificationConfigs/{config_id}
        if "/pushNotificationConfigs/" in path:
            # path = /tasks/{id}/pushNotificationConfigs/{config_id}  →  segments = ['', 'tasks', '{id}', 'pushNotificationConfigs', '{config_id}']
            segments = path.split("/")
            if len(segments) == 5 and segments[1] == "tasks" and segments[3] == "pushNotificationConfigs":
                task_id = segments[2]
                config_id = segments[4]
                self._rest_delete_push_config(task_id, config_id)
                return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        # F-B005: POST /message:send  (SendMessage)
        if path == "/message:send":
            self._do_rest_post(lambda body: self._rest_send_message(body))
            return

        # F-B009: POST /message/stream  (SendStreamingMessage)
        if path == "/message/stream":
            self._do_rest_post(lambda body: self._rest_send_message_stream(body))
            return

        # F-B007: POST /tasks/{id}:cancel  (CancelTask)
        if path.startswith("/tasks/") and ":cancel" in path:
            task_id = path.split("/tasks/")[1].split(":cancel")[0]
            self._rest_cancel_task(task_id)
            return

        # F-B010: POST /tasks/{id}/pushNotificationConfigs  (CreateTaskPushNotificationConfig)
        if path.startswith("/tasks/") and "/pushNotificationConfigs" in path:
            segments = path.strip("/").split("/")
            # /tasks/{id}/pushNotificationConfigs → 3 segments: ['tasks', '{id}', 'pushNotificationConfigs']
            if len(segments) == 3 and segments[0] == "tasks" and segments[2] == "pushNotificationConfigs":
                task_id = segments[1]
                self._do_rest_post(lambda body: self._rest_create_push_config(task_id, body))
                return

        # Fall through to JSON-RPC handling
        self._do_json_rpc()

    def _handle_send_subscribe(self, params: dict, rpc_id) -> None:
        """Handle tasks/sendSubscribe — stream SSE events to the client.

        Writes headers + initial state event directly to wfile, then loops
        polling pending events until the task reaches a terminal state or the
        client disconnects.
        """
        from .sse_handler import get_sse_streamer
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND, is_terminal_state, build_error_response, A2A_ERR_INTERNAL

        tid = params.get("taskId", "")
        if not tid:
            self._send_json({"jsonrpc": "2.0", "id": rpc_id, "error": build_error_response(-32600, "Invalid Request: taskId is required", id=rpc_id)})
            return

        q = _ensure_task_queue()
        status = q.get_status(tid)
        if status["state"] == "unknown":
            self._send_json({"jsonrpc": "2.0", "id": rpc_id, "error": build_error_response(A2A_ERR_TASK_NOT_FOUND, f"Task not found: {tid}", id=rpc_id)})
            return

        streamer = get_sse_streamer()
        stream_id = streamer.open_stream(tid)
        current_state = status.get("state", "unknown")

        # Send SSE headers directly — bypass _send_json so Content-Type is text/event-stream
        import json
        from .hooks import _event_name_for_state as _event_name

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # Do NOT send Connection: keep-alive — closing the SSE stream should close
        # the TCP connection so urllib.request.urlopen.read() returns after data.
        self.close_connection = True
        self.send_header("X-Stream-Id", stream_id)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

        def _send_line(line: str) -> bool:
            """Send one SSE line. Returns False on client disconnect."""
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        # Initial state event
        initial_event_id = str(uuid.uuid4())
        initial_payload = json.dumps({"taskId": tid, "state": current_state, "event": _event_name(current_state)}, ensure_ascii=False)
        if not _send_line(f"id: {initial_event_id}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"event: {_event_name(current_state)}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"data: {initial_payload}\n\n"):
            streamer.close_stream(stream_id)
            return

        # If already terminal, close stream immediately after initial event
        if is_terminal_state(current_state):
            streamer.close_stream(stream_id)
            return

        # Stream pending events until terminal state or client disconnect
        poll_interval = 0.1
        max_wait = float(os.getenv("A2A_SSE_TIMEOUT", "300"))
        deadline = time.time() + max_wait

        while time.time() < deadline:
            lines = streamer.get_pending(stream_id)
            for line in lines:
                if not _send_line(line):
                    streamer.close_stream(stream_id)
                    return

            if streamer.is_closed(stream_id):
                return

            status = q.get_status(tid)
            if is_terminal_state(status.get("state", "")):
                term_state = status["state"]
                term_payload = json.dumps({"taskId": tid, "state": term_state, "event": _event_name(term_state)}, ensure_ascii=False)
                _send_line(f"event: {_event_name(term_state)}\n")
                _send_line(f"data: {term_payload}\n\n")
                streamer.close_stream(stream_id)
                return

            time.sleep(poll_interval)

        # Timeout
        _send_line('event: error\ndata: {"code": -38000, "message": "SSE stream timed out"}\n\n')
        streamer.close_stream(stream_id)

    def _handle_push_subscribe(self, params: dict, rpc_id) -> dict:
        """Handle push notification subscription: tasks/pushNotification/subscribe."""
        from .subscription_store import get_subscription_store
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND

        tid = params.get("taskId", "")
        webhook_url = params.get("url", "") or params.get("webhookUrl", "")
        hmac_key = params.get("hmacKey", "") or params.get("hmac_key", "")

        if not tid:
            return build_error_response(-32600, "Invalid Request: taskId is required", id=rpc_id)
        if not webhook_url:
            return build_error_response(-32600, "Invalid Request: webhook url is required", id=rpc_id)
        if not hmac_key:
            return build_error_response(-32600, "Invalid Request: hmacKey is required", id=rpc_id)

        q = _ensure_task_queue()
        status = q.get_status(tid)
        if status["state"] == "unknown":
            return build_error_response(
                A2A_ERR_TASK_NOT_FOUND, f"Task not found: {tid}", id=rpc_id
            )

        try:
            store = get_subscription_store()
        except Exception:
            return build_error_response(
                -38002, "Push notification not supported", id=rpc_id
            )

        sub_id = store.add(tid, webhook_url, hmac_key)
        return {"subscriptionId": sub_id, "taskId": tid}

    def _handle_push_unsubscribe(self, params: dict, rpc_id) -> dict:
        """Handle push notification unsubscribe: DELETE via params."""
        from .subscription_store import get_subscription_store

        subscription_id = params.get("subscriptionId", "")
        if not subscription_id:
            return build_error_response(-32600, "Invalid Request: subscriptionId is required", id=rpc_id)

        try:
            store = get_subscription_store()
        except Exception:
            return build_error_response(
                -38002, "Push notification not supported", id=rpc_id
            )

        removed = store.remove(subscription_id)
        if not removed:
            return build_error_response(
                -38000, f"Subscription not found: {subscription_id}", id=rpc_id
            )
        return {"subscriptionId": subscription_id, "removed": True}

    def _handle_task_send(self, params: dict, rpc_id) -> dict:
        task_id = params.get("id", str(uuid.uuid4()))
        message = params.get("message", {})

        text_parts = []
        for part in message.get("parts", []):
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            return {
                "id": task_id,
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"type": "text", "text": "Empty message"}], "index": 0}],
            }

        user_text = sanitize_inbound(user_text)
        metadata = message.get("metadata", {})
        # Extract context_id from top-level params or message metadata (required per spec)
        context_id = params.get("contextId") or metadata.get("context_id") or task_id
        hermes_meta = metadata.get("hermes", {}) if isinstance(metadata.get("hermes", {}), dict) else {}
        worker_at = metadata.get("worker_at", "")

        # worker_at=target: distributed ephemeral worker — bypass queue, webhook,
        # task.ready.wait(). Run local worker directly and return result synchronously.
        if worker_at == "target" or hermes_meta.get("execution") == "remote_subprocess":
            import logging as _log
            _log.getLogger(__name__).info("[A2A] worker_at=target — dispatching to _handle_task_send_mode3")
            from .tool_handlers import _handle_task_send_mode3
            return _handle_task_send_mode3(params, metadata, user_text)

        if "sender_name" not in metadata:
            from_field = params.get("from") or params.get("sender", {}).get("name")
            metadata["sender_name"] = (
                from_field
                or metadata.get("agent_name")
                or f"agent-{self.client_address[0]}"
            )
        raw_name = metadata.get("sender_name", "") or ""
        metadata["sender_name"] = "".join(c for c in raw_name if c.isalnum() or c in "-_.@ ")[:64]

        # Mark peer-originated tasks so hooks bypass Telegram webhook delivery
        metadata["_a2a_origin"] = "peer"

        audit.log("task_received", {"task_id": task_id, "length": len(user_text)})

        q = _ensure_task_queue()

        # --- Idempotency check first (before task_id collision check) ---
        from .persistence import get_idempotency_store
        idem_store = get_idempotency_store()
        idem_key = params.get("idempotencyKey")
        if idem_key:
            # Check for payload conflict
            conflict, existing_task_id = idem_store.check_conflict(idem_key, params)
            if conflict:
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -38004,
                            "message": f"Non-idempotent task: idempotency key '{idem_key}' already used with a different payload",
                            "data": {"existingTaskId": existing_task_id},
                        },
                        "id": rpc_id,
                    },
                    409,
                )
                return
            # Return cached result on replay
            cached = idem_store.get(idem_key)
            if cached is not None:
                cached_task_id, cached_result = cached
                logger.debug("[A2A] Idempotency replay for key=%s → task_id=%s", idem_key, cached_task_id)
                return cached_result

        # Task-ID collision check
        if q.find_task_by_id(task_id) is not None:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -38004, "message": "Task ID already in use", "data": None},
                    "id": rpc_id,
                },
                409,
            )
            return

        task = q.enqueue(task_id, user_text, metadata, context_id=context_id)
        if task is None:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "Agent busy — too many pending tasks", "data": None},
                    "id": rpc_id,
                },
                503,
            )
            return

        # Wake up the gateway via internal webhook. deliver_only=True tells
        # the webhook handler to invoke the agent without routing to Telegram.
        # The agent's pre_llm_call hook drains the shared task queue, processes
        # the task, and post_llm_call marks it complete.
        def _on_webhook_failure(tid: str) -> None:
            t = _ensure_task_queue().find_task_by_id(tid)
            if t is not None and t.response is None:
                t.response = "(webhook delivery failed)"
                t.ready.set()

        def _run_async_webhook():
            asyncio.run(_trigger_webhook_async(
                task_id=task_id, deliver_only=True, on_failure=_on_webhook_failure
            ))

        threading.Thread(target=_run_async_webhook, daemon=True).start()

        task.ready.wait(timeout=_RESPONSE_TIMEOUT)

        if task.response is None:
            return {
                "id": task_id,
                "status": {"state": "working"},
                "artifacts": [{"parts": [{"type": "text", "text": "(processing — poll with tasks/get)"}], "index": 0}],
            }

        filtered = filter_outbound(task.response)
        audit.log("task_completed", {"task_id": task_id, "response_length": len(filtered)})

        result = {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": filtered}], "index": 0}],
        }

        # Store result for idempotency replay
        if idem_key:
            idem_store.set(idem_key, task_id, params, result)

        # Emit TaskArtifactUpdateEvent over SSE and push delivery for each artifact
        ctx_id = task.context_id or task_id
        try:
            from .sse_handler import emit_artifact_event, get_sse_streamer
            streamer = get_sse_streamer()
            stream_ids = streamer.get_stream_ids_for_task(task_id)
            if stream_ids:
                for artifact in result.get("artifacts", []):
                    evt = emit_artifact_event(
                        task_id=task_id,
                        context_id=ctx_id,
                        artifact=artifact,
                        metadata={"index": artifact.get("index")},
                    )
                    for sid in stream_ids:
                        streamer.push_event(sid, evt)
        except Exception:
            pass  # SSE delivery is best-effort; do not fail the response

        # Also deliver artifact events as push notifications to webhook subscribers
        try:
            from .subscription_store import get_subscription_store
            from .push_delivery import get_push_delivery
            store = get_subscription_store()
            pusher = get_push_delivery()
            subs = store.get(task_id)
            for sub in subs:
                for artifact in result.get("artifacts", []):
                    payload = {
                        "kind": "artifact",
                        "contextId": ctx_id,
                        "taskId": task_id,
                        "artifact": artifact,
                        "metadata": {"index": artifact.get("index")},
                    }
                    pusher.deliver_with_retry(sub.url, payload, sub.hmac_key)
        except Exception:
            pass  # Push delivery is best-effort; do not fail the response

        return result

    # -------------------------------------------------------------------------
    # HTTP method dispatch helpers
    # -------------------------------------------------------------------------

    def _do_rest_post(self, handler) -> None:
        """Parse body and call the given REST handler. Checks auth and rate limit."""
        if not self._check_auth():
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Unauthorized"}},
                401,
            )
            return

        if not self.server.limiter.allow(self.client_address[0]):
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Rate limit exceeded"}},
                429,
            )
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_json({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Content-Length"}}, 400)
            return

        if length <= 0 or length > 65536:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32600, "message": f"Content-Length must be 1-65536, got {length}"}},
                413 if length > 65536 else 400,
            )
            return

        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, 400)
            return

        audit.log("rest_request", {"path": self.path, "client": self.client_address[0]})
        handler(body)

    def _do_json_rpc(self) -> None:
        """Handle JSON-RPC over POST. Replaces the old do_POST body."""
        if not self._check_auth():
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "Unauthorized", "data": None},
                    "id": None,
                },
                401,
            )
            return

        if not self.server.limiter.allow(self.client_address[0]):
            audit.log("rate_limited", {"client": self.client_address[0]})
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "Rate limit exceeded", "data": None},
                    "id": None,
                },
                429,
            )
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Invalid Content-Length", "data": None},
                    "id": None,
                },
                400,
            )
            return

        if length <= 0 or length > 65536:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32600,
                        "message": f"Content-Length must be 1-65536, got {length}",
                        "data": None,
                    },
                    "id": None,
                },
                413 if length > 65536 else 400,
            )
            return

        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": "Parse error", "data": None},
                    "id": None,
                },
                400,
            )
            return

        method = body.get("method", "")
        params = body.get("params", {})
        rpc_id = body.get("id")

        # Reject requests without the jsonrpc field
        if body.get("jsonrpc") != "2.0":
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Invalid Request: missing or invalid jsonrpc field", "data": None},
                    "id": rpc_id if rpc_id is not None else None,
                },
                400,
            )
            return

        audit.log("rpc_request", {"method": method, "client": self.client_address[0]})

        if not method:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Invalid Request: missing method", "data": None},
                    "id": rpc_id,
                },
                400,
            )
            return

        if method == "tasks/send":
            result = self._handle_task_send(params, rpc_id)
        elif method == "tasks/get":
            tid = params.get("id", "")
            status = _ensure_task_queue().get_status(tid)
            if status["state"] == "unknown":
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -38000, "message": f"Task not found: {tid}", "data": None},
                        "id": rpc_id,
                    },
                    404,
                )
                return
            result = {"id": tid, "status": {"state": status["state"]}}
            if status.get("response"):
                result["artifacts"] = [{"parts": [{"type": "text", "text": filter_outbound(status["response"])}], "index": 0}]
                # Emit TaskArtifactUpdateEvent over SSE for tasks/get polling responses
                try:
                    from .sse_handler import emit_artifact_event, get_sse_streamer
                    streamer = get_sse_streamer()
                    stream_ids = streamer.get_stream_ids_for_task(tid)
                    if stream_ids:
                        pending_task = _ensure_task_queue().find_task_by_id(tid)
                        ctx_id = pending_task.context_id if pending_task else tid
                        for artifact in result.get("artifacts", []):
                            evt = emit_artifact_event(
                                task_id=tid,
                                context_id=ctx_id,
                                artifact=artifact,
                                metadata={"index": artifact.get("index")},
                            )
                            for sid in stream_ids:
                                streamer.push_event(sid, evt)
                except Exception:
                    pass  # SSE delivery is best-effort
        elif method == "tasks/cancel":
            tid = params.get("id", "")
            from .worker_registry import cancel_worker
            status = _ensure_task_queue().get_status(tid)
            if status["state"] == "unknown":
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -38000, "message": f"Task not found: {tid}", "data": None},
                        "id": rpc_id,
                    },
                    404,
                )
                return
            if status["state"] in ("completed", "failed", "canceled"):
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -38001, "message": f"Task not cancelable: task is {status['state']}", "data": None},
                        "id": rpc_id,
                    },
                    409,
                )
                return
            worker_canceled = cancel_worker(tid)
            _ensure_task_queue().cancel(tid)
            result = {"id": tid, "status": {"state": "canceled"}, "metadata": {"hermes": {"worker_canceled": worker_canceled}}}
        elif method == "tasks/pushNotification/subscribe":
            tid = params.get("taskId", "")
            webhook_url = params.get("url", "") or params.get("webhookUrl", "")
            hmac_key = params.get("hmacKey", "") or params.get("hmac_key", "")
            subscription_id = params.get("subscriptionId", "")
            if subscription_id:
                result = self._handle_push_unsubscribe(params, rpc_id)
            elif tid and webhook_url and hmac_key:
                result = self._handle_push_subscribe(params, rpc_id)
            else:
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32600, "message": "Invalid Request: missing required params for pushNotification/subscribe", "data": None},
                        "id": rpc_id,
                    },
                    400,
                )
                return
        elif method == "tasks/pushNotification":
            subscription_id = params.get("subscriptionId", "")
            result = self._handle_push_unsubscribe(params, rpc_id)
        elif method == "tasks/sendSubscribe":
            self._handle_send_subscribe(params, rpc_id)
            return
        elif method == "tasks/list":
            page_size = min(max(int(params.get("pageSize", 20)), 1), 100)
            token = params.get("continuationToken")
            result = _build_paginated_task_list(page_size, token)
        else:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}", "data": None},
                    "id": rpc_id,
                },
                404,
            )
            return

        self._send_json({"jsonrpc": "2.0", "result": result, "id": rpc_id})

    # -------------------------------------------------------------------------
    # REST handlers (F-B001, F-B005–F-B011)
    # -------------------------------------------------------------------------

    def _rest_get_task(self, task_id: str) -> None:
        """F-B001: GET /tasks/{id} — return Task object."""
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND
        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return
        result = {"id": task_id, "status": {"state": status["state"]}}
        if status.get("response"):
            result["artifacts"] = [
                {"parts": [{"type": "text", "text": filter_outbound(status["response"])}], "index": 0}
            ]
        self._send_json(result)

    def _rest_list_tasks(self) -> None:
        """F-B006: GET /tasks — return paginated list of tasks.

        Query params:
          page_size (int, default 20): number of items per page
          continuation_token (str, optional): base64-encoded offset for next page
        """
        # Parse query params
        from urllib.parse import parse_qs
        query = self.path.split("?")[1] if "?" in self.path else ""
        params = parse_qs(query)
        try:
            page_size = int(params.get("page_size", ["20"])[0])
        except (ValueError, TypeError):
            page_size = 20
        page_size = max(1, min(page_size, 100))  # clamp to [1, 100]
        continuation_token = params.get("continuation_token", [None])[0]

        result = _build_paginated_task_list(page_size, continuation_token)
        self._send_json(result)

    def _rest_cancel_task(self, task_id: str) -> None:
        """F-B007: POST /tasks/{id}:cancel — cancel a pending task."""
        from .worker_registry import cancel_worker
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND, A2A_ERR_TASK_NOT_CANCELABLE

        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return
        if status["state"] in ("completed", "failed", "canceled"):
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_CANCELABLE, "message": f"Task not cancelable: task is {status['state']}"}},
                409,
            )
            return
        worker_canceled = cancel_worker(task_id)
        _ensure_task_queue().cancel(task_id)
        self._send_json({
            "id": task_id,
            "status": {"state": "canceled"},
            "metadata": {"hermes": {"worker_canceled": worker_canceled}},
        })

    def _rest_subscribe_to_task(self, task_id: str) -> None:
        """F-B008: GET /tasks/{id}:subscribe — SSE stream for task updates."""
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND, is_terminal_state
        from .sse_handler import get_sse_streamer
        from .hooks import _event_name_for_state as _event_name

        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return

        streamer = get_sse_streamer()
        stream_id = streamer.open_stream(task_id)
        current_state = status.get("state", "unknown")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.close_connection = True
        self.send_header("X-Stream-Id", stream_id)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

        def _send_line(line: str) -> bool:
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        initial_event_id = str(uuid.uuid4())
        initial_payload = json.dumps({"taskId": task_id, "state": current_state, "event": _event_name(current_state)}, ensure_ascii=False)
        if not _send_line(f"id: {initial_event_id}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"event: {_event_name(current_state)}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"data: {initial_payload}\n\n"):
            streamer.close_stream(stream_id)
            return

        if is_terminal_state(current_state):
            streamer.close_stream(stream_id)
            return

        poll_interval = 0.1
        max_wait = float(os.getenv("A2A_SSE_TIMEOUT", "300"))
        deadline = time.time() + max_wait

        while time.time() < deadline:
            lines = streamer.get_pending(stream_id)
            for line in lines:
                if not _send_line(line):
                    streamer.close_stream(stream_id)
                    return
            if streamer.is_closed(stream_id):
                return
            status = _ensure_task_queue().get_status(task_id)
            if is_terminal_state(status.get("state", "")):
                term_state = status["state"]
                term_payload = json.dumps({"taskId": task_id, "state": term_state, "event": _event_name(term_state)}, ensure_ascii=False)
                _send_line(f"event: {_event_name(term_state)}\n")
                _send_line(f"data: {term_payload}\n\n")
                streamer.close_stream(stream_id)
                return
            time.sleep(poll_interval)

        _send_line('event: error\ndata: {"code": -38000, "message": "SSE stream timed out"}\n\n')
        streamer.close_stream(stream_id)

    def _rest_send_message(self, body: dict) -> None:
        """F-B005: POST /message:send — send a message and return a task result."""
        message = body.get("message", {})
        text_parts = []
        for part in message.get("parts", []):
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            self._send_json({
                "id": str(uuid.uuid4()),
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"type": "text", "text": "Empty message"}], "index": 0}],
            })
            return

        user_text = sanitize_inbound(user_text)
        metadata = message.get("metadata", {})
        task_id = body.get("id") or str(uuid.uuid4())
        context_id = body.get("contextId") or metadata.get("context_id") or task_id

        if "sender_name" not in metadata:
            from_field = body.get("from") or body.get("sender", {}).get("name")
            metadata["sender_name"] = (
                from_field or metadata.get("agent_name") or f"agent-{self.client_address[0]}"
            )
        raw_name = metadata.get("sender_name", "") or ""
        metadata["sender_name"] = "".join(c for c in raw_name if c.isalnum() or c in "-_.@ ")[:64]
        metadata["_a2a_origin"] = "peer"

        audit.log("task_received", {"task_id": task_id, "length": len(user_text)})

        q = _ensure_task_queue()

        # Idempotency
        from .persistence import get_idempotency_store
        idem_store = get_idempotency_store()
        idem_key = body.get("idempotencyKey")
        if idem_key:
            conflict, existing_task_id = idem_store.check_conflict(idem_key, body)
            if conflict:
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -38004,
                            "message": f"Non-idempotent task: idempotency key '{idem_key}' already used with a different payload",
                            "data": {"existingTaskId": existing_task_id},
                        }
                    },
                    409,
                )
                return
            cached = idem_store.get(idem_key)
            if cached is not None:
                self._send_json(cached[1])
                return

        if q.find_task_by_id(task_id) is not None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -38004, "message": "Task ID already in use"}},
                409,
            )
            return

        task = q.enqueue(task_id, user_text, metadata, context_id=context_id)
        if task is None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Agent busy — too many pending tasks"}},
                503,
            )
            return

        def _on_webhook_failure(tid: str) -> None:
            t = _ensure_task_queue().find_task_by_id(tid)
            if t is not None and t.response is None:
                t.response = "(webhook delivery failed)"
                t.ready.set()

        def _run_async_webhook():
            asyncio.run(_trigger_webhook_async(
                task_id=task_id, deliver_only=True, on_failure=_on_webhook_failure
            ))

        threading.Thread(target=_run_async_webhook, daemon=True).start()

        task.ready.wait(timeout=_RESPONSE_TIMEOUT)

        if task.response is None:
            result = {
                "id": task_id,
                "status": {"state": "working"},
                "artifacts": [{"parts": [{"type": "text", "text": "(processing — poll with tasks/get)"}], "index": 0}],
            }
            self._send_json(result, 200)
            return

        filtered = filter_outbound(task.response)
        audit.log("task_completed", {"task_id": task_id, "response_length": len(filtered)})

        result = {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": filtered}], "index": 0}],
        }

        if idem_key:
            idem_store.set(idem_key, task_id, body, result)

        self._send_json(result, 200)

    def _rest_send_message_stream(self, body: dict) -> None:
        """F-B009: POST /message/stream — streaming response via SSE."""
        from .a2a_spec.tasks import is_terminal_state
        from .sse_handler import get_sse_streamer
        from .hooks import _event_name_for_state as _event_name

        message = body.get("message", {})
        text_parts = []
        for part in message.get("parts", []):
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            self._send_json({
                "id": str(uuid.uuid4()),
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"type": "text", "text": "Empty message"}], "index": 0}],
            })
            return

        user_text = sanitize_inbound(user_text)
        metadata = message.get("metadata", {})
        task_id = body.get("id") or str(uuid.uuid4())
        context_id = body.get("contextId") or metadata.get("context_id") or task_id

        if "sender_name" not in metadata:
            from_field = body.get("from") or body.get("sender", {}).get("name")
            metadata["sender_name"] = (
                from_field or metadata.get("agent_name") or f"agent-{self.client_address[0]}"
            )
        raw_name = metadata.get("sender_name", "") or ""
        metadata["sender_name"] = "".join(c for c in raw_name if c.isalnum() or c in "-_.@ ")[:64]
        metadata["_a2a_origin"] = "peer"

        q = _ensure_task_queue()
        task = q.enqueue(task_id, user_text, metadata, context_id=context_id)
        if task is None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Agent busy — too many pending tasks"}},
                503,
            )
            return

        streamer = get_sse_streamer()
        stream_id = streamer.open_stream(task_id)

        def _on_webhook_failure(tid: str) -> None:
            t = _ensure_task_queue().find_task_by_id(tid)
            if t is not None and t.response is None:
                t.response = "(webhook delivery failed)"
                t.ready.set()

        def _run_async_webhook():
            asyncio.run(_trigger_webhook_async(
                task_id=task_id, deliver_only=True, on_failure=_on_webhook_failure
            ))

        threading.Thread(target=_run_async_webhook, daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.close_connection = True
        self.send_header("X-Stream-Id", stream_id)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

        def _send_line(line: str) -> bool:
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        # Initial working event
        initial_event_id = str(uuid.uuid4())
        initial_payload = json.dumps({"taskId": task_id, "state": "working", "event": _event_name("working")}, ensure_ascii=False)
        _send_line(f"id: {initial_event_id}\n")
        _send_line(f"event: {_event_name('working')}\n")
        _send_line(f"data: {initial_payload}\n\n")

        poll_interval = 0.1
        max_wait = float(os.getenv("A2A_SSE_TIMEOUT", "300"))
        deadline = time.time() + max_wait

        while time.time() < deadline:
            lines = streamer.get_pending(stream_id)
            for line in lines:
                if not _send_line(line):
                    streamer.close_stream(stream_id)
                    return
            if streamer.is_closed(stream_id):
                return
            status = _ensure_task_queue().get_status(task_id)
            state = status.get("state", "working")
            if is_terminal_state(state):
                term_payload = json.dumps({"taskId": task_id, "state": state, "event": _event_name(state)}, ensure_ascii=False)
                _send_line(f"event: {_event_name(state)}\n")
                _send_line(f"data: {term_payload}\n\n")
                streamer.close_stream(stream_id)
                return
            time.sleep(poll_interval)

        _send_line('event: error\ndata: {"code": -38000, "message": "SSE stream timed out"}\n\n')
        streamer.close_stream(stream_id)

    def _rest_create_push_config(self, task_id: str, body: dict) -> None:
        """F-B010: POST /tasks/{id}/pushNotificationConfigs — create a push config.

        Uses CreateTaskPushNotificationConfigRequest + create_push_config from push_delivery.
        Requires X-HMAC-Key header.
        """
        if not self._check_hmac_push(required=True):
            return
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND

        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return

        url = body.get("url", "")
        hmac_key = body.get("hmacKey", "") or body.get("hmac_key", "")
        if not url:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32600, "message": "url is required"}},
                400,
            )
            return
        if not hmac_key:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32600, "message": "hmacKey is required"}},
                400,
            )
            return

        # Build AuthenticationInfo from request body
        auth_info = None
        raw_auth = body.get("authentication") or body.get("auth", {})
        if raw_auth:
            auth_info = AuthenticationInfo(
                auth_type=raw_auth.get("authType") or raw_auth.get("auth_type"),
                auth_code=raw_auth.get("authCode") or raw_auth.get("auth_code"),
            )

        # Parse into CreateTaskPushNotificationConfigRequest
        req = CreateTaskPushNotificationConfigRequest(
            id=body.get("id", ""),
            task_id=task_id,
            push_transport_type=body.get("pushTransportType", "webhook"),
            endpoint=url,
            authentication=auth_info,
            metadata=body.get("metadata"),
        )

        # Delegate to push_delivery CRUD
        cfg = create_push_config(
            task_id=req.task_id,
            push_transport_type=req.push_transport_type,
            endpoint=req.endpoint,
            authentication=req.authentication,
            metadata=req.metadata,
        )

        response = CreateTaskPushNotificationConfigResponse(config=cfg)
        self._send_json({
            "configId": response.config.id,
            "config": {
                "id": response.config.id,
                "taskId": response.config.task_id,
                "pushTransportType": response.config.push_transport_type,
                "endpoint": response.config.endpoint,
                "authentication": {
                    "authType": response.config.authentication.auth_type,
                    "authCode": response.config.authentication.auth_code,
                } if response.config.authentication else None,
                "metadata": response.config.metadata,
            },
        }, 201)

    def _rest_get_push_config(self, task_id: str, config_id: str) -> None:
        """F-B010: GET /tasks/{id}/pushNotificationConfigs/{config_id} — get a push config.

        Uses GetTaskPushNotificationConfigRequest + get_push_config from push_delivery.
        X-HMAC-Key optional; if provided must be valid.
        """
        if not self._check_hmac_push(required=False):
            return
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND

        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return

        req = GetTaskPushNotificationConfigRequest(task_id=task_id, config_id=config_id)
        cfg = get_push_config(req.task_id, req.config_id)

        if cfg is None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -38000, "message": f"Push notification config not found: {config_id}"}},
                404,
            )
            return

        response = GetTaskPushNotificationConfigResponse(config=cfg)
        self._send_json({
            "configId": response.config.id,
            "config": {
                "id": response.config.id,
                "taskId": response.config.task_id,
                "pushTransportType": response.config.push_transport_type,
                "endpoint": response.config.endpoint,
                "authentication": {
                    "authType": response.config.authentication.auth_type,
                    "authCode": response.config.authentication.auth_code,
                } if response.config.authentication else None,
                "metadata": response.config.metadata,
            },
        })

    def _rest_list_push_configs(self, task_id: str) -> None:
        """F-B010: GET /tasks/{id}/pushNotificationConfigs — list push configs for a task.

        Uses ListTaskPushNotificationConfigsRequest + list_push_configs from push_delivery.
        """
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND

        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return

        req = ListTaskPushNotificationConfigsRequest(task_id=task_id)
        configs = list_push_configs(req.task_id)

        response = ListTaskPushNotificationConfigsResponse(
            items=configs,
            has_more=False,
        )
        self._send_json({
            "items": [
                {
                    "id": c.id,
                    "taskId": c.task_id,
                    "pushTransportType": c.push_transport_type,
                    "endpoint": c.endpoint,
                    "authentication": {
                        "authType": c.authentication.auth_type,
                        "authCode": c.authentication.auth_code,
                    } if c.authentication else None,
                    "metadata": c.metadata,
                }
                for c in response.items
            ],
            "hasMore": response.has_more,
        })

    def _rest_delete_push_config(self, task_id: str, config_id: str) -> None:
        """F-B010: DELETE /tasks/{id}/pushNotificationConfigs/{config_id} — delete a push config.

        Uses DeleteTaskPushNotificationConfigRequest + delete_push_config from push_delivery.
        Requires X-HMAC-Key header.
        """
        if not self._check_hmac_push(required=True):
            return
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND

        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": A2A_ERR_TASK_NOT_FOUND, "message": f"Task not found: {task_id}"}},
                404,
            )
            return

        req = DeleteTaskPushNotificationConfigRequest(task_id=task_id, config_id=config_id)
        deleted_id = delete_push_config(req.task_id, req.config_id)

        if deleted_id is None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -38000, "message": f"Push notification config not found: {config_id}"}},
                404,
            )
            return

        response = DeleteTaskPushNotificationConfigResponse(config_id=deleted_id)
        self._send_json({"configId": response.config_id, "config_id": response.config_id})

    def _rest_get_extended_agent_card(self) -> None:
        """F-B011: GET /extendedAgentCard — return full extended AgentCard."""
        from hermes_agent_a2a.a2a_spec import build_extended_agent_card

        public_url = os.getenv("A2A_PUBLIC_URL", "").rstrip("/")
        if not public_url:
            host, port = self.server.server_address
            public_url = f"http://{host}:{port}"

        card = build_extended_agent_card(
            overrides={
                "name": self.server.agent_name,
                "description": self.server.agent_description,
                "url": public_url,
                "version": HERMES_VERSION,
                "capabilities": {
                    "streaming": True,
                    "pushNotifications": True,
                    "stateTransitionHistory": False,
                },
            }
        )
        self._send_json(card)


class A2AServer(ThreadingHTTPServer):
    """Threaded HTTP server with A2A configuration.

    Each request runs in its own thread so tasks/send can block waiting
    for agent response without starving health checks and agent card requests.
    """

    daemon_threads = True

    def __init__(self, host: str, port: int, hmac_key: Optional[str] = None):
        self.agent_name = os.getenv("A2A_AGENT_NAME", "hermes-agent")
        self.agent_description = os.getenv("A2A_AGENT_DESCRIPTION", "A self-improving AI agent powered by Hermes")
        self.auth_token = os.getenv("A2A_AUTH_TOKEN", "")
        self.require_auth = os.getenv("A2A_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
        self.hmac_key = hmac_key or os.environ.get("A2A_HMAC_KEY")
        if not self.auth_token:
            logger.warning(
                "[A2A] No A2A_AUTH_TOKEN set — only localhost requests will be accepted, "
                "and localhost is not safe in containers. Set A2A_REQUIRE_AUTH=true to reject all unauthenticated requests."
            )
        self.limiter = RateLimiter()
        _start_orphaned_task_watchdog(_ensure_task_queue())
        super().__init__((host, port), A2ARequestHandler)

    def build_agent_card(self) -> dict:
        public_url = os.getenv("A2A_PUBLIC_URL", "").rstrip("/")
        if not public_url:
            host, port = self.server_address
            public_url = f"http://{host}:{port}"
        return {
            "name": self.agent_name,
            "agentId": self.agent_name,
            "description": self.agent_description,
            "url": public_url,
            "version": HERMES_VERSION,
            "protocol": "a2a",
            "protocolVersion": "0.2.0",
            "provider": {"organization": "Hermes Fleet", "category": "official"},
            "capabilities": {
                "streaming": True,
                "pushNotifications": True,
                "multiTurn": False,
                "structuredMetadata": True,
            },
            "skills": [
                {
                    "id": "general",
                    "name": "General Assistant",
                }
            ],
            "authentication": {
                "schemes": ["bearer"] if self.auth_token else [],
            },
        }
