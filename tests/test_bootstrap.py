import pytest
from unittest.mock import MagicMock
from src.bootstrap import AutoSourceBootstrap


class TestAutoSourceBootstrap:
    def test_explicit_source_wins_over_everything(self):
        mock_vault = MagicMock()
        bootstrap = AutoSourceBootstrap({}, mock_vault)
        route_cfg = {
            "source": {"platform": "telegram", "chat_id": "123"}
        }
        result = bootstrap.bootstrap_route("test_route", route_cfg)
        assert result["chat_id"] == "123"
        # vault.resolve() should NOT be called
        mock_vault.resolve.assert_not_called()

    def test_inbound_context_wins_over_vault(self):
        mock_vault = MagicMock()
        mock_vault.resolve.return_value = {
            "platforms": {"telegram": {"default_chat_id": "999"}},
            "defaults": {"platform": "telegram", "chat_type": "dm"}
        }
        bootstrap = AutoSourceBootstrap({}, mock_vault)
        route_cfg = {}
        inbound = {"platform": "telegram", "chat_id": "555", "chat_type": "group"}
        result = bootstrap.bootstrap_route("test_route", route_cfg, inbound_context=inbound)
        assert result["chat_id"] == "555"
        assert result["chat_type"] == "group"

    def test_vault_defaults_when_no_source_no_inbound(self):
        mock_vault = MagicMock()
        mock_vault.resolve.return_value = {
            "platforms": {"telegram": {"default_chat_id": "777"}},
            "defaults": {"platform": "telegram", "chat_type": "dm"}
        }
        bootstrap = AutoSourceBootstrap({}, mock_vault)
        result = bootstrap.bootstrap_route("test_route", {})
        assert result["chat_id"] == "777"

    def test_bootstrap_routes_fills_in_missing_source(self):
        mock_vault = MagicMock()
        mock_vault.resolve.return_value = {
            "platforms": {"telegram": {"default_chat_id": "111"}},
            "defaults": {"platform": "telegram", "chat_type": "dm"}
        }
        config = {
            "webhook": {
                "extra": {
                    "routes": {
                        "trigger": {},  # no source
                    }
                }
            }
        }
        bootstrap = AutoSourceBootstrap(config, mock_vault)
        bootstrap.bootstrap_routes(config)
        assert config["webhook"]["extra"]["routes"]["trigger"]["source"]["chat_id"] == "111"

    def test_bootstrap_routes_with_real_vault(self, tmp_vault_dir):
        """Integration test: tmp vault file + real VaultResolver + bootstrap_route."""
        from src.identity import VaultResolver

        # Write a real vault file
        vault_file = tmp_vault_dir / "vault.yaml"
        vault_file.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    default_chat_id: '999'\n"
            "defaults:\n"
            "  platform: telegram\n"
            "  chat_type: dm\n"
        )

        # Create VaultResolver with agent-level vault pointing to tmp_vault_dir
        # vault_dir = testprofile/a2a/, agent path = testprofile/
        agent_path = tmp_vault_dir.parent
        vault = VaultResolver(config={"agent_profile_path": str(agent_path)})

        # bootstrap_route with no explicit source, no inbound — should read from vault
        bootstrap = AutoSourceBootstrap({}, vault)
        result = bootstrap.bootstrap_route("test_route", {})

        assert result["chat_id"] == "999"
        assert result["platform"] == "telegram"
        assert result["chat_type"] == "dm"

    def test_inbound_context_propagates_through_bootstrap_routes(self):
        """bootstrap_routes with inbound_context: all routes without explicit source get inbound values."""
        mock_vault = MagicMock()
        bootstrap = AutoSourceBootstrap({}, mock_vault)

        config = {
            "webhook": {
                "extra": {
                    "routes": {
                        "route_a": {},           # no source — should get inbound
                        "route_b": {},           # no source — should get inbound
                        "route_c": {"source": {"platform": "discord", "chat_id": "explicit"}},  # explicit wins
                    }
                }
            }
        }

        inbound = {"platform": "telegram", "chat_id": "111", "chat_type": "group", "user_id": "user1"}
        bootstrap.bootstrap_routes(config, inbound_context=inbound)

        # route_a and route_b should be bootstrapped from inbound
        assert config["webhook"]["extra"]["routes"]["route_a"]["source"]["chat_id"] == "111"
        assert config["webhook"]["extra"]["routes"]["route_a"]["source"]["platform"] == "telegram"
        assert config["webhook"]["extra"]["routes"]["route_a"]["source"]["chat_type"] == "group"
        assert config["webhook"]["extra"]["routes"]["route_b"]["source"]["chat_id"] == "111"
        # route_c has explicit — vault.resolve should NOT be called
        assert config["webhook"]["extra"]["routes"]["route_c"]["source"]["chat_id"] == "explicit"
        mock_vault.resolve.assert_not_called()
