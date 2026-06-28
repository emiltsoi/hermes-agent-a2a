"""A2A HTTP server — runs in a background thread, no asyncio.

Handles inbound A2A JSON-RPC requests. Messages are queued and picked up
by the pre_llm_call hook; responses are captured by post_llm_call and
returned to the caller.
"""

from __future__ import annotations

import asyncio
import datetime
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

from .security import audit, filter_outbound, sanitize_inbound
from .rate_limiter import RateLimiter, RateLimitConfig
from .a2a_spec.tasks import build_error_response
from .a2a_spec.push import (
    CreateTaskPushNotificationConfigRequest,
    CreateTaskPushNotificationConfigResponse,
    GetTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigResponse,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    DeleteTaskPushNotificationConfigRequest,
    AuthenticationInfo,
)
from .push_delivery import (
    create_push_config,
    get_push_config,
    list_push_configs,
    delete_push_config,
)
from .a2a_direct import call_async
from .webhook_delivery import trigger, trigger_async


logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
_TASK_CACHE_MAX = 100000
_MAX_PENDING = 100000
_RESPONSE_TIMEOUT = int(os.getenv("A2A_RESPONSE_TIMEOUT", "120"))  # seconds to wait for agent response

try:
    from hermes_cli import __version__ as HERMES_VERSION
except Exception:
    HERMES_VERSION = "0.0.0"
    logger.warning("[A2A] Failed to import hermes_cli.__version, using fallback '0.0.0'. hermes_cli may be misinstalled.")

# A2A protocol version and extensions per Section 3.2.6
A2A_VERSION = "1.0"
# Comma-separated extension URIs; set via A2A_EXTENSIONS env var
_A2A_EXTENSIONS_ENV = os.getenv("A2A_EXTENSIONS", "")


def _get_a2a_extensions() -> str:
    """Return A2A-Extensions header value (comma-separated URIs or empty string)."""
    return _A2A_EXTENSIONS_ENV.strip()


def _start_async_webhook_delivery(task_id: str) -> None:
    """Fire-and-forget webhook delivery for a task. Closes over the on_failure
    callback that marks the task as failed-delivery. Used by the three call
    sites that previously duplicated the closure+thread pattern. (LOW-07,
    a2a-review-20260602)
    """
    def _on_webhook_failure(tid: str) -> None:
        t = _ensure_task_queue().find_task_by_id(tid)
        if t is not None and t.response is None:
            t.response = "(webhook delivery failed)"
            t.ready.set()

    def _run_async_webhook():
        asyncio.run(trigger_async(
            task_id=task_id, deliver_only=True, on_failure=_on_webhook_failure
        ))

    threading.Thread(target=_run_async_webhook, daemon=True).start()


class _PendingTask:
    __slots__ = ("task_id", "text", "metadata", "response", "ready", "created_at", "context_id", "_returned")

    def __init__(self, task_id: str, text: str, metadata: dict, context_id: Optional[str] = None):
        self.task_id = task_id
        self.text = text
        self.metadata = metadata
        self.response: Optional[str] = None
        self.ready = Event()
        self.created_at = time.time()
        self.context_id = context_id
        self._returned = False  # True once A2A server has returned to caller


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
        # Oldest pending task created_at — used to skip watchdog scan when queue is young
        self._oldest_pending_time: Optional[float] = None

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
            # Track oldest pending time for watchdog optimization
            if self._oldest_pending_time is None:
                self._oldest_pending_time = task.created_at
            else:
                self._oldest_pending_time = min(self._oldest_pending_time, task.created_at)
            # Only evict tasks that are not currently being processed to avoid race
            evicted = 0
            while len(self._pending) > _TASK_CACHE_MAX:
                for tid, old_task in list(self._pending.items()):
                    if tid not in self._processing:
                        self._pending.pop(tid)
                        old_task.response = "(dropped — queue overflow)"
                        old_task.ready.set()
                        evicted += 1
                        # If evicted task was the oldest, recalculate
                        if old_task.created_at == self._oldest_pending_time and self._oldest_pending_time is not None:
                            self._oldest_pending_time = min((t.created_at for t in self._pending.values()), default=None)
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
                # Only set response if we haven't already returned to the caller.
                # Late completions after timeout are logged but don't overwrite
                # the response already delivered to the client.
                if not task._returned:
                    task.response = response
                    task.ready.set()
                self._completed[task_id] = task
                self._complete_count += 1
                self._states[task_id] = "completed"
                # Update _oldest_pending_time if we removed the oldest task
                if task.created_at == self._oldest_pending_time:
                    self._oldest_pending_time = min((t.created_at for t in self._pending.values()), default=None)
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
                # Update _oldest_pending_time if we removed the oldest task
                if task.created_at == self._oldest_pending_time:
                    self._oldest_pending_time = min((t.created_at for t in self._pending.values()), default=None)
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


