"""A2A HTTP server — runs in a background thread, no asyncio.

Handles inbound A2A JSON-RPC requests. Messages are queued and picked up
by the pre_llm_call hook; responses are captured by post_llm_call and
returned to the caller.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional

from .security import audit, filter_outbound, sanitize_inbound
from .rate_limiter import RateLimiter, RateLimitConfig
from .a2a_spec.tasks import (
    A2A_ERR_TASK_NOT_CANCELABLE,
    A2A_ERR_TASK_NOT_FOUND,
    build_error_response,
    is_terminal_state,
)
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
# Task queue + pending-task model (re-exported for backward-compatible imports).
from .task_queue import _MAX_PENDING, _PendingTask, _TASK_CACHE_MAX, TaskQueue
# A2A wire-format builders (re-exported for backward-compatible imports).
from .payloads import (
    _build_message_object,
    _build_status_update_payload,
    _build_task_list_item,
    _build_task_object,
    _build_task_status,
    _utc_now_iso,
)


logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
_RESPONSE_TIMEOUT = int(os.getenv("A2A_RESPONSE_TIMEOUT", "120"))  # seconds to wait for agent response

# Sentinel returned by JSON-RPC method handlers that have already written the
# full HTTP response themselves (streaming or error paths).
_RPC_HANDLED = object()

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

def _build_paginated_task_list(page_size: int = 20, continuation_token: Optional[str] = None) -> dict:
    """Build a paginated task list from the task queue.

    Returns the final ListTasks response shape directly:
      task: list of Task objects for the page
      pageSize: requested page size
      totalSize: total number of tasks
      nextPageToken: base64-encoded offset for the next page ("" when none)
    """
    import base64

    # Consistent snapshot under the queue lock — no direct internals access.
    all_tasks = _ensure_task_queue().snapshot()
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

    items = [_build_task_list_item(task, state) for _created_at, task, state in page_tasks]

    return {
        "task": items,
        "pageSize": page_size,
        "totalSize": len(all_tasks),
        "nextPageToken": next_token if next_token else "",
    }


def _extract_text_parts(message: dict) -> str:
    """Join the text of every part in an A2A message that carries a text field.

    Preserves the exact behaviour previously inlined across _handle_task_send,
    _rest_send_message, and _rest_send_message_stream.
    """
    text_parts = []
    for part in message.get("parts", []):
        if "text" in part:
            text_parts.append(part.get("text", ""))
    return "\n".join(text_parts)


def _resolve_sender_name(metadata: dict, source: dict, client_ip: str) -> str:
    """Populate and sanitize metadata['sender_name'] in place; return it.

    `source` is the JSON-RPC params or REST body dict that may carry a
    `from`/`sender.name` field. Consolidates the sender-name derivation +
    sanitization block previously duplicated across the send handlers.
    """
    if "sender_name" not in metadata:
        from_field = source.get("from") or source.get("sender", {}).get("name")
        metadata["sender_name"] = (from_field or metadata.get("agent_name")
                                   or f"agent-{client_ip}")
    raw_name = metadata.get("sender_name", "") or ""
    metadata["sender_name"] = "".join(c for c in raw_name if c.isalnum() or c in "-_.@ ")[:64]
    return metadata["sender_name"]


def _serialize_push_config(cfg) -> dict:
    """Render a TaskPushNotificationConfig as the wire dict used by REST handlers."""
    return {
        "id": cfg.id,
        "taskId": cfg.task_id,
        "url": cfg.url,
        "authentication": {
            "scheme": cfg.authentication.scheme if cfg.authentication else None,
            "credentials": cfg.authentication.credentials if cfg.authentication else None,
        } if cfg.authentication else None,
        "metadata": cfg.metadata,
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


    def _send_rpc_error(self, code: int, message: str, status: int = 400,
                        rpc_id: str | None = None, data=None) -> None:
        """Send a JSON-RPC 2.0 error response — the single error emitter for both
        the JSON-RPC and REST paths. Always emits the proto-shaped ``data`` and
        ``id`` fields (null when unset) so every error envelope is identical on
        the wire.
        """
        self._send_json(
            {"jsonrpc": "2.0", "error": {"code": code, "message": message, "data": data}, "id": rpc_id},
            status,
        )

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
        # segments = ['', 'tasks', '{id}', 'pushNotificationConfigs', '{config_id}']
        segments = path.split("/")
        if (len(segments) == 5 and segments[1] == "tasks"
                and segments[3] == "pushNotificationConfigs"):
            self._rest_delete_push_config(segments[2], segments[4])
            return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        segments = path.split("/")

        # Ordered REST route table. Every handler dispatches through
        # _do_rest_post (auth + rate-limit + body parse); unmatched paths fall
        # through to JSON-RPC. Precedence matters: :cancel before push-config.
        routes = [
            # F-B005: POST /message:send  (SendMessage)
            (lambda p, s: p == "/message:send",
             lambda p, s: self._do_rest_post(self._rest_send_message)),
            # F-B009: POST /message:stream  (SendStreamingMessage)
            (lambda p, s: p == "/message:stream",
             lambda p, s: self._do_rest_post(self._rest_send_message_stream)),
            # F-B007: POST /tasks/{id}:cancel  (CancelTask)
            (lambda p, s: p.startswith("/tasks/") and ":cancel" in p,
             lambda p, s: self._do_rest_post(
                 lambda body: self._rest_cancel_task(p.split("/tasks/")[1].split(":cancel")[0]))),
            # F-B010: POST /tasks/{id}/pushNotificationConfigs  (CreateTaskPushNotificationConfig)
            (lambda p, s: len(s) == 4 and s[1] == "tasks" and s[3] == "pushNotificationConfigs",
             lambda p, s: self._do_rest_post(lambda body: self._rest_create_push_config(s[2], body))),
        ]
        for pred, handler in routes:
            if pred(path, segments):
                handler(path, segments)
                return

        # Fall through to JSON-RPC handling
        self._do_json_rpc()

    def _handle_send_subscribe(self, params: dict, rpc_id) -> None:
        """Handle tasks/sendSubscribe — SSE stream via _stream_task_sse."""
        tid = params.get("taskId", "")
        if not tid:
            self._send_json({"jsonrpc": "2.0", "id": rpc_id,
                             "error": build_error_response(-32600, "Invalid Request: taskId is required", id=rpc_id)})
            return

        status = _ensure_task_queue().get_status(tid)
        if status["state"] == "unknown":
            self._send_json({"jsonrpc": "2.0", "id": rpc_id,
                             "error": build_error_response(A2A_ERR_TASK_NOT_FOUND, f"Task not found: {tid}", id=rpc_id)})
            return

        extra_headers = {"A2A-Version": A2A_VERSION}
        extensions = _get_a2a_extensions()
        if extensions:
            extra_headers["A2A-Extensions"] = extensions
        self._stream_task_sse(tid, extra_headers=extra_headers, http_method="POST")

    def _handle_push_subscribe(self, params: dict, rpc_id) -> dict:
        """Handle push notification subscription: tasks/pushNotification/subscribe."""
        from .subscription_store import get_subscription_store

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

        return_immediately = configuration.get("return_immediately", False)
        push_config = configuration.get("task_push_notification_config")
        if push_config:
            logger.debug("[A2A] SendMessageConfiguration task_push_notification_config not yet implemented for local tasks")

        user_text = _extract_text_parts(message)

        if not user_text.strip():
            return _build_task_object(task_id=task_id, state="failed", response="Empty message")

        metadata = message.get("metadata", {})
        context_id = params.get("contextId") or metadata.get("context_id") or task_id
        hermes_meta = metadata.get("hermes", {}) if isinstance(metadata.get("hermes", {}), dict) else {}
        worker_at = metadata.get("worker_at", "")

        if worker_at == "target" or hermes_meta.get("execution") == "remote_subprocess":
            from .tool_handlers import _handle_task_send_mode3
            return _handle_task_send_mode3(params, metadata, user_text, context_id)

        if return_immediately:
            context_id_val = context_id or task_id
            return _build_task_object(task_id=task_id, state="working",
                                      context_id=context_id_val,
                                      response="(processing — poll with tasks/get)")

        _resolve_sender_name(metadata, params, self.client_address[0])

        idem_key = params.get("idempotencyKey")
        result = self._enqueue_and_await_task(task_id, user_text, metadata,
                                               context_id, idem_key, params, rpc_id)
        if result == "CONFLICT" or result == "BUSY":
            return  # error already sent by _enqueue_and_await_task
        if result is None:
            return _build_task_object(task_id=task_id, state="working",
                                      context_id=context_id,
                                      response="(processing — poll with tasks/get)")

        # SSE + push artifact delivery (JSON-RPC only feature)
        ctx_id = context_id or task_id
        try:
            from .sse_handler import emit_artifact_event, get_sse_streamer
            streamer = get_sse_streamer()
            stream_ids = streamer.get_stream_ids_for_task(task_id)
            if stream_ids:
                for artifact in result.get("artifacts", []):
                    evt = emit_artifact_event(task_id=task_id, context_id=ctx_id,
                                              artifact=artifact,
                                              metadata={"index": artifact.get("index")})
                    for sid in stream_ids:
                        streamer.push_event(sid, evt)
        except Exception as e:
            logger.warning("[A2A] SSE delivery failed for task %s: %s", task_id, e)
        try:
            from .subscription_store import get_subscription_store
            from .push_delivery import get_push_delivery
            store = get_subscription_store()
            pusher = get_push_delivery()
            subs = store.get(task_id)
            for sub in subs:
                for artifact in result.get("artifacts", []):
                    payload = {"artifact_update": {"contextId": ctx_id, "taskId": task_id,
                                                   "artifact": artifact,
                                                   "metadata": {"index": artifact.get("index")}}}
                    pusher.deliver_with_retry(sub.url, payload, sub.hmac_key)
        except Exception as e:
            logger.warning("[A2A] Push notification delivery failed for task %s: %s", task_id, e)

        return result


    def _prepare_and_enqueue(self, task_id: str, user_text: str, metadata: dict,
                             context_id: str, idem_key: str | None,
                             idem_payload: dict):
        """Shared inbound preamble: sanitize, mark origin, enforce idempotency
        and task-ID collision guards, then enqueue. Used by all three inbound
        paths (_handle_task_send, _rest_send_message, _rest_send_message_stream)
        so every transport gets the same guards.

        Returns the enqueued ``_PendingTask`` on success, or a sentinel tuple the
        caller translates into its transport-specific response:

          ("CONFLICT", code, http_status, message) — idem conflict or id collision
          ("BUSY",     code, http_status, message) — queue is full
          ("REPLAY",   cached_result)              — idempotent replay; reuse result

        This method never writes a response itself, so callers are free to render
        a JSON-RPC error, a REST JSON error, or an SSE error event as appropriate.
        """
        user_text = sanitize_inbound(user_text)
        metadata["_a2a_origin"] = "peer"

        audit.log("task_received", {"task_id": task_id, "length": len(user_text)})

        q = _ensure_task_queue()

        # --- Idempotency check first (before task_id collision check) ---
        from .persistence import get_idempotency_store
        idem_store = get_idempotency_store()
        if idem_key:
            conflict, existing_task_id = idem_store.check_conflict(idem_key, idem_payload)
            if conflict:
                return (
                    "CONFLICT", -38004, 409,
                    f"Non-idempotent task: idempotency key '{idem_key}' already used with a different payload",
                )
            cached = idem_store.get(idem_key)
            if cached is not None:
                cached_task_id, cached_result = cached
                logger.debug("[A2A] Idempotency replay for key=%s → task_id=%s", idem_key, cached_task_id)
                return ("REPLAY", cached_result)

        # Task-ID collision check
        if q.find_task_by_id(task_id) is not None:
            return ("CONFLICT", -38004, 409, "Task ID already in use")

        task = q.enqueue(task_id, user_text, metadata, context_id=context_id)
        if task is None:
            return ("BUSY", -32603, 503, "Agent busy — too many pending tasks")

        return task

    def _enqueue_and_await_task(self, task_id: str, user_text: str, metadata: dict,
                                 context_id: str, idem_key: str | None,
                                 idem_payload: dict, rpc_id=None) -> dict | None:
        """Synchronous core: prepare + enqueue, wait for the worker, filter the
        result. Used by _handle_task_send (JSON-RPC) and _rest_send_message (REST).

        Returns the filtered result dict on success, the cached result on an
        idempotent replay, or None if the task timed out. On CONFLICT/BUSY it
        sends the JSON-RPC error itself and returns the matching sentinel string.
        """
        outcome = self._prepare_and_enqueue(
            task_id, user_text, metadata, context_id, idem_key, idem_payload
        )
        if isinstance(outcome, tuple):
            kind = outcome[0]
            if kind == "REPLAY":
                return outcome[1]  # cached result dict
            _, code, http_status, message = outcome
            self._send_rpc_error(code, message, http_status, rpc_id)
            return kind  # "CONFLICT" or "BUSY"

        task = outcome
        _start_async_webhook_delivery(task_id)

        task.ready.wait(timeout=_RESPONSE_TIMEOUT)

        if task.response is None:
            task._returned = True
            return None  # timeout

        filtered = filter_outbound(task.response)
        audit.log("task_completed", {"task_id": task_id, "response_length": len(filtered)})

        result = _build_task_object(
            task_id=task_id,
            state="completed",
            context_id=task.context_id,
            response=filtered,
        )

        if idem_key:
            from .persistence import get_idempotency_store
            get_idempotency_store().set(idem_key, task_id, idem_payload, result)

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
        logger.debug("_do_json_rpc path=%s client=%s", self.path, self.client_address)
        try:
            self._do_json_rpc_inner()
        except Exception as exc:
            import traceback as _tb
            logger.error("[A2A] CRASH in _do_json_rpc: %s\n%s", exc, _tb.format_exc())
            try:
                self._send_rpc_error(-32603, f"Internal error: {exc}", 500)
            except Exception:
                pass

    def _do_json_rpc_inner(self) -> None:
        if not self._check_auth():
            self._send_rpc_error(-32603, "Unauthorized", 401)
            return

        allowed, retry_after = self.server.limiter.allow(self._rate_limit_client_id())
        if not allowed:
            audit.log("rate_limited", {"client": self._rate_limit_client_id()})
            self._send_json_rate_limited(retry_after)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_rpc_error(-32600, "Invalid Content-Length", 400)
            return

        if length <= 0 or length > 65536:
            self._send_rpc_error(
                -32600, f"Content-Length must be 1-65536, got {length}",
                413 if length > 65536 else 400,
            )
            return

        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_rpc_error(-32700, "Parse error", 400)
            return

        method = body.get("method", "")
        params = body.get("params", {})
        rpc_id = body.get("id")

        # Reject requests without the jsonrpc field
        if body.get("jsonrpc") != "2.0":
            self._send_rpc_error(-32600, "Invalid Request: missing or invalid jsonrpc field", 400, rpc_id)
            return

        audit.log("rpc_request", {"method": method, "client": self.client_address[0]})

        if not method:
            self._send_rpc_error(-32600, "Invalid Request: missing method", 400, rpc_id)
            return

        handler = self._RPC_METHODS.get(method)
        if handler is None:
            self._send_rpc_error(-32601, f"Method not found: {method}", 404, rpc_id)
            return

        rpc_result = handler(self, params, rpc_id)
        # Handlers that stream or send their own error response return _RPC_HANDLED.
        if rpc_result is _RPC_HANDLED:
            return
        self._send_json({"jsonrpc": "2.0", "result": rpc_result, "id": rpc_id})

    # -------------------------------------------------------------------------
    # Shared task lookup + get/cancel cores (used by both JSON-RPC and REST)
    # -------------------------------------------------------------------------

    def _load_task_or_404(self, task_id: str, rpc_id=None) -> Optional[dict]:
        """Return the task status dict, or emit a 404 task-not-found error and
        return None. Single source of the guard previously duplicated across the
        get/cancel handlers and every push-config endpoint."""
        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_rpc_error(A2A_ERR_TASK_NOT_FOUND, f"Task not found: {task_id}", 404, rpc_id)
            return None
        return status

    def _get_task_core(self, task_id: str, rpc_id=None) -> Optional[dict]:
        """Build the proto-compliant Task object for a GetTask, or 404."""
        status = self._load_task_or_404(task_id, rpc_id)
        if status is None:
            return None
        return _build_task_object(
            task_id=task_id, state=status["state"], response=status.get("response")
        )

    def _cancel_task_core(self, task_id: str, rpc_id=None) -> Optional[dict]:
        """Cancel a task. Emits 404/409 and returns None on failure; returns the
        canceled Task object on success."""
        from .worker_registry import cancel_worker
        status = self._load_task_or_404(task_id, rpc_id)
        if status is None:
            return None
        if status["state"] in ("completed", "failed", "canceled"):
            self._send_rpc_error(
                A2A_ERR_TASK_NOT_CANCELABLE,
                f"Task not cancelable: task is {status['state']}", 409, rpc_id,
            )
            return None
        logger.debug("worker_cancel for %s: %s", task_id, cancel_worker(task_id))
        _ensure_task_queue().cancel(task_id)
        return _build_task_object(task_id=task_id, state="canceled")

    def _emit_get_task_artifacts(self, task_id: str, result: dict) -> None:
        """Best-effort: emit TaskArtifactUpdateEvent over any open SSE streams for
        a GetTask poll response. JSON-RPC-only behaviour."""
        try:
            from .sse_handler import emit_artifact_event, get_sse_streamer
            streamer = get_sse_streamer()
            stream_ids = streamer.get_stream_ids_for_task(task_id)
            if not stream_ids:
                return
            pending_task = _ensure_task_queue().find_task_by_id(task_id)
            ctx_id = pending_task.context_id if pending_task else task_id
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
            pass  # SSE delivery is best-effort

    # -------------------------------------------------------------------------
    # JSON-RPC method handlers — each returns the `result` payload to wrap, or
    # _RPC_HANDLED when it has already written the full response (stream/error).
    # -------------------------------------------------------------------------

    def _rpc_send_message(self, params: dict, rpc_id):
        # _handle_task_send returns None only when it already sent an error
        # (CONFLICT/BUSY); the {"task": None} wrap preserves prior behaviour.
        result = self._handle_task_send(params, rpc_id)
        return {"task": result}

    def _rpc_get_task(self, params: dict, rpc_id):
        tid = params.get("id", "")
        result = self._get_task_core(tid, rpc_id)
        if result is None:
            return _RPC_HANDLED
        self._emit_get_task_artifacts(tid, result)
        return {"task": result}

    def _rpc_cancel_task(self, params: dict, rpc_id):
        result = self._cancel_task_core(params.get("id", ""), rpc_id)
        if result is None:
            return _RPC_HANDLED
        return {"task": result}

    def _rpc_subscribe_to_task(self, params: dict, rpc_id):
        self._handle_send_subscribe(params, rpc_id)
        return _RPC_HANDLED

    def _rpc_list_tasks(self, params: dict, rpc_id):
        page_size = min(max(int(params.get("pageSize", 20)), 1), 100)
        token = params.get("continuationToken")
        # _build_paginated_task_list already returns the ListTasksResponse shape.
        return _build_paginated_task_list(page_size, token)

    # JSON-RPC method → handler dispatch table (order-independent).
    _RPC_METHODS = {
        "SendMessage": _rpc_send_message,
        "GetTask": _rpc_get_task,
        "CancelTask": _rpc_cancel_task,
        "SubscribeToTask": _rpc_subscribe_to_task,
        "ListTasks": _rpc_list_tasks,
    }

    # -------------------------------------------------------------------------
    # REST handlers (F-B001, F-B005–F-B011)
    # -------------------------------------------------------------------------

    def _rest_get_task(self, task_id: str) -> None:
        """F-B001: GET /tasks/{id} — return Task object."""
        result = self._get_task_core(task_id)
        if result is None:
            return
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

        self._send_json(_build_paginated_task_list(page_size, continuation_token))

    def _rest_cancel_task(self, task_id: str) -> None:
        """F-B007: POST /tasks/{id}:cancel — cancel a pending task."""
        result = self._cancel_task_core(task_id)
        if result is None:
            return
        self._send_json({"task": result})

    def _rest_subscribe_to_task(self, task_id: str) -> None:
        """F-B008: GET /tasks/{id}:subscribe — SSE stream via _stream_task_sse."""
        self._stream_task_sse(task_id, http_method="GET")

    def _rest_send_message(self, body: dict) -> None:
        """F-B005: POST /message:send — send a message and return a task result."""
        message = body.get("message", {})
        user_text = _extract_text_parts(message)

        if not user_text.strip():
            self._send_json({
                "id": str(uuid.uuid4()),
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"text": "Empty message"}], "index": 0}],
            })
            return

        metadata = message.get("metadata", {})
        task_id = body.get("id") or str(uuid.uuid4())
        context_id = body.get("contextId") or metadata.get("context_id") or task_id

        _resolve_sender_name(metadata, body, self.client_address[0])

        idem_key = body.get("idempotencyKey")
        result = self._enqueue_and_await_task(task_id, user_text, metadata,
                                               context_id, idem_key, body)

        if result == "CONFLICT" or result == "BUSY":
            return
        if result is None:
            self._send_json({
                "id": task_id,
                "status": {"state": "working"},
                "artifacts": [{"parts": [{"text": "(processing — poll with tasks/get)"}], "index": 0}],
            }, 200)
            return

        self._send_json({
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"text": result.get("response", "")}], "index": 0}],
        }, 200)


    # -------------------------------------------------------------------------
    # SSE streaming primitives (shared by subscribe + message:stream)
    # -------------------------------------------------------------------------

    def _open_sse_stream(self, task_id: str, http_method: str = "POST",
                         extra_headers: dict | None = None):
        """Open a stream, write SSE response headers, and return a send-line closure.

        Returns ``(streamer, stream_id, send_line)`` where ``send_line(str) -> bool``
        writes one SSE line and returns False if the client has disconnected.
        """
        from .sse_handler import get_sse_streamer
        streamer = get_sse_streamer()
        stream_id = streamer.open_stream(task_id)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.close_connection = True
        self.send_header("X-Stream-Id", stream_id)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origins)
        self.send_header("Access-Control-Allow-Methods", f"{http_method}, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, A2A-Version, A2A-Extensions")
        self.end_headers()

        def _send_line(line: str) -> bool:
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        return streamer, stream_id, _send_line

    def _sse_emit(self, send_line, task_id: str, state: str,
                  context_id: Optional[str] = None, with_id: bool = True) -> bool:
        """Emit a TaskStatusUpdateEvent (id/event/data lines). Returns False on disconnect."""
        from .hooks import _event_name_for_state as _event_name
        payload = json.dumps(_build_status_update_payload(task_id, state, context_id), ensure_ascii=False)
        if with_id and not send_line(f"id: {str(uuid.uuid4())}\n"):
            return False
        if not send_line(f"event: {_event_name(state)}\n"):
            return False
        if not send_line(f"data: {payload}\n\n"):
            return False
        return True

    def _sse_poll_until_terminal(self, send_line, streamer, stream_id: str,
                                 task_id: str, context_id: Optional[str] = None) -> None:
        """Poll the queue, flushing pending events until terminal state, disconnect, or timeout."""
        poll_interval = 0.5  # 5x fewer wakeups than 0.1s, still responsive (<1s latency)
        deadline = time.time() + float(os.getenv("A2A_SSE_TIMEOUT", "300"))

        while time.time() < deadline:
            for line in streamer.get_pending(stream_id):
                if not send_line(line):
                    streamer.close_stream(stream_id)
                    return
            if streamer.is_closed(stream_id):
                return
            state = _ensure_task_queue().get_status(task_id).get("state", "")
            if is_terminal_state(state):
                self._sse_emit(send_line, task_id, state, context_id, with_id=False)
                streamer.close_stream(stream_id)
                return
            time.sleep(poll_interval)

        send_line('event: error\ndata: {"code": -38000, "message": "SSE stream timed out"}\n\n')
        streamer.close_stream(stream_id)

    def _stream_task_sse(self, task_id: str, extra_headers: dict | None = None,
                          http_method: str = "POST") -> None:
        """SSE stream for an existing task — used by JSON-RPC + REST subscribe."""
        status = _ensure_task_queue().get_status(task_id)
        if status["state"] == "unknown":
            self._send_rpc_error(A2A_ERR_TASK_NOT_FOUND, f"Task not found: {task_id}", 404)
            return

        current_state = status.get("state", "unknown")
        streamer, stream_id, send_line = self._open_sse_stream(task_id, http_method, extra_headers)

        if not self._sse_emit(send_line, task_id, current_state):
            streamer.close_stream(stream_id)
            return
        if is_terminal_state(current_state):
            streamer.close_stream(stream_id)
            return

        self._sse_poll_until_terminal(send_line, streamer, stream_id, task_id)

    def _rest_send_message_stream(self, body: dict) -> None:
        """F-B009: POST /message/stream — enqueue a task and stream its lifecycle via SSE."""
        message = body.get("message", {})
        user_text = _extract_text_parts(message)

        if not user_text.strip():
            self._send_json({
                "id": str(uuid.uuid4()),
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"text": "Empty message"}], "index": 0}],
            })
            return

        metadata = message.get("metadata", {})
        task_id = body.get("id") or str(uuid.uuid4())
        context_id = body.get("contextId") or metadata.get("context_id") or task_id

        _resolve_sender_name(metadata, body, self.client_address[0])

        # Route through the shared preamble so the streaming endpoint gets the
        # same idempotency + task-ID collision guards as the JSON-RPC/REST paths.
        idem_key = body.get("idempotencyKey")
        outcome = self._prepare_and_enqueue(task_id, user_text, metadata, context_id, idem_key, body)
        if isinstance(outcome, tuple):
            kind = outcome[0]
            if kind == "REPLAY":
                # Idempotent replay: stream the cached result instead of re-enqueuing.
                self._stream_cached_result(outcome[1], context_id)
                return
            # CONFLICT/BUSY fire before the SSE stream opens, so surface them as a
            # JSON HTTP error (matching the prior pre-stream busy behaviour).
            _, code, http_status, err_message = outcome
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": code, "message": err_message}},
                http_status,
            )
            return

        streamer, stream_id, send_line = self._open_sse_stream(task_id, http_method="POST")
        _start_async_webhook_delivery(task_id)

        if not self._sse_emit(send_line, task_id, "working", context_id):
            streamer.close_stream(stream_id)
            return

        self._sse_poll_until_terminal(send_line, streamer, stream_id, task_id, context_id)

    def _stream_cached_result(self, cached_result: dict, fallback_context_id: str) -> None:
        """Stream a cached idempotent result as an SSE lifecycle without enqueuing.

        Mirrors the normal completion path (a ``working`` event followed by a
        terminal status update); the client retrieves the response body via
        tasks/get, exactly as it would for a freshly completed stream.
        """
        replay_task_id = cached_result.get("id") or fallback_context_id
        replay_ctx = cached_result.get("context_id") or fallback_context_id
        terminal_state = cached_result.get("status", {}).get("state", "completed")

        streamer, stream_id, send_line = self._open_sse_stream(replay_task_id, http_method="POST")
        if not self._sse_emit(send_line, replay_task_id, "working", replay_ctx):
            streamer.close_stream(stream_id)
            return
        self._sse_emit(send_line, replay_task_id, terminal_state, replay_ctx, with_id=False)
        streamer.close_stream(stream_id)

    def _rest_create_push_config(self, task_id: str, body: dict) -> None:
        """F-B010: POST /tasks/{id}/pushNotificationConfigs — create a push config.

        Uses CreateTaskPushNotificationConfigRequest + create_push_config from push_delivery.
        Requires X-HMAC-Key header.
        """
        if not self._check_hmac_push(required=True):
            return
        if self._load_task_or_404(task_id) is None:
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
            "config": _serialize_push_config(response.config),
        }, 201)

    def _rest_get_push_config(self, task_id: str, config_id: str) -> None:
        """F-B010: GET /tasks/{id}/pushNotificationConfigs/{config_id} — get a push config.

        Uses GetTaskPushNotificationConfigRequest + get_push_config from push_delivery.
        X-HMAC-Key optional; if provided must be valid.
        """
        if not self._check_hmac_push(required=True):
            return
        if self._load_task_or_404(task_id) is None:
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
            "config": _serialize_push_config(response.config),
        })

    def _rest_list_push_configs(self, task_id: str) -> None:
        """F-B010: GET /tasks/{id}/pushNotificationConfigs — list push configs for a task.

        Uses ListTaskPushNotificationConfigsRequest + list_push_configs from push_delivery.
        """
        if self._load_task_or_404(task_id) is None:
            return

        req = ListTaskPushNotificationConfigsRequest(task_id=task_id)
        configs = list_push_configs(req.task_id)

        response = ListTaskPushNotificationConfigsResponse(
            items=configs,
            has_more=False,
        )
        self._send_json({
            "items": [_serialize_push_config(c) for c in response.items],
        })

    def _rest_delete_push_config(self, task_id: str, config_id: str) -> None:
        """F-B010: DELETE /tasks/{id}/pushNotificationConfigs/{config_id} — delete a push config.

        Uses DeleteTaskPushNotificationConfigRequest + delete_push_config from push_delivery.
        Requires X-HMAC-Key header.
        """
        if not self._check_hmac_push(required=True):
            return
        if self._load_task_or_404(task_id) is None:
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
