"""VaultResolver unit tests."""
import os
import pytest

import src.identity as identity_module
from src.identity import VaultResolver, resolve_agent, list_agents


def test_resolves_from_agent_vault(monkeypatch, tmp_path):
    """Agent vault yaml is loaded and resolved correctly."""
    vault_dir = tmp_path / "profiles" / "testagent" / "a2a"
    vault_dir.mkdir(parents=True)

    (vault_dir / "vault.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    bot_token: test-token-placeholder\n"
        "    default_chat_id: '123456789'\n"
    )

    monkeypatch.setenv("A2A_AGENT_NAME", "testagent")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    resolver = VaultResolver({})
    result = resolver.resolve()

    assert result["platforms"]["telegram"]["bot_token"] == "test-token-placeholder"
    assert result["platforms"]["telegram"]["default_chat_id"] == "123456789"


def test_falls_back_to_profile_vault(monkeypatch, tmp_path):
    """Agent vault absent → profile vault is used."""
    profile_dir = tmp_path / "profiles" / "default" / "a2a"
    profile_dir.mkdir(parents=True)

    (profile_dir / "vault.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    bot_token: profile-token-placeholder\n"
        "    default_chat_id: '123456789'\n"
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    resolver = VaultResolver({})
    result = resolver.resolve()

    assert result["platforms"]["telegram"]["bot_token"] == "profile-token-placeholder"


def test_falls_back_to_env_vars(monkeypatch, tmp_path):
    """Both vaults absent → env vars are used."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("A2A_TELEGRAM_BOT_TOKEN", "env-token-placeholder")
    monkeypatch.setenv("A2A_OWNER_CHAT_ID", "123456789")

    resolver = VaultResolver({})
    result = resolver.resolve()

    assert result["platforms"]["telegram"]["bot_token"] == "env-token-placeholder"
    assert result["platforms"]["telegram"]["default_chat_id"] == "123456789"


def test_resolve_agent_returns_correct_dict(monkeypatch, tmp_path):
    """resolve_agent reads agents registry and returns correct dict."""
    vault_dir = tmp_path / "profiles" / "remoteagent" / "a2a"
    vault_dir.mkdir(parents=True)

    (vault_dir / "vault.yaml").write_text(
        "agents:\n"
        "  remoteagent:\n"
        "    a2a_url: https://example.com/a2a\n"
        "    auth_token: agent-auth-token\n"
        "    description: A remote agent\n"
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = resolve_agent("remoteagent")

    assert result is not None
    assert result["a2a_url"] == "https://example.com/a2a"
    assert result["auth_token"] == "agent-auth-token"
    assert result["description"] == "A remote agent"


def test_list_agents_returns_all(monkeypatch, tmp_path):
    """list_agents walks all profile vaults and returns every agent."""
    # Agent 1 vault
    d1 = tmp_path / "profiles" / "alice" / "a2a"
    d1.mkdir(parents=True)
    (d1 / "vault.yaml").write_text(
        "agents:\n"
        "  alice:\n"
        "    a2a_url: https://alice.example.com/a2a\n"
        "    auth_token: alice-token\n"
        "    description: Alice agent\n"
    )

    # Agent 2 vault (different profile)
    d2 = tmp_path / "profiles" / "bob" / "a2a"
    d2.mkdir(parents=True)
    (d2 / "vault.yaml").write_text(
        "agents:\n"
        "  bob:\n"
        "    a2a_url: https://bob.example.com/a2a\n"
        "    auth_token: bob-token\n"
        "    description: Bob agent\n"
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    agents = list_agents()

    assert len(agents) == 2
    names = {a["name"] for a in agents}
    assert "alice" in names
    assert "bob" in names
