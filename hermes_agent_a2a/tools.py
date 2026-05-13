"""A2A client tool handlers — outbound calls to remote agents.

Ported from v1, replacing vault/identity loading with VaultResolver from .identity.
All paths derived from HERMES_HOME env var (defaults to ~/.hermes).

Ehrlich & Lindstrom — HermesA2A 2026.
"""
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Lazy callbacks injected by plugin.py during register()
_ensure_server: Optional[callable] = None
_get_vault_resolver: Optional[callable] = None


def _vault():
    """Lazily get vault resolver."""
    if _get_vault_resolver is not None:
        return _get_vault_resolver()
    from .identity import VaultResolver
    return VaultResolver({})


def _resolve_agent_by_name(name: str):
    from .identity import resolve_agent as _resolve_agent_fn
    return _resolve_agent_fn(name)


def _list_agents():
    return _vault().list_agents()

_DEFAULT_TIMEOUT = 120
_POLL_INTERVAL = 5
_POLL_MAX_ATTEMPTS = 60
_MAX_RESPONSE_SIZE = 100_000
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX_CALLS = 30

_call_timestamps: deque[float] = deque()
_rate_lock = threading.Lock()

# ----------------------------------------------------------------------
# Helpers (kept from v1, paths updated to HERMES_HOME)
# ----------------------------------------------------------------------


def _consume_rate_limit() -> bool:
    now = time.time()
    with _rate_lock:
        while _call_timestamps and _call_timestamps[0] < now - _RATE_LIMIT_WINDOW:
            _call_timestamps.popleft()
        if len(_call_timestamps) >= _RATE_LIMIT_MAX_CALLS:
            return False
        _call_timestamps.append(now)
        return True


def _normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _validate_target_url(url: str, allow_loopback: bool = False) -> str:
    """Validate and SSRF-protect a target URL."""
    url = _normalize_url(url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("A2A URL must be an http(s) URL")
    netloc = parsed.netloc.split(":")[0]
    if not allow_loopback and netloc in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise ValueError("A2A URL cannot point to loopback addresses")
    return url


def _rate_limited(func):
    """Decorator that enforces outbound rate limits."""
    def wrapper(*args, **kwargs):
        if not _consume_rate_limit():
            return {"error": f"Rate limit exceeded: max {_RATE_LIMIT_MAX_CALLS} calls per {_RATE_LIMIT_WINDOW}s"}
        return func(*args, **kwargs)
    return wrapper


def _dict_args_handler(func):
    def wrapper(args=None, **kwargs):
        if args is None:
            args = {}
        if isinstance(args, dict):
            return func(**args, **kwargs)
        return func(args, **kwargs)
    return wrapper


# ----------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------


def _http_request(method: str, url: str, json_body: dict = None, headers: dict = None) -> dict:
    """Synchronous HTTP request using urllib (no asyncio dependency)."""
    import urllib.request
    import urllib.error

    req_headers = {"Content-Type": "application/json", "User-Agent": "Hermes-A2A/3.0"}
    if headers:
        req_headers.update(headers)

    data = json.dumps(json_body).encode() if json_body else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            data = resp.read(_MAX_RESPONSE_SIZE + 1)
            if len(data) > _MAX_RESPONSE_SIZE:
                raise RuntimeError(f"Response exceeds {_MAX_RESPONSE_SIZE} bytes")
            return json.loads(data.decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, OSError)) and "timed out" in str(e.reason):
            raise TimeoutError(f"Timed out after {_DEFAULT_TIMEOUT}s") from e
        raise ConnectionError(f"Cannot connect: {e.reason}") from e


# ----------------------------------------------------------------------
# Tool: discover
# ----------------------------------------------------------------------


