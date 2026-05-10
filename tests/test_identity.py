import pytest
import os
from pathlib import Path
from src.identity import VaultResolver, _resolve_env, ENV_BOT_TOKEN, ENV_OWNER_CHAT_ID


class TestResolveEnv:
    def test_resolve_env_single_var(self, monkeypatch):
        monkeypatch.setenv(ENV_BOT_TOKEN, "123456:ABC")
        assert _resolve_env(f"${{{ENV_BOT_TOKEN}}}") == "123456:ABC"

    def test_resolve_env_unset_returns_original(self):
        assert _resolve_env("${NONEXISTENT_VAR}") == "${NONEXISTENT_VAR}"

    def test_resolve_env_non_string_passthrough(self):
        assert _resolve_env(None) is None
        assert _resolve_env(123) == 123


class TestVaultResolverAgentLevel:
    """Tests for agent-level vault priority."""

    def test_agent_vault_used_when_only_it_exists(self, monkeypatch, tmp_path):
        """Agent vault used when no profile vault exists."""
        agent = tmp_path / "agent"
        vault_dir = agent / "a2a"
        vault_dir.mkdir(parents=True)
        (vault_dir / "vault.yaml").write_text("""
platforms:
  telegram:
    bot_token: "agent_only_token"
    default_chat_id: "999"
defaults:
  platform: telegram
  chat_type: dm
""")
        resolver = VaultResolver({
            "agent_profile_path": str(agent),
            "profile_path": str(tmp_path / "nonexistent"),
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "agent_only_token"

    def test_agent_vault_wins_over_profile_vault(self, monkeypatch, tmp_path):
        """Agent vault has highest priority over profile vault."""
        profile = tmp_path / "profile"
        agent = tmp_path / "agent"
        for d in [profile / "a2a", agent / "a2a"]:
            d.mkdir(parents=True)
        (profile / "a2a" / "vault.yaml").write_text("""
platforms:
  telegram:
    bot_token: "profile_token"
    default_chat_id: "111"
defaults:
  platform: telegram
  chat_type: dm
""")
        (agent / "a2a" / "vault.yaml").write_text("""
platforms:
  telegram:
    bot_token: "agent_token"
    default_chat_id: "222"
defaults:
  platform: telegram
  chat_type: dm
""")
        resolver = VaultResolver({
            "agent_profile_path": str(agent),
            "profile_path": str(profile),
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "agent_token"
        assert identity["platforms"]["telegram"]["default_chat_id"] == "222"


class TestVaultResolverFile:
    """Tests for profile-level vault and vault file loading."""

    def test_vault_file_loads_and_resolves_env(self, monkeypatch, tmp_path):
        profile = tmp_path / "profile"
        vault_dir = profile / "a2a"
        vault_dir.mkdir(parents=True)
        (vault_dir / "vault.yaml").write_text(f"""
platforms:
  telegram:
    bot_token: "${{{ENV_BOT_TOKEN}}}"
    default_chat_id: "${{{ENV_OWNER_CHAT_ID}}}"
defaults:
  platform: telegram
  chat_type: dm
""")
        monkeypatch.setenv(ENV_BOT_TOKEN, "555:XYZ")
        monkeypatch.setenv(ENV_OWNER_CHAT_ID, "777")
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(profile),
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "555:XYZ"
        assert identity["platforms"]["telegram"]["default_chat_id"] == "777"


class TestVaultResolverEnv:
    """Tests for environment variable resolution."""

    def test_env_resolution(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_BOT_TOKEN, "123456:ABC")
        monkeypatch.setenv(ENV_OWNER_CHAT_ID, "999")
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(tmp_path / "nonexistent"),
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "123456:ABC"
        assert identity["platforms"]["telegram"]["default_chat_id"] == "999"

    def test_no_vault_no_env_no_config_raises(self, tmp_path):
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(tmp_path / "nonexistent"),
        })
        with pytest.raises(RuntimeError, match="no valid identity found"):
            resolver.resolve()

    def test_explicit_config_is_last_resort(self, monkeypatch, tmp_path):
        """Explicit config only used when no vault files and no env vars exist."""
        profile = tmp_path / "profile"
        vault_dir = profile / "a2a"
        vault_dir.mkdir(parents=True)
        (vault_dir / "vault.yaml").write_text("""
platforms:
  telegram:
    bot_token: "profile_vault_token"
    default_chat_id: "111"
defaults:
  platform: telegram
  chat_type: dm
""")
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(profile),
            "a2a": {"bot_token": "explicit_token", "default_chat_id": "222"},
        })
        identity = resolver.resolve()
        # Profile vault wins over explicit config (explicit is last resort)
        assert identity["platforms"]["telegram"]["bot_token"] == "profile_vault_token"

    def test_env_vars_are_reached_after_no_profile_vault(self, monkeypatch, tmp_path):
        """Env vars — deployment override, reached when no vault files exist."""
        monkeypatch.setenv(ENV_BOT_TOKEN, "env_override_token")
        monkeypatch.setenv(ENV_OWNER_CHAT_ID, "333")
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(tmp_path / "nonexistent"),
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "env_override_token"
        assert identity["platforms"]["telegram"]["default_chat_id"] == "333"

    def test_explicit_config_is_last_resort_only_when_no_vault_and_no_env(self, monkeypatch, tmp_path):
        """Explicit config — last resort when no vault files and no env vars."""
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(tmp_path / "nonexistent"),
            "a2a": {"bot_token": "explicit_token", "default_chat_id": "222"},
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "explicit_token"


class TestVaultModeNone:
    """Tests for vault: none mode — skip all vault resolution."""

    def test_vault_none_skips_vault_and_uses_explicit_config(self, monkeypatch, tmp_path):
        """vault: none — skip vault loading, force explicit config."""
        profile = tmp_path / "profile"
        vault_dir = profile / "a2a"
        vault_dir.mkdir(parents=True)
        (vault_dir / "vault.yaml").write_text("""
platforms:
  telegram:
    bot_token: "profile_vault_token"
    default_chat_id: "111"
defaults:
  platform: telegram
  chat_type: dm
""")
        # vault: none should skip the profile vault and use explicit config
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(profile),
            "a2a": {"vault": "none", "bot_token": "explicit_token", "default_chat_id": "222"},
        })
        identity = resolver.resolve()
        assert identity["platforms"]["telegram"]["bot_token"] == "explicit_token"

    def test_vault_none_no_explicit_config_raises(self, monkeypatch, tmp_path):
        """vault: none with no explicit config — error."""
        profile = tmp_path / "profile"
        vault_dir = profile / "a2a"
        vault_dir.mkdir(parents=True)
        (vault_dir / "vault.yaml").write_text("""
platforms:
  telegram:
    bot_token: "profile_vault_token"
    default_chat_id: "111"
defaults:
  platform: telegram
  chat_type: dm
""")
        resolver = VaultResolver({
            "agent_profile_path": str(tmp_path / "nonexistent"),
            "profile_path": str(profile),
            "a2a": {"vault": "none"},  # no bot_token
        })
        with pytest.raises(RuntimeError, match="vault mode is none but no explicit bot_token"):
            resolver.resolve()
