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
    __slots__ = ("task_id", "text", "metadata", "response", "ready", "created_at")

    def __init__(self, task_id: str, text: str, metadata: dict):
        self.task_id = task_id
        self.text = text
        self.metadata = metadata
        self.response: Optional[str] = None
        self.ready = Event()
        self.created_at = time.time()


class TaskQueue:
    """Thread-safe queue for pending A2A tasks."""

    def __init__(self):
        self._pending: OrderedDict[str, _PendingTask] = OrderedDict()
        self._completed: OrderedDict[str, _PendingTask] = OrderedDict()
        self._processing: set[str] = set()
        self._lock = Lock()
        # Atomic counters — eliminate re-entrancy risk in _get_queue_depth
        self._enqueue_count = 0
        self._complete_count = 0
        self._cancel_count = 0

    def pending_count(self) -> int:
        # Counter-based — no re-entrancy risk, no traversal of singleton
        with self._lock:
            return max(0, self._enqueue_count - self._complete_count - self._cancel_count)

    def enqueue(self, task_id: str, text: str, metadata: dict) -> _PendingTask | None:
        task = _PendingTask(task_id, text, metadata)
        with self._lock:
            if len(self._pending) >= _MAX_PENDING:
                return None
            if task_id in self._pending:
                return None
            self._pending[task_id] = task
            # Only evict tasks that are not currently being processed to avoid race
            while len(self._pending) > _TASK_CACHE_MAX:
                for tid, old_task in list(self._pending.items()):
                    if tid not in self._processing:
                        self._pending.pop(tid)
                        old_task.response = "(dropped — queue overflow)"
                        old_task.ready.set()
                        break
                else:
                    # All pending tasks are being processed, stop evicting
                    break
            # Increment counter after successful enqueue (after eviction check)
            self._enqueue_count += 1
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
        # Record task canceled metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_canceled()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_canceled): %s", exc)

    def get_status(self, task_id: str) -> dict:
        with self._lock:
            if task_id in self._pending:
                return {"state": "working"}
            task = self._completed.get(task_id)
            if task:
                if task.response == "(canceled)":
                    return {"state": "canceled"}
                return {"state": "completed", "response": filter_outbound(task.response)}
        return {"state": "unknown"}

    def get_task_metadata(self, task_id: str) -> dict:
        """Get metadata for a task by ID (public API for hooks)."""
        with self._lock:
            if task_id in self._pending:
                return getattr(self._pending[task_id], "metadata", {})
            if task_id in self._completed:
                return getattr(self._completed[task_id], "metadata", {})
        return {}

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
            if task_id in self._pending:
                return self._pending[task_id]
            if task_id in self._completed:
                return self._completed[task_id]
        return None


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


class A2ARequestHandler(BaseHTTPRequestHandler):
    """Handles A2A HTTP requests."""

    server: "A2AServer"

    def log_message(self, format, *args):
        logger.debug("A2A HTTP: %s", format % args)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def do_GET(self) -> None:
        if self.path == "/.well-known/agent.json":
            self._send_json(self.server.build_agent_card())
        elif self.path == "/health":
            self._send_json({
                "status": "ok",
                "agent": self.server.agent_name,
                "version": HERMES_VERSION,
            })
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if not self._check_auth():
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Unauthorized"}, "id": None},
                401,
            )
            return

        if not self.server.limiter.allow(self.client_address[0]):
            audit.log("rate_limited", {"client": self.client_address[0]})
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Rate limit exceeded"}, "id": None},
                429,
            )
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Content-Length"}, "id": None},
                400,
            )
            return

        if length <= 0 or length > 65536:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32600, "message": f"Content-Length must be 1-65536, got {length}"}, "id": None},
                413 if length > 65536 else 400,
            )
            return

        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
                400,
            )
            return

        method = body.get("method", "")
        params = body.get("params", {})
        rpc_id = body.get("id")

        audit.log("rpc_request", {"method": method, "client": self.client_address[0]})

        if method == "tasks/send":
            result = self._handle_task_send(params)
        elif method == "tasks/get":
            tid = params.get("id", "")
            status = _ensure_task_queue().get_status(tid)
            result = {"id": tid, "status": {"state": status["state"]}}
            if status.get("response"):
                result["artifacts"] = [{"parts": [{"type": "text", "text": filter_outbound(status["response"])}], "index": 0}]
        elif method == "tasks/cancel":
            tid = params.get("id", "")
            from .worker_registry import cancel_worker
            worker_canceled = cancel_worker(tid)
            _ensure_task_queue().cancel(tid)
            result = {"id": tid, "status": {"state": "canceled"}, "metadata": {"hermes": {"worker_canceled": worker_canceled}}}
        else:
            self._send_json({
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Method not found: {method}"},
                "id": rpc_id,
            })
            return

        self._send_json({"jsonrpc": "2.0", "result": result, "id": rpc_id})

    def _handle_task_send(self, params: dict) -> dict:
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
        if q.find_task_by_id(task_id) is not None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Task ID already in use"}, "id": rpc_id},
                409,
            )
            return

        task = q.enqueue(task_id, user_text, metadata)
        if task is None:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Agent busy — too many pending tasks"}, "id": rpc_id},
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

        return {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": filtered}], "index": 0}],
        }


class A2AServer(ThreadingHTTPServer):
    """Threaded HTTP server with A2A configuration.

    Each request runs in its own thread so tasks/send can block waiting
    for agent response without starving health checks and agent card requests.
    """

    daemon_threads = True

    def __init__(self, host: str, port: int):
        self.agent_name = os.getenv("A2A_AGENT_NAME", "hermes-agent")
        self.agent_description = os.getenv("A2A_AGENT_DESCRIPTION", "A self-improving AI agent powered by Hermes")
        self.auth_token = os.getenv("A2A_AUTH_TOKEN", "")
        self.require_auth = os.getenv("A2A_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
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
            "description": self.agent_description,
            "url": public_url,
            "version": HERMES_VERSION,
            "protocol": "a2a",
            "protocolVersion": "0.2.0",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "multiTurn": False,
                "structuredMetadata": True,
            },
            "skills": [
                {
                    "id": "general",
                    "name": "General Assistant",
                    "description": "General-purpose AI assistant with tool use, web search, and more",
                }
            ],
            "authentication": {
                "schemes": ["bearer"] if self.auth_token else [],
            },
        }