def handle_discover(name: Optional[str] = None, url: Optional[str] = None, task_id: Optional[str] = None, user_task: Optional[str] = None) -> dict:
    """Fetch a remote agent's Agent Card by name or direct URL.

    Uses VaultResolver.resolve_agent(name) to look up the agent's a2a_url.
    Returns the full agent card dict.
    """
    if not name and not url:
        return {"error": "Provide either 'name' or 'url'"}

    target_url = ""
    auth_token = ""

    if name:
        agent_info = _resolve_agent_by_name(name)
        if not agent_info:
            return {"error": f"Agent '{name}' not found in vault registry"}
        target_url = agent_info.get("a2a_url", "")
        auth_token = agent_info.get("auth_token", "")
        if not target_url:
            return {"error": f"Agent '{name}' has no a2a_url in vault"}
    else:
        target_url = url

    try:
        target_url = _validate_target_url(target_url, allow_loopback=bool(name))
    except ValueError as e:
        return {"error": str(e)}

    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        card = _http_request("GET", f"{target_url.rstrip('/')}/.well-known/agent.json", headers=headers)
    except ConnectionError:
        return {"error": f"Cannot connect to {target_url}"}
    except Exception as e:
        return {"error": f"Discovery failed: {e}"}

    return {
        "agent_name": card.get("name", "unknown"),
        "description": card.get("description", ""),
        "url": target_url,
        "version": card.get("version", ""),
        "skills": [
            {"name": s.get("name", ""), "description": s.get("description", "")}
            for s in card.get("skills", [])
        ],
        "capabilities": card.get("capabilities", {}),
    }


# ----------------------------------------------------------------------
# Tool: list
# ----------------------------------------------------------------------


def handle_list(task_id: Optional[str] = None, user_task: Optional[str] = None) -> dict:
    """Return all agents registered in the vault registry.

    Uses VaultResolver.list_agents() to enumerate $HERMES_HOME/profiles/*/a2a/vault.yaml.
    """
    agents = _list_agents()
    return {
        "agents": agents,
        "count": len(agents),
    }


# ----------------------------------------------------------------------
# Mode 2: ephemeral worker on caller machine
# ----------------------------------------------------------------------


def _handle_call_mode2(
    name: str,
    message: str,
    timeout: int = 300,
) -> dict:
    """Mode 2: spawn ephemeral worker subprocess on the caller machine.

    Bypasses HTTP server, webhook, and queue entirely. Runs AIAgent with
    the target agent's profile HERMES_HOME.
    """
    if not name:
        return {"error": "'name' is required for Mode 2"}
    if not message:
        return {"error": "'message' is required for Mode 2"}

    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    agent_home = os.path.join(hermes_home, "profiles", name.lower())
    if not os.path.isdir(agent_home):
        return {"error": f"Agent profile not found: {agent_home}"}

    venv_python = os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python")
    worker_script = str(Path(__file__).parent / "_mode2_worker.py")
    plugin_dir = str(Path(__file__).parent)
    params = {
        "agent_home": agent_home,
        "hermes_home": hermes_home,
        "message": message,
        "timeout": timeout,
    }
    env = {**os.environ, "PYTHONPATH": plugin_dir + os.pathsep + os.environ.get("PYTHONPATH", "")}

    try:
        proc = subprocess.run(
            [venv_python, worker_script],
            input=json.dumps(params),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if proc.returncode == 0:
            try:
                result = json.loads(proc.stdout)
                return result if isinstance(result, dict) else {"response": str(result)}
            except json.JSONDecodeError:
                return {"error": f"Mode 2 worker returned non-JSON on stdout (rc=0): {proc.stdout[:500]!r}"}
        else:
            return {"error": f"Mode 2 worker error (rc={proc.returncode}): {proc.stderr.strip()[:500]}"}
    except subprocess.TimeoutExpired:
        return {"error": f"Mode 2 worker timed out after {timeout}s"}


# ----------------------------------------------------------------------
# Mode 3: distributed ephemeral worker (target-side)
# ----------------------------------------------------------------------


def _handle_task_send_mode3(params: dict, metadata: dict, user_text: str) -> dict:
    """Called by server.py when worker_at='target'.

    Runs the local worker subprocess and returns a JSON-RPC compatible dict.
    """
    logger.info("[A2A] _handle_task_send_mode3 — spawning local worker")
    task_id = params.get("id", str(uuid.uuid4()))

    agent_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    hermes_home = os.path.dirname(agent_home.rstrip("/"))
    if hermes_home.endswith("/profiles"):
        hermes_home = os.path.dirname(hermes_home)
    # If the derived hermes_home doesn't have a hermes-agent subdirectory, the
    # derived path was wrong (HERMES_HOME pointed to a subdir, not the root).
    # Fall back to os.path.expanduser("~/.hermes") which is the standard fleet root.
    if not Path(hermes_home).joinpath("hermes-agent").is_dir():
        hermes_home = os.path.expanduser("~/.hermes")

    venv_python = os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python")
    worker_script = str(Path(__file__).parent / "_mode2_worker.py")
    plugin_dir = str(Path(__file__).parent)
    worker_params = {
        "agent_home": agent_home,
        "hermes_home": hermes_home,
        "message": user_text,
        "timeout": 300,
    }
    env = {**os.environ, "PYTHONPATH": plugin_dir + os.pathsep + os.environ.get("PYTHONPATH", "")}

    try:
        proc = subprocess.run(
            [venv_python, worker_script],
            input=json.dumps(worker_params),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "id": task_id,
            "status": {"state": "failed"},
            "artifacts": [{"parts": [{"type": "text", "text": "Mode 3 worker timed out"}], "index": 0}],
        }

    if proc.returncode == 0:
        try:
            worker_result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {
                "id": task_id,
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"type": "text", "text": f"Mode 3: non-JSON worker output: {proc.stdout[:200]!r}"}], "index": 0}],
            }
        response_text = worker_result.get("response", "")
        return {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": response_text}], "index": 0}],
        }
    else:
        return {
            "id": task_id,
            "status": {"state": "failed"},
            "artifacts": [{"parts": [{"type": "text", "text": f"Mode 3 worker error: {proc.stderr.strip()[:300]}"}], "index": 0}],
        }


