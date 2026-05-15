"""Vault resolution chain for A2A identity — fleet-agnostic, HERMES_HOME-aware.

Resolution order (first valid wins):
1. Agent-level vault   — $HERMES_HOME/profiles/<agent>/a2a/vault.yaml
2. Profile-level vault — $HERMES_HOME/profiles/<profile>/a2a/vault.yaml
3. Environment vars    — A2A_TELEGRAM_BOT_TOKEN, A2A_OWNER_CHAT_ID (deployment override)

Ehrlich & Lindstrom — HermesA2A 2026.
"""
import os
import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENV_BOT_TOKEN = "A2A_TELEGRAM_BOT_TOKEN"
ENV_OWNER_CHAT_ID = "A2A_OWNER_CHAT_ID"


def _resolve_env(value: str) -> Optional[str]:
    """Resolve ${ENV_VAR} interpolations in vault file values."""
    if not isinstance(value, str):
        return value
    pattern = r"^\$\{([^}]+)\}$"
    match = re.fullmatch(pattern, value.strip())
    if match:
        env_key = match.group(1)
        return os.environ.get(env_key, value)
    return value


def _hermes_home() -> Path:
    """Return HERMES_HOME as a Path, defaulting to ~/.hermes."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _hermes_root() -> Path:
    home = _hermes_home()
    parts = home.parts
    if "profiles" in parts:
        idx = parts.index("profiles")
        return Path(*parts[:idx]) if idx > 0 else Path("/")
    return home


def _fleet_root() -> Path:
    return Path(os.environ.get("A2A_VAULT_PATH", str(_hermes_root() / "fleet")))


def _load_a2a_agents() -> dict:
    """Load A2A agent registry from ~/.hermes/config.yaml.

    Returns a dict keyed by agent name (lowercase), e.g.:
      {"isa": {"url": "http://127.0.0.1:41808", "auth_token": "..."}}
    """
    try:
        import yaml
        cfg_path = _hermes_home() / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            agents = cfg.get("a2a", {}).get("agents", []) or []
            return {a.get("name", "").lower(): a for a in agents if a.get("name")}
    except Exception:
        pass
    return {}


def _normalize_identity(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    data = dict(raw)
    if "id" not in data and data.get("name"):
        data["id"] = str(data.get("name")).lower()
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
    telegram = platforms.get("telegram")
    if not isinstance(telegram, dict):
        telegram = {}
    if not telegram.get("bot_token") and data.get("telegram_bot_token"):
        telegram["bot_token"] = data.get("telegram_bot_token")
    if not telegram.get("default_chat_id") and data.get("telegram_chat_id"):
        telegram["default_chat_id"] = data.get("telegram_chat_id")
    if telegram:
        platforms["telegram"] = telegram
        data["platforms"] = platforms
    transports = data.get("transports")
    if not isinstance(transports, dict):
        transports = {}
    a2a_rpc = transports.get("a2a_rpc")
    if not isinstance(a2a_rpc, dict):
        a2a_rpc = {}
    hermes_webhook = transports.get("hermes_webhook")
    if not isinstance(hermes_webhook, dict):
        hermes_webhook = {}
    agent_card = transports.get("agent_card")
    if not isinstance(agent_card, dict):
        agent_card = {}
    if not a2a_rpc.get("url") and data.get("a2a_url"):
        a2a_rpc["url"] = data.get("a2a_url")
    if not a2a_rpc.get("protocol"):
        a2a_rpc["protocol"] = "google-a2a"
    if not a2a_rpc.get("auth"):
        auth = {}
        if data.get("auth_token"):
            auth = {"type": "bearer", "token": data.get("auth_token")}
        a2a_rpc["auth"] = auth or {"type": "none"}
    if a2a_rpc.get("url"):
        transports["a2a_rpc"] = a2a_rpc
        data["a2a_url"] = a2a_rpc.get("url", "")
        a2a_auth = a2a_rpc.get("auth") or {}
        if not data.get("auth_token"):
            data["auth_token"] = a2a_auth.get("token", "")
    if not hermes_webhook.get("url") and data.get("webhook_url"):
        hermes_webhook["url"] = data.get("webhook_url")
    if not hermes_webhook.get("protocol"):
        hermes_webhook["protocol"] = "hermes-webhook"
    if not hermes_webhook.get("auth"):
        auth = {}
        if data.get("webhook_secret"):
            auth = {"type": "hmac-sha256", "secret": data.get("webhook_secret")}
        hermes_webhook["auth"] = auth or {"type": "none"}
    if hermes_webhook.get("url"):
        transports["hermes_webhook"] = hermes_webhook
        data["webhook_url"] = hermes_webhook.get("url", "")
        webhook_auth = hermes_webhook.get("auth") or {}
        if not data.get("webhook_secret"):
            data["webhook_secret"] = webhook_auth.get("secret", "")
    if agent_card.get("url"):
        if not agent_card.get("auth"):
            agent_card["auth"] = a2a_rpc.get("auth") or {"type": "none"}
        transports["agent_card"] = agent_card
    if transports:
        data["transports"] = transports
    if "defaults" not in data:
        data["defaults"] = {"platform": "telegram", "chat_type": "dm"}
    return data


def _load_yaml_file(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    import yaml
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RuntimeError(
            f"A2A identity error: failed to parse {path} — malformed YAML: {e}"
        ) from e
    except OSError as e:
        raise RuntimeError(
            f"A2A identity error: failed to read {path}: {e}"
        ) from e
    if not raw:
        return None
    data = _normalize_identity(raw)
    for transport in data.get("transports", {}).values():
        auth = transport.get("auth")
        if isinstance(auth, dict):
            if "token" in auth:
                auth["token"] = _resolve_env(auth["token"])
            if "secret" in auth:
                auth["secret"] = _resolve_env(auth["secret"])
            if "value" in auth:
                auth["value"] = _resolve_env(auth["value"])
            if auth.get("token_env") and not auth.get("token"):
                auth["token"] = os.environ.get(auth["token_env"], "")
            if auth.get("secret_env") and not auth.get("secret"):
                auth["secret"] = os.environ.get(auth["secret_env"], "")
            if auth.get("value_env") and not auth.get("value"):
                auth["value"] = os.environ.get(auth["value_env"], "")
    data = _normalize_identity(data)
    platforms = data.get("platforms", {})
    for platform, cfg in platforms.items():
        if "bot_token" in cfg:
            cfg["bot_token"] = _resolve_env(cfg["bot_token"])
        if "default_chat_id" in cfg:
            cfg["default_chat_id"] = _resolve_env(cfg["default_chat_id"])
    return data


class VaultResolver:
    """Resolves A2A identity via vault resolution chain."""

    def __init__(self, config: dict):
        self.config = config
        self._vault_mode = config.get("a2a", {}).get("vault", "auto")

    def resolve(self) -> dict:
        """Resolve identity from the vault chain.

        Priority: agent vault > profile vault > env vars > explicit config.
        Env vars are the deployment override — they win over profile vault.
        Explicit config is the last resort (airgapped deployments with no vault files).
        """
        if self._vault_mode == "none":
            logger.info("[VaultResolver] vault mode is none — skipping vault resolution")
            vault = self._from_explicit_config()
            if vault:
                return vault
            raise RuntimeError(
                "A2A vault error: vault mode is none but no explicit bot_token found. "
                "Set bot_token in config.yaml or remove vault: none."
            )

        # 1. Agent-level vault
        vault = self._load_vault(_agent_vault_path())
        if vault:
            logger.info("[VaultResolver] using agent-level vault")
            return vault

        # 2. Profile-level vault
        vault = self._load_vault(_profile_vault_path())
        if vault:
            logger.info("[VaultResolver] using profile-level vault")
            return vault

        # 3. Environment variables — deployment override
        vault = self._from_env()
        if vault:
            logger.info("[VaultResolver] using environment variables (deployment override)")
            return vault

        # 4. Explicit config — last resort
        vault = self._from_explicit_config()
        if vault:
            logger.info("[VaultResolver] using explicit config")
            return vault

        raise RuntimeError(
            f"A2A vault error: no valid identity found. "
            f"Set {ENV_BOT_TOKEN} and {ENV_OWNER_CHAT_ID} env vars, "
            "configure a vault file, or add bot_token to config.yaml."
        )

    def _load_vault(self, base_path: Path) -> Optional[dict]:
        """Load identity.yaml or vault.yaml from a given base path."""
        for candidate in (
            base_path / "identity.yaml",
            base_path / "vault.yaml",
            base_path / "a2a" / "identity.yaml",
            base_path / "a2a" / "vault.yaml",
        ):
            data = _load_yaml_file(candidate)
            if data:
                return data
        return None

    def _from_env(self) -> Optional[dict]:
        """Read identity directly from environment variables (deployment override)."""
        token = os.environ.get(ENV_BOT_TOKEN)
        chat_id = os.environ.get(ENV_OWNER_CHAT_ID)
        if not token:
            return None
        return {
            "platforms": {
                "telegram": {
                    "bot_token": token,
                    "default_chat_id": chat_id,
                }
            },
            "defaults": {
                "platform": "telegram",
                "chat_type": "dm",
            },
        }

    def _from_explicit_config(self) -> Optional[dict]:
        """Read identity from config.yaml values (last resort before env)."""
        a2a = self.config.get("a2a", {})
        if not a2a:
            return None
        token = a2a.get("bot_token", "").strip()
        chat_id = a2a.get("default_chat_id", "").strip()
        if not token:
            return None
        return {
            "platforms": {
                "telegram": {
                    "bot_token": token,
                    "default_chat_id": chat_id,
                }
            },
            "defaults": {
                "platform": "telegram",
                "chat_type": "dm",
            },
        }

    def skip_vault_resolution(self) -> bool:
        """True if vault: none is set — skip vault entirely."""
        return self.config.get("a2a", {}).get("vault", "auto") == "none"

    def resolve_agent(self, name: str) -> Optional[dict]:
        return resolve_agent(name)

    def list_agents(self) -> list[dict]:
        return list_agents()


def _agent_vault_path() -> Path:
    """Path to this agent's own vault: $HERMES_HOME/a2a/vault.yaml.

    When HERMES_HOME points to a profile dir (e.g. .../profiles/britney),
    the vault lives directly under HERMES_HOME/a2a/, not HERMES_HOME/profiles/britney/a2a/.
    """
    agent_name = os.environ.get("A2A_AGENT_NAME", "").lower()
    if not agent_name:
        return Path("/nonexistent")
    return _fleet_root() / "a2a" / "agents" / agent_name


def _profile_vault_path() -> Path:
    """Path to the current profile's vault: $HERMES_HOME/a2a/vault.yaml.

    Profile is inferred from HERMES_HOME if it ends with /profiles/<name>.
    When HERMES_HOME points to a profile dir, the vault is at HERMES_HOME/a2a/.
    """
    home = _hermes_home()
    # If HERMES_HOME ends with /profiles/<name>, vault is directly under HERMES_HOME/a2a
    parts = home.parts
    if "profiles" in parts:
        idx = parts.index("profiles")
        if idx + 1 < len(parts):
            profile = parts[idx + 1]
            if home.name == profile:
                # HERMES_HOME is the profile dir — vault is at HERMES_HOME/a2a
                return home / "a2a"
    # Fallback: use agent name as profile name
    agent_name = os.environ.get("A2A_AGENT_NAME", "").lower()
    if agent_name:
        return home / "profiles" / agent_name / "a2a"
    return home / "profiles" / "default" / "a2a"


def resolve_agent(name: str) -> Optional[dict]:
    """Look up an agent by name in the vault registry.

    Searches: $HERMES_HOME/profiles/<name>/a2a/vault.yaml → agents[name]
    Returns {a2a_url, auth_token, description} or None if not found.
    """
    if not name:
        return None
    agent_key = name.lower()
    identity_file = _fleet_root() / "a2a" / "agents" / agent_key / "identity.yaml"
    try:
        identity = _load_yaml_file(identity_file)
    except RuntimeError:
        raise
    except Exception:
        identity = None
    if identity:
        return {
            "name": identity.get("name", ""),
            "a2a_url": identity.get("a2a_url", ""),
            "description": identity.get("description", ""),
            "role": identity.get("role", ""),
        }
    agent_vault = _hermes_root() / "profiles" / agent_key / "a2a" / "vault.yaml"
    try:
        raw = _load_yaml_file(agent_vault) or {}
    except Exception:
        return None
    agents = raw.get("agents", {})
    agent_entry = agents.get(agent_key) or agents.get(name)
    if not agent_entry:
        return None
    # Also surface root-level webhook fields if present (not nested under agents[name])
    return {
        "a2a_url": agent_entry.get("a2a_url", ""),
        "description": agent_entry.get("description", ""),
        "role": agent_entry.get("role", ""),
    }


def list_agents() -> list[dict]:
    """Return all agents in the vault registry.

    Searches: $HERMES_HOME/profiles/*/a2a/vault.yaml → agents[]
    Returns a list of {name, a2a_url, description, role} dicts — no credentials.
    """
    profiles_dir = _fleet_root() / "a2a" / "agents"
    if not profiles_dir.is_dir():
        profiles_dir = _hermes_root() / "profiles"
        if not profiles_dir.is_dir():
            return []
    agents = []
    seen = set()
    for profile_dir in profiles_dir.iterdir():
        if not profile_dir.is_dir():
            continue
        try:
            raw = _load_yaml_file(profile_dir / "identity.yaml")
            if raw:
                agent_name = str(raw.get("name") or profile_dir.name).lower()
                if agent_name in seen:
                    continue
                seen.add(agent_name)
                agents.append({
                    "name": agent_name,
                    "a2a_url": raw.get("a2a_url", ""),
                    "description": raw.get("description", raw.get("role", "")),
                    "role": raw.get("role", ""),
                })
                continue
            raw = _load_yaml_file(profile_dir / "a2a" / "vault.yaml") or {}
        except Exception:
            continue
        for agent_name, agent_data in raw.get("agents", {}).items():
            if agent_name in seen:
                continue
            seen.add(agent_name)
            agents.append({
                "name": agent_name,
                "a2a_url": agent_data.get("a2a_url", ""),
                "description": agent_data.get("description", ""),
                "role": agent_data.get("role", ""),
            })
    return agents