def _start_orphaned_task_watchdog(task_queue: TaskQueue) -> threading.Thread:
    """Periodically mark tasks older than 2 * _RESPONSE_TIMEOUT as failed."""
    def run():
        while True:
            time.sleep(_RESPONSE_TIMEOUT)
            cutoff = time.time() - 2 * _RESPONSE_TIMEOUT
            # Skip full scan if queue is too young to have orphans
            if task_queue._oldest_pending_time is not None and task_queue._oldest_pending_time >= cutoff:
                continue
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

def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_status_update_payload(task_id: str, state: str, context_id: Optional[str] = None) -> dict:
    """Build a proto-compliant TaskStatusUpdateEvent payload per a2a.proto:788-800.

    TaskStatusUpdateEvent fields:
      contextId (REQUIRED): context identifier
      taskId (REQUIRED): task identifier
      status (REQUIRED): TaskStatus with state and timestamp
      event (optional): SSE event name
      metadata (optional): additional metadata
    """
    from .hooks import _event_name_for_state as _event_name
    return {
        "taskId": task_id,
        "contextId": context_id or task_id,
        "status": {
            "state": state,
            "timestamp": _utc_now_iso(),
        },
        "event": _event_name(state),
    }


def _build_message_object(text: str, role: int = 2, message_id: Optional[str] = None) -> dict:
    """Build a proto-compliant Message object per a2a.proto:260-277.

    Args:
        text: The message text content.
        role: Role enum value (1=USER, 2=AGENT). Default AGENT since this is server response.
        message_id: Optional message ID. If not provided, a UUID is generated.
    """
    return {
        "message_id": message_id or str(uuid.uuid4()),
        "role": role,
        "parts": [{"text": text}] if text else [],
        "metadata": {},
    }


def _build_task_status(state: str, message_text: Optional[str] = None, timestamp: Optional[str] = None) -> dict:
    """Build a proto-compliant TaskStatus object per a2a.proto:211-219.

    TaskStatus.message is a Message object, not a raw string.
    """
    status = {
        "state": state,
        "timestamp": timestamp or _utc_now_iso(),
    }
    if message_text:
        status["message"] = _build_message_object(message_text, role=2)
    return status


def _build_task_object(task_id: str, state: str, context_id: Optional[str] = None,
                       response: Optional[str] = None, created_at: Optional[float] = None) -> dict:
    """Build a proto-compliant Task object per a2a.proto:163-183.

    Returns a full Task with all fields: id, context_id, status, artifacts.
    """
    ctx_id = context_id or task_id
    filtered = filter_outbound(response) if response else None

    task = {
        "id": task_id,
        "context_id": ctx_id,
        "status": _build_task_status(state, filtered),
    }

    if created_at is not None:
        task["status"]["timestamp"] = datetime.datetime.fromtimestamp(
            created_at, tz=datetime.timezone.utc
        ).isoformat()

    if filtered:
        task["artifacts"] = [
            {
                "artifact_id": str(uuid.uuid4()),
                "parts": [{"text": filtered}],
                "index": 0,
            }
        ]

    return task


def _build_task_list_item(task: "_PendingTask", state: str) -> dict:
    """Build a single task item dict for ListTasks responses.

    Returns a proto-compliant Task object per a2a.proto:163-183.
    """
    filtered = filter_outbound(task.response) if task.response else None
    item = _build_task_object(
        task_id=task.task_id,
        state=state,
        context_id=task.context_id,
        response=filtered,
        created_at=task.created_at,
    )
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
        "task": items,
        "pageSize": page_size,
        "totalSize": len(all_tasks),
        "nextPageToken": next_token if next_token else "",
    }