# ----------------------------------------------------------------------
# Mode 3: caller side
# ----------------------------------------------------------------------


def _handle_call_mode3(
    name: str,
    message: str,
    timeout: int = 300,
    task_id: Optional[str] = None,
) -> dict:
    """Mode 3 caller side: POST to target A2A server with worker_at='target'.

    Target runs local worker and returns result synchronously.
    """
    if not message:
        return {"error": "'message' is required for Mode 3"}
    if not name:
        return {"error": "'name' is required for Mode 3 (URL not supported in Mode 3)"}

    agent_info = _resolve_agent_by_name(name)
    if not agent_info:
        return {"error": f"Agent '{name}' not found in vault registry"}
    target_url = agent_info.get("a2a_url", "")
    auth_token = agent_info.get("auth_token", "")
    if not target_url:
        return {"error": f"Agent '{name}' has no a2a_url in vault"}

    try:
        target_url = _validate_target_url(target_url, allow_loopback=True)
    except ValueError as e:
        return {"error": str(e)}

    task_id = task_id or str(uuid.uuid4())
    tid = str(uuid.uuid4())

    payload = {
        "jsonrpc": "2.0",
        "id": tid,
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": message}],
                "metadata": {
                    "intent": "consultation",
                    "expected_action": "reply",
                    "context_scope": "full",
                    "sender_name": os.getenv("A2A_AGENT_NAME", "hermes-agent"),
                    "worker_at": "target",
                },
            },
        },
    }

    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        result = _http_request("POST", target_url.rstrip("/"), json_body=payload, headers=headers)
    except Exception as exc:
        return {"error": f"Mode 3 HTTP error: {exc}"}

    rpc_result = result.get("result", {})
    artifacts = rpc_result.get("artifacts", [])
    if artifacts:
        for artifact in artifacts:
            for part in artifact.get("parts", []):
                if part.get("type") == "text":
                    return {
                        "task_id": task_id,
                        "state": rpc_result.get("status", {}).get("state", "completed"),
                        "response": part.get("text", ""),
                        "source": f"ephemeral:{name}",
                        "mode": "3",
                    }

    status_state = rpc_result.get("status", {}).get("state", "unknown")
    return {"error": f"Mode 3: target returned status={status_state}"}


# ----------------------------------------------------------------------
# Tool: call
# ----------------------------------------------------------------------


