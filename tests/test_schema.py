"""Config schema unit tests."""
import pytest
from src.schema import validate_config, apply_defaults


def test_valid_config_returns_true_empty_warnings():
    """Valid config → (True, []) returned."""
    config = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
                "default_chat_id": "123456789",
            }
        }
    }

    valid, warnings = validate_config(config)

    assert valid is True
    assert warnings == []


def test_missing_required_field_raises_runtimeerror():
    """Missing required top-level field → RuntimeError raised."""
    config = {}  # missing "platforms"

    with pytest.raises(RuntimeError, match="required field 'platforms' is missing"):
        validate_config(config)


def test_apply_defaults_returns_new_dict_does_not_mutate_input():
    """apply_defaults returns a new dict; original is not modified."""
    original = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
                "default_chat_id": "123456789",
            }
        }
    }

    result = apply_defaults(original)

    # Result must have defaults filled in
    assert result.get("enabled") is True
    assert result.get("vault") == "auto"
    assert result["defaults"]["platform"] == "telegram"
    assert result["defaults"]["chat_type"] == "dm"

    # Original must NOT be mutated
    assert "enabled" not in original
    assert "vault" not in original
    assert "defaults" not in original
