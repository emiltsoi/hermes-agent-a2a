"""Vault resolution chain for A2A identity.

Resolution order (first valid wins):
1. Agent-level vault   — profiles/<agent>/a2a/vault.yaml (agent-specific isolation)
2. Explicit config     — hardcoded in config.yaml (last resort before env override)
3. Profile-level vault — profiles/<profile>/a2a/vault.yaml (shared context, not sacred)
4. Environment vars    — A2A_TELEGRAM_BOT_TOKEN, A2A_OWNER_CHAT_ID (deployment override — wins)

Env vars are the deployment override mechanism. They win over profile-level vault.
Profile vault is a convenience layer, not the source of truth.

Ehrlich & Lindstrom — HermesA2A 2026.
"""
import os
import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Standard env var names for A2A identity (v2)
ENV_BOT_TOKEN = "A2A_TELEGRAM_BOT_TOKEN"
ENV_OWNER_CHAT_ID = "A2A_OWNER_CHAT_ID"


def _resolve_env(value: str) -> Optional[str]:
    """Resolve ${ENV_VAR} interpolations in vault file values."""
    if not isinstance(value, str):
        return value
    pattern = r"\$\{([^}]+)\}"
    match = re.fullmatch(pattern, value.strip())
    if match:
        env_key = match.group(1)
        return os.environ.get(env_key, value)
    return value


class VaultResolver:
    """Resolves A2A identity via vault resolution chain."""

    def __init__(self, config: dict):
        self.config = config
        self.agent_path = config.get("agent_profile_path", "")
        self.profile_path = config.get("profile_path", "")
        self._vault_mode = config.get("a2a", {}).get("vault", "auto")

    def resolve(self) -> dict:
        """Resolve identity from the vault chain. Returns first valid result.

        Priority: agent vault > profile vault > env vars > explicit config.
        Env vars are the deployment override mechanism — they win over profile vault.
        Explicit config is the last resort (e.g. airgapped deployments with no vault files).

        If vault: "none" is set, skip all vault loading entirely — force explicit config only.
        """
        # vault: none — skip all vault resolution, force explicit config only
        if self._vault_mode == "none":
            logger.info("[VaultResolver] vault mode is none — skipping vault resolution")
            vault = self._from_explicit_config()
            if vault:
                return vault
            raise RuntimeError(
                "A2A vault error: vault mode is none but no explicit bot_token found in config.yaml. "
                "Set bot_token in config.yaml or remove vault: none."
            )

        # 1. Agent-level vault (agent-specific isolation)
        if self.agent_path:
            vault = self._load_vault(self.agent_path)
            if vault:
                logger.info("[VaultResolver] using agent-level vault")
                return vault

        # 2. Profile-level vault (shared context — convenience layer)
        if self.profile_path:
            vault = self._load_vault(self.profile_path)
            if vault:
                logger.info("[VaultResolver] using profile-level vault")
                return vault

        # 3. Environment variables — deployment override (wins over profile vault and explicit)
        vault = self._from_env()
        if vault:
            logger.info("[VaultResolver] using environment variables (deployment override)")
            return vault

        # 4. Explicit config — last resort (airgapped deployments with no vault files)
        vault = self._from_explicit_config()
        if vault:
            logger.info("[VaultResolver] using explicit config")
            return vault

        raise RuntimeError(
            "A2A vault error: no valid identity found. "
            f"Set {ENV_BOT_TOKEN} and {ENV_OWNER_CHAT_ID} env vars, "
            "configure a vault file, or add bot_token to config.yaml."
        )

    def _load_vault(self, base_path: str) -> Optional[dict]:
        """Load vault.yaml from a given base path."""
        vault_path = Path(base_path) / "a2a" / "vault.yaml"
        if not vault_path.exists():
            return None
        import yaml
        with open(vault_path) as f:
            raw = yaml.safe_load(f)
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
                "chat_id_resolver": "default_chat_id",
            }
        }

    def _from_explicit_config(self) -> Optional[dict]:
        """Read identity from hardcoded config.yaml values (last resort before env)."""
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
            }
        }

    def skip_vault_resolution(self) -> bool:
        """True if vault: none is set — skip vault entirely, force explicit config."""
        a2a = self.config.get("a2a", {})
        return a2a.get("vault", "auto") == "none"