@_rate_limited
def handle_call(
    name: Optional[str] = None,
    url: Optional[str] = None,
    message: str = "",
    worker_at: Optional[str] = None,
    task_id: Optional[str] = None,
    intent: Optional[str] = None,
    expected_action: Optional[str] = None,
    user_task: Optional[str] = None,
) -> dict:
    """Send a task/message to a remote A2A agent.

    Mode 1 (default): POST to target URL, poll for result.
    Mode 2 (worker_at='caller'): spawn ephemeral worker subprocess locally.
    Mode 3 (worker_at='target'): POST to target A2A server, worker runs on target.

    Uses VaultResolver.resolve_agent(name) to look up agent URL.
    """
    if worker_at == "caller":
        return _handle_call_mode2(name=name or "", message=message)

    if worker_at == "target":
        return _handle_call_mode3(name=name or "", message=message, task_id=task_id)

    # Mode 1 — default queued delivery
    if not message:
        return {"error": "'message' is required"}
    if not url and not name:
        return {"error": "Provide either 'url' or 'name'"}

    target_url = ""
    auth_token = ""

    if name:
        agent_info = _resolve_agent_by_name(name)
        if not agent_info:
            return {"error": f"Agent '{name}' not found in vault registry"}
        target_url = agent_info.get("a2a_url", "")
        auth_token = agent_info.get("auth_token", "")
        if not target_url:
            return {"error": f"Agent '{name}' has no a2a_url in vault"}
    else:
        target_url = url

    try:
        target_url = _validate_target_url(target_url, allow_loopback=bool(name))
    except ValueError as e:
        return {"error": str(e)}

    task_id = task_id or str(uuid.uuid4())
    tid = str(uuid.uuid4())
    resolved_intent = intent or "consultation"
    resolved_action = expected_action or "reply"

    payload = {
        "jsonrpc": "2.0",
        "id": tid,
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": message}],
                "metadata": {
                    "intent": resolved_intent,
                    "expected_action": resolved_action,
                    "context_scope": "full",
                    "sender_name": os.getenv("A2A_AGENT_NAME", "hermes-agent"),
                },
            },
        },
    }

    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    response_text = ""
    task_state = "unknown"
    error_msg = ""

    try:
        result = _http_request("POST", target_url.rstrip("/"), json_body=payload, headers=headers)
    except ConnectionError:
        error_msg = f"Cannot connect to {target_url}"
    except TimeoutError:
        error_msg = f"Remote agent timed out after {_DEFAULT_TIMEOUT}s"
    except Exception as e:
        error_msg = f"Call failed: {e}"
    else:
        rpc_error = result.get("error")
        if rpc_error:
            err_msg = rpc_error.get("message", str(rpc_error)) if isinstance(rpc_error, dict) else str(rpc_error)
            error_msg = f"Remote agent error: {err_msg}"
        else:
            rpc_result = result.get("result", {})
            task_state = rpc_result.get("status", {}).get("state", "unknown")
            remote_task_id = rpc_result.get("id", task_id)

            if task_state == "working" and remote_task_id:
                poll_payload = {
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "tasks/get",
                    "params": {"id": remote_task_id},
                }
                for attempt in range(_POLL_MAX_ATTEMPTS):
                    time.sleep(_POLL_INTERVAL)
                    try:
                        poll_result = _http_request("POST", target_url.rstrip("/"), json_body=poll_payload, headers=headers)
                        poll_inner = poll_result.get("result", {})
                        poll_state = poll_inner.get("status", {}).get("state", "")
                        if poll_state in ("completed", "failed", "canceled"):
                            rpc_result = poll_inner
                            task_state = poll_state
                            break
                    except Exception:
                        continue

            for artifact in rpc_result.get("artifacts", []):
                for part in artifact.get("parts", []):
                    if part.get("type") == "text":
                        response_text += part.get("text", "") + "\n"
            response_text = response_text.strip()

    if error_msg:
        return {"error": error_msg}

    return {
        "task_id": rpc_result.get("id", task_id),
        "state": task_state,
        "response": response_text or "(no text response)",
        "source": target_url,
    }


# ----------------------------------------------------------------------
# Tool: session message
# ----------------------------------------------------------------------


