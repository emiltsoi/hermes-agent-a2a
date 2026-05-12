"""AutoSourceBootstrap unit tests."""
from unittest.mock import MagicMock
from src.bootstrap import AutoSourceBootstrap


def test_explicit_source_wins():
    """Explicit source in route_cfg is used over vault defaults."""
    mock_vault = MagicMock()
    bootstrap = AutoSourceBootstrap({}, mock_vault)

    route_cfg = {
        "source": {
            "platform": "telegram",
            "chat_id": "999999999",
            "chat_type": "dm",
        }
    }

    result = bootstrap.bootstrap_route("my_route", route_cfg)

    assert result["platform"] == "telegram"
    assert result["chat_id"] == "999999999"
    mock_vault.resolve.assert_not_called()


def test_missing_source_uses_vault_defaults(monkeypatch):
    """No source block → vault defaults are applied."""
    mock_vault = MagicMock()
    mock_vault.resolve.return_value = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
                "default_chat_id": "123456789",
            }
        },
        "defaults": {
            "platform": "telegram",
            "chat_type": "dm",
        },
    }

    bootstrap = AutoSourceBootstrap({}, mock_vault)

    route_cfg = {}
    result = bootstrap.bootstrap_route("my_route", route_cfg)

    assert result["platform"] == "telegram"
    assert result["chat_type"] == "dm"
    assert result["chat_id"] == "123456789"


def test_bootstrap_routes_fills_all_routes():
    """bootstrap_routes fills in source for every route that lacks one."""
    mock_vault = MagicMock()
    mock_vault.resolve.return_value = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
                "default_chat_id": "123456789",
            }
        },
        "defaults": {
            "platform": "telegram",
            "chat_type": "dm",
        },
    }

    bootstrap = AutoSourceBootstrap({}, mock_vault)

    config = {
        "webhook": {
            "extra": {
                "routes": {
                    "route_one": {},
                    "route_two": {"source": {"platform": "telegram", "chat_id": "999999999"}},
                    "route_three": {},
                }
            }
        }
    }

    bootstrap.bootstrap_routes(config)

    # route_one and route_three should be filled from vault
    assert config["webhook"]["extra"]["routes"]["route_one"]["source"]["chat_id"] == "123456789"
    assert config["webhook"]["extra"]["routes"]["route_three"]["source"]["chat_id"] == "123456789"

    # route_two had explicit source → unchanged
    assert config["webhook"]["extra"]["routes"]["route_two"]["source"]["chat_id"] == "999999999"
