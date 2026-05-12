import pytest
from src.schema import validate_config, apply_defaults, A2A_SCHEMA


class TestValidateConfig:
    """Required fields and type validation."""

    def test_missing_default_chat_id_raises(self):
        """Missing platforms.telegram.default_chat_id raises RuntimeError."""
        config = {
            "platforms": {
                "telegram": {"bot_token": "123:ABC"}
            }
        }
        with pytest.raises(RuntimeError) as exc:
            validate_config(config)
        assert "default_chat_id" in str(exc.value)

    def test_missing_bot_token_raises(self):
        """Missing platforms.telegram.bot_token raises RuntimeError."""
        config = {
            "platforms": {
                "telegram": {"default_chat_id": "555"}
            }
        }
        with pytest.raises(RuntimeError) as exc:
            validate_config(config)
        assert "bot_token" in str(exc.value)

    def test_invalid_type_enabled_string_rejected(self):
        """enabled: 'yes' (string instead of bool) raises RuntimeError."""
        config = {
            "enabled": "yes",  # wrong type
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            }
        }
        with pytest.raises(RuntimeError) as exc:
            validate_config(config)
        assert "enabled" in str(exc.value)
        assert "str" in str(exc.value)  # Python type name

    def test_invalid_type_bot_token_number_rejected(self):
        """bot_token: 12345 (number instead of string) raises RuntimeError."""
        config = {
            "platforms": {
                "telegram": {"bot_token": 12345, "default_chat_id": "555"}  # wrong type
            }
        }
        with pytest.raises(RuntimeError) as exc:
            validate_config(config)
        assert "bot_token" in str(exc.value)
        assert "int" in str(exc.value)  # Python type name

    def test_valid_config_passes(self):
        """Full valid config passes with no errors or warnings."""
        config = {
            "enabled": True,
            "vault": "auto",
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            },
            "defaults": {
                "platform": "telegram",
                "chat_type": "dm"
            }
        }
        valid, warnings = validate_config(config)
        assert valid is True
        assert warnings == []


class TestApplyDefaults:
    """Default value application."""

    def test_apply_defaults_preserves_explicit_values(self):
        """Explicit values are not overwritten by defaults."""
        config = {
            "enabled": False,
            "vault": "none",
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            }
        }
        result = apply_defaults(config)
        assert result["enabled"] is False
        assert result["vault"] == "none"
        assert result["platforms"]["telegram"]["bot_token"] == "123:ABC"

    def test_apply_defaults_adds_enabled_true(self):
        """enabled defaults to True when absent."""
        config = {
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            }
        }
        result = apply_defaults(config)
        assert result["enabled"] is True

    def test_apply_defaults_adds_vault_auto(self):
        """vault defaults to 'auto' when absent."""
        config = {
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            }
        }
        result = apply_defaults(config)
        assert result["vault"] == "auto"

    def test_apply_defaults_adds_nested_defaults(self):
        """defaults subfields get correct nested defaults."""
        config = {
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            }
        }
        result = apply_defaults(config)
        assert result["defaults"]["platform"] == "telegram"
        assert result["defaults"]["chat_type"] == "dm"
        assert result["defaults"]["chat_id_resolver"] == "default_chat_id"

    def test_apply_defaults_partial_defaults(self):
        """Partial defaults block is merged, not replaced."""
        config = {
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            },
            "defaults": {
                "platform": "telegram"
                # chat_type and chat_id_resolver absent
            }
        }
        result = apply_defaults(config)
        assert result["defaults"]["platform"] == "telegram"
        assert result["defaults"]["chat_type"] == "dm"
        assert result["defaults"]["chat_id_resolver"] == "default_chat_id"


class TestUnknownKeys:
    """Unknown key warnings (backward compat)."""

    def test_unknown_key_warns(self):
        """Unknown keys generate warnings, not errors."""
        config = {
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            },
            "unknown_field": "value",
            "another_bad_key": 123,
        }
        valid, warnings = validate_config(config)
        assert valid is True
        assert len(warnings) == 2
        assert any("unknown_field" in w for w in warnings)
        assert any("another_bad_key" in w for w in warnings)

    def test_no_warnings_when_all_keys_known(self):
        """Clean config generates zero warnings."""
        config = {
            "enabled": True,
            "vault": "auto",
            "platforms": {
                "telegram": {"bot_token": "123:ABC", "default_chat_id": "555"}
            },
        }
        valid, warnings = validate_config(config)
        assert valid is True
        assert warnings == []
