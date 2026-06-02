"""A2A webhook delivery transport — HMAC-signed POST to the local agent webhook.

This is the *production* float path for cross-agent delivery: signed
payloads, retry-with-backoff, SSRF guard on the host env var. The
opposite of ``a2a_direct`` (which is a one-shot JSON-RPC call to a
specific target URL). The britney-private concession framed this as
``webhook_delivery.deliver(task_id)``, but the original function names
``trigger`` and ``trigger_async`` are preserved here to keep the move
purely a code-organization refactor with no public-API churn. The
"deliver" verb can land in a follow-up commit if the naming is
revisited.

Extracted from ``server.py`` as part of LOW-08 (a2a-review-20260602).
The transport is leaf-level — no britney, no linda, no assumptions
about who's calling. The caller passes a message, task_id, and
optional mode/deliver_only flags; the env-var reads (retry count,
backoff, secret, host, port) are preserved as-is.

Why the module name is the transport:
    * ``webhook_delivery.trigger`` — synchronous, blocking, used from
      sync callers (the main ``_trigger_webhook`` call path).
    * ``webhook_delivery.trigger_async`` — async wrapper that runs the
      blocking I/O in a thread pool via ``asyncio.to_thread``.

No behaviour change vs the previous ``_trigger_webhook`` /
``_trigger_webhook_async`` private functions in ``server.py``. The
leading underscores are dropped because the module name is the
transport, and the public function name is the action.

The SSRF guard (``_validate_webhook_host``) stays in ``server.py``
because it's also used by ``_push_notifications_capability`` (a class
method on the A2ARequestHandler). Importing it lazily here breaks the
circular dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def _urlopen_with_status(req, timeout):
    """Open a URL and return the response object. Used via asyncio.to_thread."""
    return urllib.request.urlopen(req, timeout=timeout)


def trigger(message: str = "", task_id: str = "", mode: str = None, deliver_only: bool = False, retries=None, base_delay=None, on_failure=None, use_direct_a2a: bool = False, target_url: str = "", auth_token: str = ""):
    """Trigger an agent turn via webhook or direct A2A call.

    Args:
        deliver_only: if True, the webhook handler invokes the agent but skips
            Telegram routing (used for peer-originated A2A tasks).
        use_direct_a2a: if True, use direct A2A JSON-RPC call instead of webhook.
        target_url: A2A endpoint URL (required when use_direct_a2a=True).
        auth_token: Bearer token for A2A authentication (optional).
        on_failure: optional callable invoked with (task_id,) if all retries fail.
    """
    # Lazy import to break the circular dependency with server.py —
    # _validate_webhook_host lives in server.py because it's also used
    # by _push_notifications_capability (a class method on the request
    # handler), and importing server.py at module-load time would
    # create a cycle.
    from .server import _validate_webhook_host

    # Use direct A2A for modes 1,2,3 (protocol tasks, workers)
    if use_direct_a2a and target_url:
        from .a2a_direct import call
        result = call(target_url, message, task_id, auth_token)
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


async def trigger_async(message: str = "", task_id: str = "", mode: str = None, deliver_only: bool = False, retries=None, base_delay=None, on_failure=None, use_direct_a2a: bool = False, target_url: str = "", auth_token: str = ""):
    """Async variant of trigger — runs blocking I/O in a thread pool.

    Replaces the sync trigger when called from an async context to avoid
    blocking the event loop with urllib.request.urlopen calls.
    """
    # Lazy import to break the circular dependency with server.py.
    from .server import _validate_webhook_host

    if use_direct_a2a and target_url:
        from .a2a_direct import call_async
        result = await call_async(target_url, message, task_id, auth_token)
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
