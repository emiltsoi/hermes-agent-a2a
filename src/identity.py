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
        """Load vault.yaml from a given base path."""
        vault_path = base_path / "a2a" / "vault.yaml"
        if not vault_path.exists():
            return None
        import yaml
        try:
            with open(vault_path) as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(
                f"A2A vault error: failed to parse {vault_path} — malformed YAML: {e}"
            ) from e
        except OSError as e:
            raise RuntimeError(
                f"A2A vault error: failed to read {vault_path}: {e}"
            ) from e
        if not raw:
            return None
        # Resolve ${ENV_VAR} interpolations in vault file values
        platforms = raw.get("platforms", {})
        for platform, cfg in platforms.items():
            if "bot_token" in cfg:
                cfg["bot_token"] = _resolve_env(cfg["bot_token"])
            if "default_chat_id" in cfg:
                cfg["default_chat_id"] = _resolve_env(cfg["default_chat_id"])
        return raw

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


def _agent_vault_path() -> Path:
    """Path to this agent's own vault: $HERMES_HOME/profiles/<agent_name>/a2a/."""
    agent_name = os.environ.get("A2A_AGENT_NAME", "").lower()
    if not agent_name:
        return Path("/nonexistent")
    return _hermes_home() / "profiles" / agent_name


def _profile_vault_path() -> Path:
    """Path to the current profile's vault: $HERMES_HOME/profiles/<profile>/a2a/.

    Profile is inferred from HERMES_HOME if it contains /profiles/, otherwise
    derived from the A2A_AGENT_NAME for backwards compat.
    """
    home = _hermes_home()
    # If HERMES_HOME ends with /profiles/<name>, use that
    parts = home.parts
    if "profiles" in parts:
        idx = parts.index("profiles")
        if idx + 1 < len(parts):
            profile = parts[idx + 1]
            return home.parent / "profiles" / profile
    # Fallback: use agent name as profile name
    agent_name = os.environ.get("A2A_AGENT_NAME", "").lower()
    if agent_name:
        return home / "profiles" / agent_name
    return home / "profiles" / "default"


def resolve_agent(name: str) -> Optional[dict]:
    """Look up an agent by name in the vault registry.

    Searches: $HERMES_HOME/profiles/<name>/a2a/vault.yaml → agents[name]
    Returns {a2a_url, auth_token, description} or None if not found.
    """
    if not name:
        return None
    agent_path = _hermes_home() / "profiles" / name.lower()
    vault_file = agent_path / "a2a" / "vault.yaml"
    if not vault_file.exists():
        return None
    import yaml
    try:
        with open(vault_file) as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return None
    agents = raw.get("agents", {})
    return agents.get(name.lower()) or agents.get(name)


def list_agents() -> list[dict]:
    """Return all agents in the vault registry.

    Searches: $HERMES_HOME/profiles/*/a2a/vault.yaml → agents[]
    Returns a list of {name, a2a_url, auth_token, description} dicts.
    """
    home = _hermes_home()
    profiles_dir = home / "profiles"
    if not profiles_dir.is_dir():
        return []
    agents = []
    seen = set()
    for profile_dir in profiles_dir.iterdir():
        if not profile_dir.is_dir():
            continue
        vault_file = profile_dir / "a2a" / "vault.yaml"
        if not vault_file.exists():
            continue
        import yaml
        try:
            with open(vault_file) as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            continue
        for agent_name, agent_data in raw.get("agents", {}).items():
            if agent_name in seen:
                continue
            seen.add(agent_name)
            agents.append({
                "name": agent_name,
                "a2a_url": agent_data.get("a2a_url", ""),
                "auth_token": agent_data.get("auth_token", ""),
                "description": agent_data.get("description", ""),
            })
    return agents
