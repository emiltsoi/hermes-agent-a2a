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