def handle_telegram(args: dict = None, **kwargs) -> dict:
    """Send a session-aware message to a Hermes mesh peer.

    Two-part delivery:
    1. Webhook to target agent's Hermes gateway relay.
    2. Echo to sender's Telegram DM when configured.

    Routes the message to the target agent's gateway webhook so that the
    target gateway/config resolves it into the target Telegram session and
    invokes the target agent. Also echoes the same padded message to the
    sender's own Telegram DM for operator visibility when sender Telegram
    credentials are available.
    Auto-pads [a2a][from:<self>][to:<agent>][id:<uuid>][cta:<cta>] header.
    Caller passes raw message; tool handles mesh metadata. No response returned.

    Supports two call conventions:
    - registry.dispatch(name, {key: val})      → args dict is first positional
    - registry.dispatch(name, {}, key=val)    → kwargs carry the arguments
    """
    # Merge args (from positional dict) with kwargs (from **kwargs dispatch).
    # This covers both registry.dispatch(name, args) and the LLM's
    # registry.dispatch(name, {}, message=..., agent=...) patterns.
    merged = dict(args) if args else {}
    merged.update(kwargs)

    message = merged.get("message", "")
    agent = merged.get("agent", "")
    cta = merged.get("cta", "reply")
    ref = merged.get("ref")
    task_id = merged.get("task_id")
    user_task = merged.get("user_task")

    if not message:
        return {"error": "'message' is required"}
    if not agent:
        return {"error": "'agent' is required"}

    # Own bot_token: resolve from caller's own vault via VaultResolver.
    # This is used only for the non-fatal sender-side visibility echo.
    try:
        own_vault = _vault().resolve()
    except RuntimeError:
        own_vault = {}

    own_bot_token = own_vault.get("platforms", {}).get("telegram", {}).get("bot_token", "")

    # Target delivery: route to target gateway webhook. The target gateway's
    # config.yaml owns target_session/deliver_extra and resolves the message
    # into the target Telegram session.
    target_info = _resolve_agent_by_name(agent)
    if not target_info:
        return {"error": f"Agent '{agent}' not found in vault registry"}

    from_agent = os.getenv("A2A_AGENT_NAME", "hermes-agent")
    msg_id = str(uuid.uuid4())[:12]
    header = f"[a2a][from:{from_agent}][to:{agent}][id:{msg_id}][cta:{cta}]"
    if ref:
        header += f"[ref:{ref}]"
    padded_message = f"{header} {message}"

    # Part 1: Webhook to target agent's gateway relay.
    target_webhook_url = target_info.get("webhook_url", "") if isinstance(target_info, dict) else ""
    import hashlib, hmac, json as _json
    delivery_id = None
    if target_webhook_url:
        webhook_secret = target_info.get("webhook_secret", "") if isinstance(target_info, dict) else ""
        body = _json.dumps({"text": padded_message})
        sig = hmac.new(
            webhook_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest() if webhook_secret else ""
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
        }
        try:
            import urllib.request
            req = urllib.request.Request(
                target_webhook_url,
                data=body.encode(),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read().decode())
                delivery_id = result.get("delivery_id", "unknown")
        except Exception as exc:
            return {"error": f"Webhook to agent '{agent}' failed: {exc}"}
    else:
        return {"error": f"Agent '{agent}' has no webhook_url in vault"}

    # Part 2: Echo to sender's Telegram DM for visibility.
    own_telegram_chat_id = own_vault.get("platforms", {}).get("telegram", {}).get("default_chat_id", "")
    echo_ok = False
    if own_bot_token and own_telegram_chat_id:
        from .platforms.telegram import TelegramHandler
        handler = TelegramHandler()
        echo_result = handler.send_message(
            token=own_bot_token,
            chat_id=str(own_telegram_chat_id),
            text=padded_message,
            parse_mode="HTML",
        )
        echo_ok = bool(echo_result.get("ok", False))
        # Non-fatal if echo fails — the webhook delivery is what matters
        if not echo_ok:
            import logging
            logging.getLogger(__name__).warning(
                "[a2a_send_session_message] sender echo failed (non-fatal): %s", echo_result
            )

    return {
        "status": "delivered",
        "message_id": delivery_id,
        "agent": agent,
        "gateway_delivery": True,
        "sender_echo": echo_ok,
    }


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


def register(registry, ensure_server=None, get_vault_resolver=None) -> None:
    """Register all A2A tools with the Hermes gateway registry.

    Args:
        registry: Hermes plugin registry
        ensure_server: Callback to start the A2A server (lazy init)
        get_vault_resolver: Callable returning a VaultResolver instance
    """
    from . import schemas

    # Store callbacks for lazy startup
    global _ensure_server, _get_vault_resolver
    _ensure_server = ensure_server
    _get_vault_resolver = get_vault_resolver

    registry.register_tool(
        name=schemas.A2A_DISCOVER["name"],
        toolset="a2a",
        schema=schemas.A2A_DISCOVER,
        handler=_dict_args_handler(handle_discover),
    )
    registry.register_tool(
        name=schemas.A2A_LIST["name"],
        toolset="a2a",
        schema=schemas.A2A_LIST,
        handler=_dict_args_handler(handle_list),
    )
    registry.register_tool(
        name=schemas.A2A_CALL["name"],
        toolset="a2a",
        schema=schemas.A2A_CALL,
        handler=_dict_args_handler(handle_call),
    )
    registry.register_tool(
        name=schemas.A2A_TELEGRAM["name"],
        toolset="a2a",
        schema=schemas.A2A_TELEGRAM,
        handler=handle_telegram,
    )
    logger.info("[A2A] Phase 3 tools registered")