class A2ARequestHandler(BaseHTTPRequestHandler):
    """Handles A2A HTTP requests."""

    server: "A2AServer"

    def log_message(self, format, *args):
        logger.debug("A2A HTTP: %s", format % args)

    def _rate_limit_client_id(self) -> str:
        """Extract caller identity for rate limiting.

        Uses the configured header (default X-Forwarded-For); falls back
        to the connection peer address.
        """
        header_name = self.server.limiter.config.header_name
        forwarded = self.headers.get(header_name, "")
        if forwarded:
            # X-Forwarded-For may contain a comma-separated chain; use first IP
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _send_json_rate_limited(self, retry_after: int) -> None:
        """Send a 429 Too Many Requests response with Retry-After header."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": "Rate limit exceeded",
                    "data": {"retryAfter": retry_after},
                },
                "id": None,
            },
            ensure_ascii=False,
        ).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(retry_after))
        self.send_header("A2A-Version", A2A_VERSION)
        extensions = _get_a2a_extensions()
        if extensions:
            self.send_header("A2A-Extensions", extensions)
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, A2A-Version, A2A-Extensions")
        self.end_headers()
        self.wfile.write(body)

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
                self._send_rpc_error(-32603, "Unauthorized", 401)
                return False
        else:
            # Read-only: optional auth — if key is provided it must be valid
            if hmac_key and hmac_key != expected_key:
                self._send_rpc_error(-32603, "Unauthorized", 401)
                return False
        return True

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # A2A protocol headers per Section 3.2.6
        self.send_header("A2A-Version", A2A_VERSION)
        extensions = _get_a2a_extensions()
        if extensions:
            self.send_header("A2A-Extensions", extensions)
        # CORS headers on all A2A responses (browser-accessible agents)
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, A2A-Version, A2A-Extensions")
        self.end_headers()
        self.wfile.write(body)


    def _send_rpc_error(self, code: int, message: str, status: int = 400, rpc_id: str | None = None) -> None:
        """Send a JSON-RPC 2.0 error response. Consolidates the repeated
        pattern that was copied across the file."""
        error_body = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
        if rpc_id:
            error_body["id"] = rpc_id
        self._send_json(error_body, status)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        path = self.path.split("?")[0]
        # Advertise only the methods supported by the target endpoint to avoid
        # browser CORS mismatches (Issue 13).
        if path.startswith("/tasks/") and ":subscribe" in path:
            allowed = "GET, OPTIONS"
        elif path == "/message:stream":
            allowed = "POST, OPTIONS"
        else:
            allowed = "POST, OPTIONS"
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
        self.send_header("Access-Control-Allow-Methods", allowed)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, A2A-Version, A2A-Extensions")
        self.send_header("A2A-Version", A2A_VERSION)
        extensions = _get_a2a_extensions()
        if extensions:
            self.send_header("A2A-Extensions", extensions)
        self.end_headers()

    def _check_auth(self) -> bool:
        remote = self.client_address[0]
        # Always bypass auth for localhost — fleet-local subprocess calls
        # (Mode 2/3) may have no Bearer token configured, which is correct.
        if remote in ("127.0.0.1", "::1"):
            if self.server.require_auth:
                logger.warning(
                    "[A2A] Localhost request from %s — rejecting (A2A_REQUIRE_AUTH=true)",
                    remote,
                )
                return False
            logger.warning(
                "[A2A] Allowing localhost request from %s — bypassing auth",
                remote,
            )
            return True
        token = self.server.auth_token
        if not token:
            if self.server.require_auth:
                logger.warning(
                    "[A2A] Rejecting request from %s — A2A_REQUIRE_AUTH=true but no A2A_AUTH_TOKEN set",
                    self.client_address[0],
                )
                return False
            return False
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

        # Immediate initial event (TaskStatusUpdateEvent per a2a.proto:788-800)
        import json
        from .hooks import _event_name_for_state as _event_name
        initial_event_id = str(uuid.uuid4())
        initial = _build_status_update_payload(task_id, current_state)
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
        # 0.5s balances responsiveness (<1s latency) against thread contention.
        # 0.1s would cause 10 wakeups/second per SSE client — too aggressive.
        poll_interval = 0.5  # seconds
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
                # Send final state event and close (TaskStatusUpdateEvent per a2a.proto:788-800)
                term_state = status["state"]
                term_event_id = str(uuid.uuid4())
                term_event = _build_status_update_payload(task_id, term_state)
                if not _send_line(f"id: {term_event_id}\n"):
                    streamer.close_stream(stream_id)
                    return
                if not _send_line(f"event: {_event_name(term_state)}\n"):
                    streamer.close_stream(stream_id)
                    return
                if not _send_line(f"data: {json.dumps(term_event, ensure_ascii=False)}\n\n"):
                    streamer.close_stream(stream_id)
                    return
                streamer.close_stream(stream_id)
                return

            _time.sleep(poll_interval)

        # Timeout — send error and close
        _send_line('event: error\ndata: {"code": -38000, "message": "SSE stream timed out"}\n\n')
        streamer.close_stream(stream_id)

    # GET route table — order IS precedence (first match wins).
    # Each entry: (predicate(path, segments), handler)
    _GET_ROUTES: list[tuple] = []  # initialized in __init__ or class body

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        # Auth guard for task/card endpoints
        if path.startswith(("/tasks", "/extendedAgentCard")):
            if self.server.require_auth and not self._check_auth():
                self._send_rpc_error(-32603, "Unauthorized", 401)
                return

        # Ordered route table — precedence matters.
        # /tasks/{id}:subscribe must fire before /tasks/{id}
        segments = path.split("/")
        routes = [
            # (predicate, handler, name)
            (lambda p, s: p.startswith("/tasks/") and ":subscribe" in p,
             lambda p, s: self._rest_subscribe_to_task(s[2].split(":subscribe")[0]),
             "SubscribeToTask"),
            (lambda p, s: len(s) == 5 and s[1] == "tasks" and s[3] == "pushNotificationConfigs",
             lambda p, s: self._rest_get_push_config(s[2], s[4]),
             "GetTaskPushNotificationConfig"),
            (lambda p, s: len(s) == 4 and s[1] == "tasks" and s[3] == "pushNotificationConfigs",
             lambda p, s: self._rest_list_push_configs(s[2]),
             "ListTaskPushNotificationConfigs"),
            (lambda p, s: p.startswith("/tasks/") and s[1] == "tasks" and len(s) == 3,
             lambda p, s: self._rest_get_task(s[2]),
             "GetTask"),
            (lambda p, s: p == "/extendedAgentCard",
             lambda p, s: self._rest_get_extended_agent_card(),
             "ExtendedAgentCard"),
            (lambda p, s: p == "/tasks",
             lambda p, s: self._rest_list_tasks(),
             "ListTasks"),
        ]
        for pred, handler, _name in routes:
            try:
                if pred(path, segments):
                    handler(path, segments)
                    return
            except Exception:
                logger.exception("GET route handler failed: %s", _name)
                self._send_rpc_error(-32603, "Internal error", 500)
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

        # F-B009: POST /message:stream  (SendStreamingMessage)
        if path == "/message:stream":
            self._do_rest_post(lambda body: self._rest_send_message_stream(body))
            return

        # F-B007: POST /tasks/{id}:cancel  (CancelTask)
        if path.startswith("/tasks/") and ":cancel" in path:
            task_id = path.split("/tasks/")[1].split(":cancel")[0]
            self._do_rest_post(lambda body: self._rest_cancel_task(task_id))
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
        from .a2a_spec.tasks import A2A_ERR_TASK_NOT_FOUND, is_terminal_state, build_error_response

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
        # A2A protocol headers per Section 3.2.6
        self.send_header("A2A-Version", A2A_VERSION)
        extensions = _get_a2a_extensions()
        if extensions:
            self.send_header("A2A-Extensions", extensions)
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, A2A-Version, A2A-Extensions")
        self.end_headers()

        def _send_line(line: str) -> bool:
            """Send one SSE line. Returns False on client disconnect."""
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        # Initial state event (TaskStatusUpdateEvent per a2a.proto:788-800)
        initial_event_id = str(uuid.uuid4())
        initial_payload = json.dumps(_build_status_update_payload(tid, current_state), ensure_ascii=False)
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
        poll_interval = 0.5  # seconds — 5x fewer wakeups than 0.1s, still responsive
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
                term_payload = json.dumps(_build_status_update_payload(tid, term_state), ensure_ascii=False)
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

        # SSRF check before storing
        from .security import validate_webhook_endpoint
        valid, reason = validate_webhook_endpoint(webhook_url)
        if not valid:
            return build_error_response(-38003, f"Invalid webhook endpoint: {reason}", id=rpc_id)

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
        configuration = params.get("configuration", {})

        # Extract SendMessageConfiguration fields per a2a.proto:143-161
        return_immediately = configuration.get("return_immediately", False)
        push_config = configuration.get("task_push_notification_config")
        # NOTE: accepted_output_modes and history_length are accepted per spec
        # but not yet wired into local-task handling. See MED-03/LOW-03 notes
        # in the review (a2a-review-20260602) — the assignments were F841 dead
        # locals, removed.

        if push_config:
            logger.debug("[A2A] SendMessageConfiguration task_push_notification_config not yet implemented for local tasks")

        text_parts = []
        for part in message.get("parts", []):
            if "text" in part:
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            return _build_task_object(
                task_id=task_id,
                state="failed",
                response="Empty message",
            )

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
            return _handle_task_send_mode3(params, metadata, user_text, context_id)

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
        _start_async_webhook_delivery(task_id)

        # Per SendMessageConfiguration.return_immediately (a2a.proto:143-161):
        # If True, return immediately without waiting for task completion.
        if return_immediately:
            task._returned = True
            return _build_task_object(
                task_id=task_id,
                state="working",
                context_id=task.context_id,
                response="(processing — poll with tasks/get)",
            )

        task.ready.wait(timeout=_RESPONSE_TIMEOUT)

        if task.response is None:
            # Timed out without any gateway response — mark as returned so any
            # late gateway completion is ignored (not silently lost to caller).
            task._returned = True
            return _build_task_object(
                task_id=task_id,
                state="working",
                context_id=task.context_id,
                response="(processing — poll with tasks/get)",
            )

        filtered = filter_outbound(task.response)
        audit.log("task_completed", {"task_id": task_id, "response_length": len(filtered)})

        result = _build_task_object(
            task_id=task_id,
            state="completed",
            context_id=task.context_id,
            response=filtered,
        )

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
        except Exception as e:
            logger.warning("[A2A] SSE delivery failed for task %s: %s", task_id, e)

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
                        "artifact_update": {
                            "contextId": ctx_id,
                            "taskId": task_id,
                            "artifact": artifact,
                            "metadata": {"index": artifact.get("index")},
                        }
                    }
                    pusher.deliver_with_retry(sub.url, payload, sub.hmac_key)
        except Exception as e:
            logger.warning("[A2A] Push notification delivery failed for task %s: %s", task_id, e)

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

        allowed, retry_after = self.server.limiter.allow(self._rate_limit_client_id())
        if not allowed:
            self._send_json_rate_limited(retry_after)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_rpc_error(-32600, "Invalid Content-Length", 400)
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
            self._send_rpc_error(-32700, "Parse error", 400)
            return

        audit.log("rest_request", {"path": self.path, "client": self.client_address[0]})
        handler(body)

    def _do_json_rpc(self) -> None:
        """Handle JSON-RPC over POST. Replaces the old do_POST body."""
        import logging as _dbg
        _dbg.getLogger(__name__).info("[A2A DEBUG] _do_json_rpc path=%s client=%s", self.path, self.client_address)
        try:
            self._do_json_rpc_inner()
        except Exception as exc:
            import traceback as _tb
            _dbg.getLogger(__name__).error("[A2A] CRASH in _do_json_rpc: %s\n%s", exc, _tb.format_exc())
            try:
                self._send_json(
                    {"jsonrpc": "2.0", "error": {"code": -32603, "message": f"Internal error: {exc}"}, "id": None},
                    500,
                )
            except Exception:
                pass

    def _do_json_rpc_inner(self) -> None:
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

        allowed, retry_after = self.server.limiter.allow(self._rate_limit_client_id())
        if not allowed:
            audit.log("rate_limited", {"client": self._rate_limit_client_id()})
            self._send_json_rate_limited(retry_after)
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

        if method == "SendMessage":
            result = self._handle_task_send(params, rpc_id)
        elif method == "GetTask":
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
            # Build full proto-compliant Task per a2a.proto:163-183
            result = _build_task_object(
                task_id=tid,
                state=status["state"],
                response=status.get("response"),
            )
            # Emit TaskArtifactUpdateEvent over SSE for GetTask polling responses
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
        elif method == "CancelTask":
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
            logger.debug("worker_cancel for %s: %s", tid, cancel_worker(tid))
            _ensure_task_queue().cancel(tid)
            result = _build_task_object(task_id=tid, state="canceled")
        elif method == "SubscribeToTask":
            self._handle_send_subscribe(params, rpc_id)
            return
        elif method == "ListTasks":
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

        # Wrap task-returning results per proto message definitions:
        # SendMessageResponse, GetTaskResponse, CancelTaskResponse all use Task task = 1
        if method in ("SendMessage", "GetTask", "CancelTask"):
            rpc_result = {"task": result}
        elif method == "ListTasks":
            # ListTasksResponse: repeated Task task + pagination metadata at top level
            rpc_result = {
                "task": result.get("task", []),
                "pageSize": result.get("pageSize"),
                "totalSize": result.get("totalSize"),
                "nextPageToken": result.get("nextPageToken"),
            }
        else:
            rpc_result = result
        self._send_json({"jsonrpc": "2.0", "result": rpc_result, "id": rpc_id})

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
        # Build full proto-compliant Task per a2a.proto:163-183
        result = _build_task_object(
            task_id=task_id,
            state=status["state"],
            response=status.get("response"),
        )
        self._send_json({"task": result})

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
        self._send_json({"task": result.get("task", []), "pageSize": result.get("pageSize"), "totalSize": result.get("totalSize"), "nextPageToken": result.get("nextPageToken")})

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
        logger.debug("worker_cancel for %s: %s", task_id, cancel_worker(task_id))
        _ensure_task_queue().cancel(task_id)
        # Build full proto-compliant Task per a2a.proto:163-183
        result = _build_task_object(task_id=task_id, state="canceled")
        self._send_json({"task": result})

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
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
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
        initial_payload = json.dumps(_build_status_update_payload(task_id, current_state), ensure_ascii=False)
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

        poll_interval = 0.5  # seconds — 5x fewer wakeups than 0.1s, still responsive
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
                term_payload = json.dumps(_build_status_update_payload(task_id, term_state), ensure_ascii=False)
                if not _send_line(f"event: {_event_name(term_state)}\n"):
                    streamer.close_stream(stream_id)
                    return
                if not _send_line(f"data: {term_payload}\n\n"):
                    streamer.close_stream(stream_id)
                    return
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
            if "text" in part:
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            self._send_json({
                "id": str(uuid.uuid4()),
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"text": "Empty message"}], "index": 0}],
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

        _start_async_webhook_delivery(task_id)

        task.ready.wait(timeout=_RESPONSE_TIMEOUT)

        if task.response is None:
            result = {
                "id": task_id,
                "status": {"state": "working"},
                "artifacts": [{"parts": [{"text": "(processing — poll with tasks/get)"}], "index": 0}],
            }
            self._send_json(result, 200)
            return

        filtered = filter_outbound(task.response)
        audit.log("task_completed", {"task_id": task_id, "response_length": len(filtered)})

        result = {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"text": filtered}], "index": 0}],
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
            if "text" in part:
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            self._send_json({
                "id": str(uuid.uuid4()),
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"text": "Empty message"}], "index": 0}],
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

        _start_async_webhook_delivery(task_id)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.close_connection = True
        self.send_header("X-Stream-Id", stream_id)
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
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

        # Initial working event (TaskStatusUpdateEvent per a2a.proto:788-800)
        initial_event_id = str(uuid.uuid4())
        initial_payload = json.dumps(_build_status_update_payload(task_id, "working", context_id), ensure_ascii=False)
        if not _send_line(f"id: {initial_event_id}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"event: {_event_name('working')}\n"):
            streamer.close_stream(stream_id)
            return
        if not _send_line(f"data: {initial_payload}\n\n"):
            streamer.close_stream(stream_id)
            return

        poll_interval = 0.5  # seconds — 5x fewer wakeups than 0.1s, still responsive
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
                term_payload = json.dumps(_build_status_update_payload(task_id, state, context_id), ensure_ascii=False)
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

        # Build AuthenticationInfo from request body — use spec field names
        auth_info = None
        raw_auth = body.get("authentication") or body.get("auth", {})
        if raw_auth:
            auth_info = AuthenticationInfo(
                scheme=raw_auth.get("scheme") or raw_auth.get("authType") or raw_auth.get("auth_type"),
                credentials=raw_auth.get("credentials") or raw_auth.get("authCode") or raw_auth.get("auth_code"),
            )

        # Parse into CreateTaskPushNotificationConfigRequest (spec-compliant: id, task_id, url)
        req = CreateTaskPushNotificationConfigRequest(
            id=body.get("id", ""),
            task_id=task_id,
            url=url,
            authentication=auth_info,
            metadata=body.get("metadata"),
        )

        # Delegate to push_delivery CRUD (spec-compliant: task_id, url)
        cfg = create_push_config(
            task_id=req.task_id,
            url=req.url,
            authentication=req.authentication,
            metadata=req.metadata,
        )

        response = CreateTaskPushNotificationConfigResponse(config=cfg)
        self._send_json({
            "configId": response.config.id,
            "config": {
                "id": response.config.id,
                "taskId": response.config.task_id,
                "url": response.config.url,
                "authentication": {
                    "scheme": response.config.authentication.scheme if response.config.authentication else None,
                    "credentials": response.config.authentication.credentials if response.config.authentication else None,
                } if response.config.authentication else None,
                "metadata": response.config.metadata,
            },
        }, 201)

    def _rest_get_push_config(self, task_id: str, config_id: str) -> None:
        """F-B010: GET /tasks/{id}/pushNotificationConfigs/{config_id} — get a push config.

        Uses GetTaskPushNotificationConfigRequest + get_push_config from push_delivery.
        X-HMAC-Key optional; if provided must be valid.
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
                "url": response.config.url,
                "authentication": {
                    "scheme": response.config.authentication.scheme if response.config.authentication else None,
                    "credentials": response.config.authentication.credentials if response.config.authentication else None,
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
                    "url": c.url,
                    "authentication": {
                        "scheme": c.authentication.scheme if c.authentication else None,
                        "credentials": c.authentication.credentials if c.authentication else None,
                    } if c.authentication else None,
                    "metadata": c.metadata,
                }
                for c in response.items
            ],
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

        # DELETE returns 204 with empty body per spec (google.protobuf.Empty)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
        self.end_headers()

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
                    "pushNotifications": self.server._push_notifications_capability(),
                    "stateTransitionHistory": False,
                },
            }
        )
        self._send_json(card)


