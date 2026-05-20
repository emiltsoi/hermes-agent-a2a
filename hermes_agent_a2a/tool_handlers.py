"""A2A client tool handlers — outbound calls to remote agents.

Ported from v1, replacing vault/identity loading with VaultResolver from .identity.
All paths derived from HERMES_HOME env var (defaults to ~/.hermes).

Ehrlich & Lindstrom — HermesA2A 2026.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .a2a_spec.agent_card import validate_skill
from .a2a_spec.hermes_ext import build_hermes_metadata
from .a2a_spec.tasks import (
    build_task_cancel_payload,
    build_task_get_payload,
    build_task_send_payload,
    is_terminal_state,
    parse_json_rpc_error,
    parse_task_result,
)
from .worker_registry import cancel_worker, register_worker, unregister_worker

logger = logging.getLogger(__name__)

# Lazy callbacks injected by plugin.py during register()
_ensure_server: Optional[callable] = None
_get_vault_resolver: Optional[callable] = None


def set_runtime_callbacks(ensure_server=None, get_vault_resolver=None, force=False) -> None:
    """Set runtime callbacks injected by plugin.py during register().
    
    Args:
        force: If True, overwrite existing callbacks. If False (default),
               only set callbacks that are currently None to prevent
               overwriting on plugin reload.
    """
    global _ensure_server, _get_vault_resolver
    if force or _ensure_server is None:
        _ensure_server = ensure_server
    if force or _get_vault_resolver is None:
        _get_vault_resolver = get_vault_resolver


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


def _fleet_agents_root() -> Path:
    from .identity import _fleet_root
    return _fleet_root() / "a2a" / "agents"


def _is_local_fleet_agent(agent_name: str) -> bool:
    """Returns True if agent is registered in the local fleet registry with a valid non-loopback URL."""
    fleet_path = Path(os.environ.get("A2A_VAULT_PATH", str(Path.home() / ".hermes/fleet")))
    registry_path = fleet_path / "fleet-registry.yaml"
    if not registry_path.exists():
        return False
    try:
        import yaml
        with open(registry_path, encoding="utf-8") as f:
            registry = yaml.safe_load(f) or {}
        agents = registry.get("agents", {})
        if agent_name not in agents:
            return False
        entry = agents[agent_name]
        if isinstance(entry, dict):
            registry_url = entry.get("url", "")
        else:
            registry_url = str(entry) if entry else ""
        if registry_url:
            _validate_target_url(registry_url, allow_loopback=True)
        return True
    except Exception:
        return False


def _derive_hermes_home() -> str:
    """Derive the Hermes root directory from HERMES_HOME or sensible defaults.
    
    Handles both profile paths (e.g., ~/.hermes/profiles/agent0) and root paths
    (e.g., ~/.hermes). Validates that the derived path contains expected structure.
    
    Returns:
        Absolute path to Hermes root directory.
    
    Raises:
        ValueError: If derived path doesn't contain expected structure.
    """
    # Start with HERMES_HOME env var or default
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    hermes_home = os.path.abspath(hermes_home)
    
    # If path ends with /profiles, strip it to get the root
    if hermes_home.endswith("/profiles"):
        hermes_home = os.path.dirname(hermes_home)
    
    # If path is inside a profiles subdir, go up to root
    if "/profiles/" in hermes_home:
        hermes_home = hermes_home.split("/profiles/")[0]
    
    # Validate the derived path contains expected structure
    hermes_agent_path = os.path.join(hermes_home, "hermes-agent")
    if not Path(hermes_agent_path).is_dir():
        # Only fall back if HERMES_HOME was not explicitly set
        hermes_home_was_default = "HERMES_HOME" not in os.environ
        if hermes_home_was_default:
            fallback = str(Path.home() / ".hermes")
            if Path(os.path.join(fallback, "hermes-agent")).is_dir():
                return fallback
        raise ValueError(
            f"Cannot find Hermes installation at {hermes_home}. "
            f"Set HERMES_HOME to the correct root directory."
        )
    
    return hermes_home


def _validate_agent_webhook_config(agent_info: dict) -> tuple[bool, str]:
    """Validate that an agent has required webhook configuration for session relay.
    
    Args:
        agent_info: Agent identity dictionary from vault.
    
    Returns:
        Tuple of (is_valid, error_message).
    """
    hermes_webhook = _transport(agent_info, "hermes_webhook")
    webhook_url = hermes_webhook.get("url", "")
    webhook_secret = _transport_auth_value(hermes_webhook, "secret")
    
    if not webhook_url:
        return False, "Agent has no hermes_webhook.url configured"
    
    if not webhook_secret:
        return False, "Agent has no hermes_webhook.secret configured - HMAC signature required"
    
    return True, ""


def _validate_webhook_reachable(webhook_url: str, timeout: int = 5) -> tuple[bool, str]:
    """Validate that a webhook URL is reachable via HEAD request.
    
    Args:
        webhook_url: The webhook URL to check.
        timeout: Timeout in seconds for the reachability check.
    
    Returns:
        Tuple of (is_reachable, error_message).
    """
    try:
        import urllib.request
        req = urllib.request.Request(webhook_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Any 2xx or 3xx response is considered reachable
            if 200 <= resp.status < 400:
                return True, ""
            return False, f"Webhook returned status {resp.status}"
    except urllib.error.HTTPError as exc:
        # Some servers don't support HEAD, try GET instead
        if exc.code == 405:
            try:
                req = urllib.request.Request(webhook_url, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if 200 <= resp.status < 400:
                        return True, ""
                    return False, f"Webhook returned status {resp.status}"
            except Exception as e:
                return False, f"Webhook unreachable: {e}"
        return False, f"Webhook returned status {exc.code}"
    except Exception as exc:
        return False, f"Webhook unreachable: {exc}"

_DEFAULT_TIMEOUT = int(os.getenv("A2A_DEFAULT_TIMEOUT", "120"))
_POLL_INTERVAL = int(os.getenv("A2A_POLL_INTERVAL", "5"))
_POLL_MAX_ATTEMPTS = int(os.getenv("A2A_POLL_MAX_ATTEMPTS", "60"))
_MAX_RESPONSE_SIZE = int(os.getenv("A2A_MAX_RESPONSE_SIZE", "100000"))
_RATE_LIMIT_WINDOW = int(os.getenv("A2A_RATE_LIMIT_WINDOW", "60"))
_RATE_LIMIT_MAX_CALLS = int(os.getenv("A2A_RATE_LIMIT_MAX_CALLS", "30"))

_call_timestamps: deque[float] = deque()
_rate_lock = threading.Lock()


def handle_help(topic: str = "overview") -> dict:
    topic = (topic or "overview").strip().lower()
    tools = {
        "a2a_help": "Show this guide.",
        "a2a_list": "List agents in the local Hermes A2A fleet identity registry.",
        "a2a_discover": "Fetch an Agent Card from a known fleet agent or direct external A2A URL.",
        "a2a_send_protocol_task": "Send a real A2A protocol task over tasks/send and poll tasks/get.",
        "a2a_cancel_protocol_task": "Cancel a protocol task with tasks/cancel.",
        "a2a_run_local_agent_task": "Run a target Hermes profile as an ephemeral worker on the caller machine.",
        "a2a_run_remote_agent_task": "Ask a target Hermes agent to spawn an ephemeral worker on the target machine.",
        "a2a_send_session_message": "Send through a target Hermes gateway into its configured platform/session context.",
        "a2a_get_metrics": "Get current A2A plugin metrics (uptime, webhook stats, task counts, queue depth).",
    }
    guidance = {
        "overview": [
            "Use a2a_send_protocol_task for the actual A2A protocol path.",
            "Use a2a_run_local_agent_task or a2a_run_remote_agent_task for Hermes-specific ephemeral workers.",
            "Use a2a_send_session_message when you need the target gateway/session context rather than protocol task state.",
            "Use a2a_discover before protocol calls, especially for external agents.",
        ],
        "protocol": [
            "a2a_send_protocol_task uses JSON-RPC tasks/send and tasks/get.",
            "a2a_cancel_protocol_task uses JSON-RPC tasks/cancel for cancelable remote tasks.",
            "It is the compatibility path for A2A-style agents and the path to expand for external agents.",
            "It accepts either name from the fleet registry or a direct url.",
        ],
        "workers": [
            "a2a_run_local_agent_task runs the target profile locally and requires that profile on the caller filesystem.",
            "a2a_run_remote_agent_task calls the target A2A server and asks it to run a target-side worker.",
            "Worker tools are Hermes-specific and are not generic external A2A protocol operations.",
        ],
        "sessions": [
            "a2a_send_session_message sends one-way through the Hermes gateway/session relay.",
            "The target Hermes profile must configure session/webhook routing in config.yaml so inbound webhook text reaches the intended platform/session.",
            "Use it for human-visible or platform-routed conversations where config.yaml owns session routing.",
            "It returns delivery status only; it does not wait for or guarantee a semantic reply.",
            "It is a Hermes extension to the A2A-shaped task model, not a standard request/response protocol task.",
            "CTA is 2D: action (do|info) + reply (yes|no). Action: do (take action) | info (log/acknowledge). Reply: yes (expects reply) | no (fire-and-forget).",
        ],
        "external_agents": [
            "Start with a2a_discover(url='https://external-agent.example') to fetch the Agent Card.",
            "Then use a2a_send_protocol_task(url='https://external-agent.example', message='...', auth_token='...') when bearer auth is required.",
            "External-agent support should extend the protocol path, not the Hermes worker tools.",
            "The protocol path supports direct URLs, bearer/api-key/custom-header auth, timeouts, and tasks/get polling controls.",
            "Future work: richer Agent Card skill selection, auth negotiation beyond bearer tokens, streaming, and non-Hermes task state mapping.",
        ],
        "external_requirements": [
            "Ask the external A2A provider for the Agent Card URL, usually https://host/.well-known/agent.json.",
            "Ask for the JSON-RPC task endpoint URL that accepts tasks/send and tasks/get.",
            "Ask whether the Agent Card URL and JSON-RPC endpoint URL are the same base URL or separate URLs.",
            "Ask for the auth scheme: none, bearer token, API key header, signed request, mTLS, or another mechanism.",
            "Ask for supported A2A methods: tasks/send, tasks/get, tasks/cancel, streaming, or vendor-specific methods.",
            "Ask for supported message part types: text, data, file, image, or vendor-specific parts.",
            "Ask for task state behavior: submitted, working, completed, failed, canceled, rejected, or custom states.",
            "Ask for timeout, polling, rate-limit, and maximum payload/response-size expectations.",
            "Ask whether responses return text in artifacts.parts, status.message.parts, message.parts, or a custom field.",
            "For named external agents, store transports.a2a_rpc.url/auth and optional transports.agent_card.url/path/auth in identity.yaml.",
            "After receiving the Agent Card URL, call a2a_discover(url='...', auth_token='...') and inspect raw_card.",
        ],
        "register_external": [
            "Use a2a_discover(url='...', register=True, register_as='name', rpc_url='...') to create a local identity.yaml entry.",
            "Prefer auth_token_env or auth_value_env when registering so secrets stay in environment variables, not files.",
            "After registration, call a2a_discover(name='name') and a2a_send_protocol_task(name='name', message='...').",
            "Use register_overwrite=True only when intentionally replacing an existing identity.",
        ],
        "security": [
            "Direct URL calls reject loopback addresses; named registry calls may allow local fleet URLs.",
            "Prefer environment-variable backed auth in identity.yaml: token_env or value_env.",
            "Supported direct auth modes are none, bearer, api_key, and custom_header.",
            "Do not store raw third-party secrets in prompts, chat history, or committed identity files.",
        ],
        "troubleshooting": [
            "401/403 usually means auth_type/auth_header/auth_token/auth_value or identity.yaml auth is wrong.",
            "Connection errors usually mean the JSON-RPC endpoint URL is wrong or unreachable.",
            "Discovery errors usually mean agent_card_path is wrong or the server does not expose an Agent Card.",
            "No text response means the external agent returned a non-standard response shape; inspect raw_result.",
        ],
        "architecture": [
            "The A2A plugin runs within the Hermes gateway process, not as a separate service.",
            "The A2A HTTP server is a background thread within the gateway process.",
            "Logging is gateway-side: all plugin logging uses the gateway's logger configuration.",
            "Log destination (stdout, file, aggregation) is controlled by gateway logging config, not the A2A plugin.",
            "The plugin shares process-wide state via A2ARuntimeState singleton (replaces builtins hack).",
            "Hooks intercept LLM calls to inject/extract A2A tasks from the gateway's conversation flow.",
        ],
        "examples": [
            "a2a_discover(name='yoyo')",
            "a2a_send_protocol_task(name='yoyo', message='Review this plan')",
            "a2a_run_local_agent_task(name='yoyo', message='Think locally', timeout=300)",
            "a2a_run_remote_agent_task(name='yoyo', message='Think on your own machine', timeout=300)",
            "a2a_send_session_message(agent='yoyo', message='Please reply in your active session')",
            "a2a_discover(url='https://external-agent.example')",
            "a2a_send_protocol_task(url='https://external-agent.example', message='Hello external A2A', auth_token='...', timeout=30)",
        ],
    }
    return {
        "topic": topic,
        "tools": tools,
        "guidance": guidance.get(topic, guidance["overview"]),
        "topics": sorted(guidance.keys()),
    }


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


def _transport(agent_info: dict, name: str) -> dict:
    if not isinstance(agent_info, dict):
        return {}
    transport = agent_info.get("transports", {}).get(name, {})
    return transport if isinstance(transport, dict) else {}


def _transport_auth_value(transport: dict, key: str) -> str:
    auth = transport.get("auth", {}) if isinstance(transport, dict) else {}
    if not isinstance(auth, dict):
        return ""
    return auth.get(key, "") or ""


def _auth_headers(auth: Optional[dict] = None) -> dict:
    if not isinstance(auth, dict):
        return {}
    auth_type = str(auth.get("type") or "none").lower()
    if auth_type in ("none", ""):
        return {}
    if auth_type == "bearer":
        token = auth.get("token") or auth.get("value") or ""
        return {"Authorization": f"Bearer {token}"} if token else {}
    if auth_type in ("api_key", "custom_header"):
        header = auth.get("header") or auth.get("name") or ""
        value = auth.get("value") or auth.get("token") or ""
        return {header: value} if header and value else {}
    return {}


def _direct_auth(auth_token: Optional[str] = None, auth_type: Optional[str] = None, auth_header: Optional[str] = None, auth_value: Optional[str] = None) -> dict:
    if auth_type or auth_header or auth_value:
        return {
            "type": auth_type or ("custom_header" if auth_header else "bearer"),
            "header": auth_header or "",
            "value": auth_value or auth_token or "",
            "token": auth_token or auth_value or "",
        }
    return {"type": "bearer", "token": auth_token} if auth_token else {"type": "none"}


def _safe_agent_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9_.-]+", "-", (name or "").strip().lower()).strip("-._")
    return key[:80]


def _public_auth_config(auth_type: Optional[str], auth_header: Optional[str], auth_token_env: Optional[str], auth_value_env: Optional[str], auth: dict) -> dict:
    auth_type = (auth_type or auth.get("type") or "none").lower()
    if auth_type == "bearer":
        return {"type": "bearer", "token_env": auth_token_env} if auth_token_env else {"type": "none"}
    if auth_type in ("api_key", "custom_header"):
        header = auth_header or auth.get("header") or auth.get("name") or ""
        cfg = {"type": auth_type}
        if header:
            cfg["header"] = header
        if auth_value_env:
            cfg["value_env"] = auth_value_env
        return cfg if cfg.get("header") and cfg.get("value_env") else {"type": "none"}
    return {"type": "none"}


def _register_external_agent_identity(
    *,
    register_as: str,
    card: dict,
    agent_card_url: str,
    agent_card_path: str,
    rpc_url: str,
    auth: dict,
    auth_type: Optional[str],
    auth_header: Optional[str],
    auth_token_env: Optional[str],
    auth_value_env: Optional[str],
    overwrite: bool,
) -> dict:
    import yaml

    agent_key = _safe_agent_key(register_as)
    if not agent_key:
        return {"error": "register_as must contain at least one alphanumeric, '.', '_' or '-' character"}
    try:
        rpc_url = _validate_target_url(rpc_url, allow_loopback=False)
        agent_card_url = _validate_target_url(agent_card_url, allow_loopback=False)
    except ValueError as e:
        return {"error": str(e)}

    target_dir = _fleet_agents_root() / agent_key
    target_file = target_dir / "identity.yaml"
    if target_file.exists() and not overwrite:
        return {"error": f"Identity already exists for '{agent_key}'; pass register_overwrite=true to replace it"}

    if ".." in agent_card_path:
        return {"error": "agent_card_path contains '..' which is not allowed for security reasons"}
    safe_path = agent_card_path if agent_card_path.startswith("/") else f"/{agent_card_path}"
    public_auth = _public_auth_config(auth_type, auth_header, auth_token_env, auth_value_env, auth)
    identity = {
        "id": agent_key,
        "name": register_as,
        "description": card.get("description", ""),
        "role": "external-a2a-agent",
        "external": True,
        "transports": {
            "a2a_rpc": {
                "protocol": "google-a2a",
                "url": rpc_url,
                "auth": public_auth,
            },
            "agent_card": {
                "protocol": "google-a2a-agent-card",
                "url": agent_card_url,
                "path": safe_path,
                "auth": public_auth,
            },
        },
        "metadata": {
            "source": "a2a_discover",
            "agent_card_name": card.get("name", ""),
            "version": card.get("version", ""),
            "skills": [
                {"name": skill.get("name", ""), "description": skill.get("description", "")}
                for skill in card.get("skills", []) if isinstance(skill, dict)
            ],
            "capabilities": card.get("capabilities", {}),
        },
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    with open(target_file, "w") as f:
        yaml.safe_dump(identity, f, sort_keys=False)
    return {"registered": True, "name": agent_key, "path": str(target_file)}


# ----------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------


def _http_request(method: str, url: str, json_body: dict = None, headers: dict = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """Synchronous HTTP request using urllib (no asyncio dependency)."""
    import urllib.request
    import urllib.error

    req_headers = {"Content-Type": "application/json", "User-Agent": "Hermes-A2A/3.0"}
    if headers:
        req_headers.update(headers)

    data = json.dumps(json_body).encode() if json_body else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(_MAX_RESPONSE_SIZE + 1)
            if len(data) > _MAX_RESPONSE_SIZE:
                raise RuntimeError(f"Response exceeds {_MAX_RESPONSE_SIZE} bytes")
            return json.loads(data.decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(_MAX_RESPONSE_SIZE).decode(errors="replace")
        except Exception:
            body = ""
        detail = f": {body[:500]}" if body else ""
        raise RuntimeError(f"HTTP {e.code}{detail}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, OSError)) and "timed out" in str(e.reason):
            raise TimeoutError(f"Timed out after {timeout}s") from e
        raise ConnectionError(f"Cannot connect: {e.reason}") from e


# ----------------------------------------------------------------------
# Tool: discover
# ----------------------------------------------------------------------


def handle_discover(
    name: Optional[str] = None,
    url: Optional[str] = None,
    auth_token: Optional[str] = None,
    auth_type: Optional[str] = None,
    auth_header: Optional[str] = None,
    auth_value: Optional[str] = None,
    agent_card_path: str = "/.well-known/agent.json",
    register: bool = False,
    register_as: Optional[str] = None,
    rpc_url: Optional[str] = None,
    auth_token_env: Optional[str] = None,
    auth_value_env: Optional[str] = None,
    register_overwrite: bool = False,
    task_id: Optional[str] = None,
    user_task: Optional[str] = None,
) -> dict:
    """Fetch a remote agent's Agent Card by name or direct URL.

    Uses VaultResolver.resolve_agent(name) to look up the agent's a2a_url.
    Returns the full agent card dict.
    """
    if not name and not url:
        return {"error": "Provide either 'name' or 'url'"}

    target_url = ""
    resolved_auth = _direct_auth(auth_token, auth_type, auth_header, auth_value)

    if name:
        agent_info = _resolve_agent_by_name(name)
        if not agent_info:
            return {"error": f"Agent '{name}' not found in vault registry"}
        a2a_rpc = _transport(agent_info, "a2a_rpc")
        agent_card = _transport(agent_info, "agent_card")
        target_url = agent_card.get("url", "") or a2a_rpc.get("url", "") or agent_info.get("a2a_url", "")
        resolved_auth = agent_card.get("auth") or a2a_rpc.get("auth") or {"type": "bearer", "token": agent_info.get("auth_token", "")}
        agent_card_path = agent_card.get("path", agent_card_path)
        allow_loopback = bool(agent_card.get("allow_loopback") or a2a_rpc.get("allow_loopback") or agent_info.get("allow_loopback"))
        if not target_url:
            return {"error": f"Agent '{name}' has no a2a_url in vault"}
    else:
        target_url = url
        allow_loopback = False

    try:
        target_url = _validate_target_url(target_url, allow_loopback=allow_loopback)
    except ValueError as e:
        return {"error": str(e)}

    headers = _auth_headers(resolved_auth)

    try:
        card_path = agent_card_path or "/.well-known/agent.json"
        if not card_path.startswith("/"):
            card_path = "/" + card_path
        # Prevent path traversal attacks
        if ".." in card_path:
            return {"error": "card_path contains '..' which is not allowed for security reasons"}
        card = _http_request("GET", f"{target_url.rstrip('/')}{card_path}", headers=headers)
    except ConnectionError:
        return {"error": f"Cannot connect to {target_url}"}
    except Exception as e:
        return {"error": f"Discovery failed: {e}"}

    result = {
        "agent_name": card.get("name", "unknown"),
        "description": card.get("description", ""),
        "url": target_url,
        "version": card.get("version", ""),
        "skills": [
            {"name": s.get("name", ""), "description": s.get("description", "")}
            for s in card.get("skills", [])
        ],
        "capabilities": card.get("capabilities", {}),
        "raw_card": card,
    }
    if register:
        if name and not register_as:
            result["registration"] = {"registered": False, "reason": "already_resolved_by_name"}
        else:
            registration = _register_external_agent_identity(
                register_as=register_as or card.get("name") or urlparse(target_url).netloc,
                card=card,
                agent_card_url=target_url,
                agent_card_path=card_path,
                rpc_url=rpc_url or target_url,
                auth=resolved_auth,
                auth_type=auth_type,
                auth_header=auth_header,
                auth_token_env=auth_token_env,
                auth_value_env=auth_value_env,
                overwrite=bool(register_overwrite),
            )
            if registration.get("error"):
                result["registration"] = {"registered": False, **registration}
            else:
                result["registration"] = registration
    return result


# ----------------------------------------------------------------------
# Tool: list
# ----------------------------------------------------------------------


def handle_list(task_id: Optional[str] = None, user_task: Optional[str] = None) -> dict:
    """Return all agents registered in the vault registry.

    Uses VaultResolver.list_agents() to enumerate $HERMES_HOME/profiles/*/a2a/vault.yaml.

    Args:
        task_id: Optional task correlation ID (passed through, not used for filtering).
        user_task: Optional user task label (passed through, not used for filtering).
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
    task_id: Optional[str] = None,
) -> dict:
    """Mode 2: spawn ephemeral worker subprocess on the caller machine.

    Bypasses HTTP server, webhook, and queue entirely. Runs AIAgent with
    the target agent's profile HERMES_HOME.
    """
    if not name:
        return {"error": "'name' is required for Mode 2"}
    if not message:
        return {"error": "'message' is required for Mode 2"}

    task_id = task_id or str(uuid.uuid4())
    hermes = build_hermes_metadata(route="worker", execution="local_subprocess", isolation="local_profile")
    envelope = build_task_send_payload(
        task_id=task_id,
        message=message,
        sender_name=os.getenv("A2A_AGENT_NAME", "hermes-agent"),
        intent="consultation",
        expected_action="reply",
        hermes=hermes,
    )
    envelope["params"]["timeout"] = timeout

    hermes_home = _derive_hermes_home()
    agent_home = os.path.join(hermes_home, "profiles", name.lower())
    if not os.path.isdir(agent_home):
        return {"error": f"Agent profile not found: {agent_home}"}

    venv_python = os.environ.get("A2A_VENV_PYTHON", os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python"))
    worker_script = str(Path(__file__).parent / "_mode2_worker.py")
    plugin_dir = str(Path(__file__).parent)
    params = {
        "agent_home": agent_home,
        "hermes_home": hermes_home,
        "message": message,
        "timeout": timeout,
    }
    env = {
        "HERMES_HOME": hermes_home,
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": plugin_dir + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    proc = None
    try:
        proc = subprocess.Popen(
            [venv_python, worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        register_worker(task_id, proc)
        stdout, stderr = proc.communicate(input=json.dumps(params), timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.wait()
        return {"error": f"Mode 2 worker timed out after {timeout}s"}
    finally:
        unregister_worker(task_id)

    if proc is None or proc.returncode != 0:
        err = stderr.strip() if proc else "process not started"
        return {"error": f"Mode 2 worker error (rc={proc.returncode if proc else 'N/A'}): {err[:500]}"}
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            result = {"response": str(result)}
        return {
            "task_id": task_id,
            "state": "completed",
            "response": result.get("response", ""),
            "source": f"ephemeral-local:{name}",
            "mode": "2",
            "hermes": hermes,
            "a2a_envelope": envelope,
            "raw_result": result,
        }
    except json.JSONDecodeError:
        return {"error": f"Mode 2 worker returned non-JSON on stdout (rc=0): {stdout[:500]!r}"}


# ----------------------------------------------------------------------
# Mode 3: distributed ephemeral worker (target-side)
# ----------------------------------------------------------------------


def _handle_task_send_mode3(params: dict, metadata: dict, user_text: str) -> dict:
    """Called by server.py when worker_at='target'.

    Runs the local worker subprocess and returns a JSON-RPC compatible dict.
    """
    logger.info("[A2A] _handle_task_send_mode3 — spawning local worker")
    task_id = params.get("id", str(uuid.uuid4()))
    timeout = int(metadata.get("timeout") or params.get("timeout") or 300)

    hermes_home = _derive_hermes_home()
    name = params.get("name") or metadata.get("agent_name")
    agent_home = os.path.join(hermes_home, "profiles", name.lower())

    venv_python = os.environ.get("A2A_VENV_PYTHON", os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python"))
    worker_script = str(Path(__file__).parent / "_mode2_worker.py")
    plugin_dir = str(Path(__file__).parent)
    worker_params = {
        "agent_home": agent_home,
        "hermes_home": hermes_home,
        "message": user_text,
        "timeout": timeout,
    }
    env = {
        "HERMES_HOME": hermes_home,
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": plugin_dir + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    proc = None
    try:
        proc = subprocess.Popen(
            [venv_python, worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        register_worker(task_id, proc)
        stdout, stderr = proc.communicate(input=json.dumps(worker_params), timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.wait()
        return {
            "jsonrpc": "2.0",
            "id": task_id,
            "status": {"state": "failed"},
            "artifacts": [{"parts": [{"type": "text", "text": f"Mode 3 worker timed out after {timeout}s"}], "index": 0}],
        }
    finally:
        unregister_worker(task_id)
        from .worker_registry import cleanup_zombie_processes
        cleanup_zombie_processes()

    if proc.returncode == 0:
        try:
            worker_result = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "id": task_id,
                "status": {"state": "failed"},
                "artifacts": [{"parts": [{"type": "text", "text": f"Mode 3: non-JSON worker output: {stdout[:200]!r}"}], "index": 0}],
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
            "artifacts": [{"parts": [{"type": "text", "text": f"Mode 3 worker error: {stderr.strip()[:300]}"}], "index": 0}],
        }
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
    a2a_rpc = _transport(agent_info, "a2a_rpc")
    target_url = a2a_rpc.get("url", "") or agent_info.get("a2a_url", "")
    resolved_auth = a2a_rpc.get("auth") or {"type": "bearer", "token": agent_info.get("auth_token", "")}
    if not target_url:
        return {"error": f"Agent '{name}' has no a2a_url in vault"}

    try:
        target_url = _validate_target_url(target_url, allow_loopback=True)
    except ValueError as e:
        return {"error": str(e)}

    task_id = task_id or str(uuid.uuid4())
    hermes = build_hermes_metadata(route="worker", execution="remote_subprocess", isolation="target_profile")
    payload = build_task_send_payload(
        task_id=task_id,
        message=message,
        sender_name=os.getenv("A2A_AGENT_NAME", "hermes-agent"),
        intent="consultation",
        expected_action="reply",
        hermes=hermes,
    )
    payload["params"]["message"]["metadata"]["worker_at"] = "target"
    payload["params"]["message"]["metadata"]["timeout"] = timeout
    payload["params"]["timeout"] = timeout

    headers = _auth_headers(resolved_auth)

    try:
        result = _http_request("POST", target_url.rstrip("/"), json_body=payload, headers=headers, timeout=timeout)
    except Exception as exc:
        return {"error": f"Mode 3 HTTP error: {exc}"}

    err_msg = parse_json_rpc_error(result)
    if err_msg:
        return {"error": f"Mode 3 remote agent error: {err_msg}"}

    parsed = parse_task_result(result.get("result", {}) or {}, default_task_id=task_id)
    if parsed["response"]:
        return {
            "task_id": parsed["task_id"],
            "state": parsed["state"],
            "response": parsed["response"],
            "source": f"ephemeral:{name}",
            "mode": "3",
            "hermes": hermes,
        }

    return {"error": f"Mode 3: target returned status={parsed['state']}"}


# ----------------------------------------------------------------------
# Tool: task
# ----------------------------------------------------------------------


@_rate_limited
def handle_send_protocol_task(
    name: Optional[str] = None,
    url: Optional[str] = None,
    auth_token: Optional[str] = None,
    auth_type: Optional[str] = None,
    auth_header: Optional[str] = None,
    auth_value: Optional[str] = None,
    message: str = "",
    skill: Optional[str] = None,
    task_id: Optional[str] = None,
    intent: Optional[str] = None,
    expected_action: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    poll_interval: int = _POLL_INTERVAL,
    poll_attempts: int = _POLL_MAX_ATTEMPTS,
    user_task: Optional[str] = None,
) -> dict:
    """Send a task/message to a remote A2A agent.

    Posts to the target A2A RPC URL and polls for result.
    """
    if not message:
        return {"error": "'message' is required"}
    if not url and not name:
        return {"error": "Provide either 'url' or 'name'"}

    target_url = ""
    resolved_auth = _direct_auth(auth_token, auth_type, auth_header, auth_value)

    if name:
        agent_info = _resolve_agent_by_name(name)
        if not agent_info:
            return {"error": f"Agent '{name}' not found in vault registry"}
        a2a_rpc = _transport(agent_info, "a2a_rpc")
        target_url = a2a_rpc.get("url", "") or agent_info.get("a2a_url", "")
        resolved_auth = a2a_rpc.get("auth") or {"type": "bearer", "token": agent_info.get("auth_token", "")}
        allow_loopback = bool(a2a_rpc.get("allow_loopback") or agent_info.get("allow_loopback"))
        if skill:
            valid_skill, available_skills = validate_skill(agent_info, skill)
            if not valid_skill:
                return {"error": f"Skill '{skill}' not found for agent '{name}'", "available_skills": available_skills}
        if not target_url:
            return {"error": f"Agent '{name}' has no a2a_url in vault"}
    else:
        target_url = url
        allow_loopback = False

    try:
        target_url = _validate_target_url(target_url, allow_loopback=allow_loopback)
    except ValueError as e:
        return {"error": str(e)}

    timeout = int(timeout or _DEFAULT_TIMEOUT)
    poll_interval = int(poll_interval or _POLL_INTERVAL)
    poll_attempts = int(poll_attempts or _POLL_MAX_ATTEMPTS)
    task_id = task_id or str(uuid.uuid4())
    resolved_intent = intent or "consultation"
    resolved_action = expected_action or "reply"

    payload = build_task_send_payload(
        task_id=task_id,
        message=message,
        sender_name=os.getenv("A2A_AGENT_NAME", "hermes-agent"),
        intent=resolved_intent,
        expected_action=resolved_action,
        skill=skill,
        hermes=build_hermes_metadata(route="protocol", execution="remote_a2a"),
    )

    headers = _auth_headers(resolved_auth)

    response_text = ""
    task_state = "unknown"
    error_msg = ""
    rpc_result = {}

    try:
        result = _http_request("POST", target_url.rstrip("/"), json_body=payload, headers=headers, timeout=timeout)
    except ConnectionError:
        error_msg = f"Cannot connect to {target_url}"
    except TimeoutError:
        error_msg = f"Remote agent timed out after {timeout}s"
    except Exception as e:
        error_msg = f"Call failed: {e}"
    else:
        err_msg = parse_json_rpc_error(result)
        if err_msg:
            error_msg = f"Remote agent error: {err_msg}"
        else:
            rpc_result = result.get("result", {}) or {}
            parsed = parse_task_result(rpc_result, default_task_id=task_id)
            task_state = parsed["state"]
            remote_task_id = parsed["task_id"]

            if task_state in ("working", "submitted") and remote_task_id and poll_attempts > 0:
                poll_payload = build_task_get_payload(remote_task_id)
                poll_errors = 0
                for attempt in range(poll_attempts):
                    time.sleep(max(0, poll_interval))
                    try:
                        poll_result = _http_request("POST", target_url.rstrip("/"), json_body=poll_payload, headers=headers, timeout=timeout)
                        if parse_json_rpc_error(poll_result):
                            continue
                        poll_inner = poll_result.get("result", {}) or {}
                        poll_parsed = parse_task_result(poll_inner, default_task_id=remote_task_id)
                        poll_state = poll_parsed["state"]
                        if poll_state and poll_state != "unknown":
                            rpc_result = poll_inner
                            task_state = poll_state
                        if is_terminal_state(poll_state):
                            break
                    except Exception:
                        poll_errors += 1
                        continue

                if poll_errors == poll_attempts and not is_terminal_state(task_state):
                    return {"error": f"All {poll_attempts} poll attempts failed. Could not determine task result."}

            parsed = parse_task_result(rpc_result, default_task_id=task_id)
            response_text = parsed["response"]

    if error_msg:
        return {"error": error_msg}

    return {
        "task_id": rpc_result.get("id", task_id),
        "state": task_state,
        "response": response_text or "(no text response)",
        "source": target_url,
        "raw_result": rpc_result,
    }


@_rate_limited
def handle_run_local_agent_task(
    name: str = "",
    message: str = "",
    task_id: Optional[str] = None,
    timeout: int = 300,
    user_task: Optional[str] = None,
) -> dict:
    return _handle_call_mode2(name=name or "", message=message, task_id=task_id, timeout=int(timeout or 300))


@_rate_limited
def handle_run_remote_agent_task(
    name: str = "",
    message: str = "",
    task_id: Optional[str] = None,
    timeout: int = 300,
    user_task: Optional[str] = None,
) -> dict:
    return _handle_call_mode3(name=name or "", message=message, task_id=task_id, timeout=int(timeout or 300))


@_rate_limited
def handle_cancel_protocol_task(
    name: Optional[str] = None,
    url: Optional[str] = None,
    auth_token: Optional[str] = None,
    auth_type: Optional[str] = None,
    auth_header: Optional[str] = None,
    auth_value: Optional[str] = None,
    task_id: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
    user_task: Optional[str] = None,
) -> dict:
    if not task_id:
        return {"error": "'task_id' is required"}

    local_canceled = cancel_worker(task_id)

    if not url and not name:
        return {
            "task_id": task_id,
            "state": "canceled" if local_canceled else "unknown",
            "local_canceled": local_canceled,
            "response": "canceled local worker" if local_canceled else "no local worker found",
        }

    resolved_auth = _direct_auth(auth_token, auth_type, auth_header, auth_value)
    if name:
        agent_info = _resolve_agent_by_name(name)
        if not agent_info:
            return {"error": f"Agent '{name}' not found in vault registry"}
        a2a_rpc = _transport(agent_info, "a2a_rpc")
        target_url = a2a_rpc.get("url", "") or agent_info.get("a2a_url", "")
        resolved_auth = a2a_rpc.get("auth") or {"type": "bearer", "token": agent_info.get("auth_token", "")}
        allow_loopback = bool(a2a_rpc.get("allow_loopback") or agent_info.get("allow_loopback"))
        if not target_url:
            return {"error": f"Agent '{name}' has no a2a_url in vault"}
    else:
        target_url = url
        allow_loopback = False

    try:
        target_url = _validate_target_url(target_url, allow_loopback=allow_loopback)
    except ValueError as e:
        return {"error": str(e), "task_id": task_id, "local_canceled": local_canceled}

    try:
        result = _http_request(
            "POST",
            target_url.rstrip("/"),
            json_body=build_task_cancel_payload(task_id),
            headers=_auth_headers(resolved_auth),
            timeout=int(timeout or _DEFAULT_TIMEOUT),
        )
    except Exception as exc:
        return {"error": f"Cancel failed: {exc}", "task_id": task_id, "local_canceled": local_canceled}

    err_msg = parse_json_rpc_error(result)
    if err_msg:
        return {"error": f"Remote agent error: {err_msg}", "task_id": task_id, "local_canceled": local_canceled}
    parsed = parse_task_result(result.get("result", {}) or {}, default_task_id=task_id)
    return {
        "task_id": parsed["task_id"],
        "state": parsed["state"],
        "response": parsed["response"] or "(no text response)",
        "source": target_url,
        "local_canceled": local_canceled,
        "raw_result": parsed["raw_result"],
    }


# ----------------------------------------------------------------------
# Tool: session message
# ----------------------------------------------------------------------


def handle_send_session_message(args: dict = None, **kwargs) -> dict:
    """Send a session-aware message to a Hermes mesh peer.

    Two-part delivery:
    1. Webhook to target agent's Hermes gateway relay.
    2. Echo to sender's Telegram DM when configured.

    Routes the message to the target agent's gateway webhook so that the
    target gateway/config resolves it into the target Telegram session and
    invokes the target agent. Also echoes the same padded message to the
    sender's own Telegram DM for operator visibility when sender Telegram
    credentials are available.
    Auto-pads [a2a][from:<self>][to:<agent>][id:<uuid>][action:<action>][reply:<reply>] header.
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
    action = merged.get("action", "do")
    reply = merged.get("reply", "yes")
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
        # Agent not found - check if it's a /a2a_metrics command for local handling
        if os.getenv("A2A_METRICS_COMMAND_ENABLED", "false").lower() == "true":
            stripped_message = message.strip()
            if stripped_message.startswith("/a2a_metrics"):
                from .runtime_state import get_runtime_state as get_state
                metrics = get_state().get_metrics().get_metrics()
                return {
                    "state": "completed",
                    "response": _format_metrics_for_telegram(metrics),
                    "delivery": "command_response",
                }
        return {"error": f"Agent '{agent}' not found in vault registry"}

    # Validate webhook configuration before attempting delivery
    from .identity import get_raw_agent_identity
    raw_info = get_raw_agent_identity(agent)
    is_valid, validation_error = _validate_agent_webhook_config(raw_info)
    if not is_valid:
        return {"error": f"Agent '{agent}' webhook configuration invalid: {validation_error}"}

    # Optional webhook reachability check
    if os.getenv("A2A_WEBHOOK_REACHABILITY_CHECK", "false").lower() == "true":
        hermes_webhook = _transport(raw_info, "hermes_webhook")
        target_webhook_url = hermes_webhook.get("url", "") or (raw_info.get("webhook_url", "") if isinstance(raw_info, dict) else "")
        if target_webhook_url:
            try:
                target_webhook_url = _validate_target_url(target_webhook_url, allow_loopback=_is_local_fleet_agent(agent))
            except ValueError as e:
                return {"error": f"Agent '{agent}' webhook URL failed SSRF check: {e}"}
            reachability_timeout = int(os.getenv("A2A_WEBHOOK_REACHABILITY_TIMEOUT", "5"))
            is_reachable, reachability_error = _validate_webhook_reachable(target_webhook_url, reachability_timeout)
            if not is_reachable:
                return {"error": f"Agent '{agent}' webhook unreachable: {reachability_error}"}

    from_agent = os.getenv("A2A_AGENT_NAME", "hermes-agent")
    task_id = task_id or str(uuid.uuid4())
    msg_id = task_id
    hermes = build_hermes_metadata(route="session", execution="gateway_session", delivery="one_way", reply_mode="none")
    # Session messages are one-way by design. The envelope uses notification/acknowledge at protocol level.
    # The 2D CTA (action/reply) is semantic guidance for the recipient LLM, embedded in the text header.
    envelope = build_task_send_payload(
        task_id=task_id,
        message=message,
        sender_name=from_agent,
        intent="notification",
        expected_action="acknowledge",
        hermes=hermes,
    )
    header = f"[a2a][from:{from_agent}][to:{agent}][id:{msg_id}][action:{action}][reply:{reply}]"
    if ref:
        header += f"[ref:{ref}]"
    padded_message = f"{header} {message}"

    # Part 1: Webhook to target agent's gateway relay.
    hermes_webhook = _transport(raw_info, "hermes_webhook")
    target_webhook_url = hermes_webhook.get("url", "") or (raw_info.get("webhook_url", "") if isinstance(raw_info, dict) else "")
    # SSRF check: validate webhook URL before delivery, mirroring the check in _resolve_agent.
    # Do NOT assume the resolved agent card URL and the webhook URL share the same host.
    if target_webhook_url:
        try:
            target_webhook_url = _validate_target_url(target_webhook_url, allow_loopback=_is_local_fleet_agent(agent))
        except ValueError as e:
            return {"error": f"Agent '{agent}' webhook URL failed SSRF check: {e}"}
    import hashlib
    import hmac
    delivery_id = None
    if target_webhook_url:
        webhook_secret = _transport_auth_value(hermes_webhook, "secret") or (raw_info.get("webhook_secret", "") if isinstance(raw_info, dict) else "")
        if not webhook_secret:
            return {"error": "Webhook delivery failed"}
        body = json.dumps({"text": padded_message}, sort_keys=True)
        sig = hmac.new(
            webhook_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
        }
        # Make retry logic configurable via environment variables
        delivery_retries = int(os.getenv("A2A_WEBHOOK_DELIVERY_RETRIES", "3"))
        delivery_backoff = float(os.getenv("A2A_WEBHOOK_DELIVERY_BACKOFF", "1.0"))
        delivery_timeout = int(os.getenv("A2A_WEBHOOK_DELIVERY_TIMEOUT", "10"))
        
        import urllib.request
        import logging
        _logger = logging.getLogger(__name__)
        
        # Get metrics instance for recording
        from .runtime_state import get_runtime_state as get_state
        metrics = get_state().get_metrics()
        
        for attempt in range(delivery_retries):
            try:
                req = urllib.request.Request(
                    target_webhook_url,
                    data=body.encode(),
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=delivery_timeout) as resp:
                    result = json.loads(resp.read().decode())
                    delivery_id = result.get("delivery_id", "unknown")
                metrics.record_webhook_result(success=True)
                if attempt > 0:
                    _logger.info("[a2a_send_session_message] Webhook delivery succeeded on attempt %d/%d", attempt + 1, delivery_retries)
                break
            except Exception as exc:
                if attempt < delivery_retries - 1:
                    backoff = delivery_backoff * (2 ** attempt)
                    _logger.warning("[a2a_send_session_message] Webhook delivery attempt %d/%d failed: %s, retrying in %.1fs", attempt + 1, delivery_retries, exc, backoff)
                    time.sleep(backoff)
                else:
                    metrics.record_webhook_result(success=False)
                    _logger.error("[a2a_send_session_message] Webhook delivery failed after %d attempts: %s", delivery_retries, exc)
                    return {"error": f"Webhook to agent '{agent}' failed after {delivery_retries} attempts: {exc}"}
    else:
        return {"error": f"Agent '{agent}' has no webhook_url in vault"}

    # Part 2: Echo to sender's Telegram DM for visibility.
    # Can be disabled via A2A_DISABLE_SENDER_ECHO env var
    if os.getenv("A2A_DISABLE_SENDER_ECHO", "false").lower() == "true":
        echo_ok = False
    else:
        own_telegram_chat_id = own_vault.get("platforms", {}).get("telegram", {}).get("default_chat_id", "")
        echo_ok = False
        if own_bot_token and own_telegram_chat_id:
            try:
                import urllib.request
                url = f"https://api.telegram.org/bot{own_bot_token}/sendMessage"
                payload = json.dumps({
                    "chat_id": str(own_telegram_chat_id),
                    "text": padded_message,
                    "parse_mode": "HTML",
                }).encode()
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    echo_result = json.loads(resp.read().decode())
                if echo_result.get("ok"):
                    echo_ok = True
                else:
                    _logger.warning("[a2a_send_session_message] sender echo failed (non-fatal): %s", echo_result)
            except Exception as exc:
                _logger.warning("[a2a_send_session_message] sender echo failed (non-fatal): %s", exc)

    return {
        "task_id": task_id,
        "state": "completed",
        "status": "delivered",
        "delivery": "delivered",
        "reply_expected": reply == "yes",
        "message_id": delivery_id,
        "agent": agent,
        "gateway_delivery": True,
        "sender_echo": echo_ok,
        "hermes": hermes,
        "a2a_envelope": envelope,
    }


def handle_get_metrics(args=None, **kwargs) -> dict:
    """Get current A2A plugin metrics.

    Accepts an optional first positional arg (the args dict from dispatch)
    plus arbitrary kwargs. Neither is used — metrics are internal.
    """
    from .runtime_state import get_runtime_state as get_state

    return get_state().get_metrics().get_metrics()


def _format_metrics_for_telegram(metrics: dict) -> str:
    """Format metrics for Telegram display."""
    uptime = metrics.get("uptime_seconds", 0)
    uptime_hours = int(uptime // 3600)
    uptime_mins = int((uptime % 3600) // 60)
    uptime_secs = int(uptime % 60)

    webhook = metrics.get("webhook", {})
    tasks = metrics.get("tasks", {})
    queue = metrics.get("queue", {})

    lines = [
        "📊 A2A Metrics",
        "",
        f"⏱️ Uptime: {uptime_hours}h {uptime_mins}m {uptime_secs}s",
        "",
        "🔗 Webhook",
        f"Attempts: {webhook.get('attempts', 0)}",
        f"✅ Success: {webhook.get('successes', 0)} ({webhook.get('success_rate_percent', 0):.2f}%)",
        f"❌ Failed: {webhook.get('failures', 0)}",
        "",
        "📨 Tasks",
        f"Received: {tasks.get('received', 0)}",
        f"Completed: {tasks.get('completed', 0)}",
        f"Failed: {tasks.get('failed', 0)}",
        f"Canceled: {tasks.get('canceled', 0)}",
        "",
        f"📬 Queue: {queue.get('pending_count', 0)} pending",
    ]
    return "\n".join(lines)


def _handle_a2a_metrics_command(raw_args: str) -> str | None:
    """Telegram slash command handler for /a2a_metrics."""
    metrics = handle_get_metrics()
    return _format_metrics_for_telegram(metrics)
