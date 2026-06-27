"""A2A direct transport — single-shot JSON-RPC call to a remote A2A agent.

This is the protocol-level transport: one POST to a target agent's A2A
endpoint, no retry, no webhook routing. For webhook-based delivery (with
HMAC, retries, SSRF guard), see ``webhook_delivery``.

Extracted from ``server.py`` as part of LOW-08 (a2a-review-20260602) to
keep transport modules leaf-level — no britney, no linda, no assumptions
about who's calling. The caller passes a target URL and credentials.

Why the module-level name is the transport:
    * ``a2a_direct.call`` — synchronous single-shot
    * ``a2a_direct.call_async`` — async wrapper using ``asyncio.to_thread``

No behaviour change vs the previous ``_call_a2a_direct`` /
``_call_a2a_direct_async`` private functions in ``server.py``. The leading
underscores are dropped because the module name is the transport, and the
public function name is the action.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from .security import validate_target_url as _validate_target_url


def call(url: str, message: str, task_id: str, auth_token: str = "", timeout: int = 10, _allow_loopback: bool = False) -> dict:
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
    # Lazy import preserved from the original server.py implementation —
    # avoids pulling a2a_spec onto the hot path when a2a_direct is imported
    # for tests or for other transports.
    from .a2a_spec.tasks import build_task_send_payload

    from_agent = os.getenv("A2A_AGENT_NAME", "hermes-agent")
    payload = build_task_send_payload(
        task_id=task_id,
        message=message,
        sender_name=from_agent,
        intent="consultation",
        expected_action="reply",
    )
    _validate_target_url(url, allow_loopback=_allow_loopback)

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


async def call_async(url: str, message: str, task_id: str, auth_token: str = "", timeout: int = 10) -> dict:
    """Async wrapper for ``call`` — runs blocking I/O in a thread pool."""
    return await asyncio.to_thread(call, url, message, task_id, auth_token, timeout)