def _load_rate_limit_config_from_env() -> RateLimitConfig:
    """Build RateLimitConfig from environment variables (fallback when no config.yaml).

    Uses the same defaults as RateLimitConfig; env vars override them.
    """
    enabled = os.getenv("A2A_RATE_LIMIT_ENABLED", "").lower() in ("1", "true", "yes")
    requests_per_window = int(os.getenv("A2A_RATE_LIMIT_REQUESTS", "100"))
    window_seconds = int(os.getenv("A2A_RATE_LIMIT_WINDOW", "60"))
    burst_multiplier = float(os.getenv("A2A_RATE_LIMIT_BURST", "2.0"))
    header_name = os.getenv("A2A_RATE_LIMIT_HEADER", "X-Forwarded-For")
    cleanup_interval = int(os.getenv("A2A_RATE_LIMIT_CLEANUP", "300"))
    max_entries = int(os.getenv("A2A_RATE_LIMIT_MAX_ENTRIES", "10000"))

    return RateLimitConfig(
        enabled=enabled,
        requests_per_window=requests_per_window,
        window_seconds=window_seconds,
        burst_multiplier=burst_multiplier,
        header_name=header_name,
        cleanup_interval_seconds=cleanup_interval,
        max_entries=max_entries,
    )


class A2AServer(ThreadingHTTPServer):
    """Threaded HTTP server with A2A configuration.

    Each request runs in its own thread so tasks/send can block waiting
    for agent response without starving health checks and agent card requests.
    """

    daemon_threads = True

    def __init__(self, host: str, port: int, hmac_key: Optional[str] = None,
                 rate_limit_config: Optional[RateLimitConfig] = None):
        self.agent_name = os.getenv("A2A_AGENT_NAME", "hermes-agent")
        self.agent_description = os.getenv("A2A_AGENT_DESCRIPTION", "A self-improving AI agent powered by Hermes")
        self.auth_token = os.getenv("A2A_AUTH_TOKEN", "")
        self.require_auth = os.getenv("A2A_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
        self.hmac_key = hmac_key or os.environ.get("A2A_HMAC_KEY")
        # CORS origins — default to * for backward compat; override via A2A_CORS_ORIGINS
        self.cors_origins = os.getenv("A2A_CORS_ORIGINS", "*")
        if not self.auth_token:
            logger.warning(
                "[A2A] No A2A_AUTH_TOKEN set — only localhost requests will be accepted, "
                "and localhost is not safe in containers. Set A2A_REQUIRE_AUTH=true to reject all unauthenticated requests."
            )
        # Build RateLimitConfig from config.yaml (L6→L10 bridge);
        # fall back to env-driven defaults if no dataclass supplied.
        if rate_limit_config is None:
            rate_limit_config = _load_rate_limit_config_from_env()
        self.limiter = RateLimiter(rate_limit_config)
        self.limiter.start_cleanup()
        _start_orphaned_task_watchdog(_ensure_task_queue())
        super().__init__((host, port), A2ARequestHandler)

    def _push_notifications_capability(self) -> bool | dict:
        """Return pushNotifications capability per A2A spec.

        Per the A2A spec, when a webhook URL is configured, pushNotifications
        must be an object with a webhookUrl field. When not configured, it
        should be the boolean True to indicate the capability is available
        but not yet configured.
        """
        webhook_host = os.getenv("A2A_WEBHOOK_HOST", "")
        webhook_port = int(os.getenv("WEBHOOK_PORT", "8644"))
        # If A2A_WEBHOOK_HOST is set and passes SSRF validation, use object form
        if webhook_host:
            try:
                from .security import validate_host
                validate_host(webhook_host)
                webhook_url = f"http://{webhook_host}:{webhook_port}/webhooks/a2a_trigger"
                return {"webhookUrl": webhook_url}
            except ValueError:
                pass
        return True

    def build_agent_card(self) -> dict:
        public_url = os.getenv("A2A_PUBLIC_URL", "").rstrip("/")
        if not public_url:
            host, port = self.server_address
            public_url = f"http://{host}:{port}"
        push_notif = self._push_notifications_capability()
        return {
            "name": self.agent_name,
            "agentId": self.agent_name,
            "description": self.agent_description,
            "url": public_url,
            "version": HERMES_VERSION,
            "protocol": "a2a",
            "protocolVersion": "1.0",
            "provider": {"url": public_url, "organization": "Hermes Fleet"},
            "capabilities": {
                "streaming": True,
                "push_notifications": push_notif,
                "extensions": False,
                "extended_agent_card": True,
            },
            "skills": [
                {
                    "id": "general",
                    "name": "General Assistant",
                }
            ],
            "supported_interfaces": [
                {
                    "url": public_url,
                    "protocol_binding": "http://a2a-protocol.hermes.ai",
                    "protocol_version": "1.0",
                }
            ],
            "default_input_modes": ["text"],
            "default_output_modes": ["text"],
            "security_schemes": [{"type": "bearer", "bearer_format": "JWT"}] if self.auth_token else [],
            "security_requirements": [{"bearer": []}] if self.auth_token else [],
        }
