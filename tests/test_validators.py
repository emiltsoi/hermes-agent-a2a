"""BootValidator unit tests."""
import pytest
from unittest.mock import MagicMock
from src.validators import BootValidator


def test_validate_passes_with_valid_token_and_chat_id():
    """Valid bot_token and chat_id → validate() completes without error."""
    mock_vault = MagicMock()
    validator = BootValidator(mock_vault)

    identity = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
                "default_chat_id": "123456789",
            }
        }
    }

    # Should not raise
    validator.validate(identity)


def test_validate_raises_on_missing_token():
    """Missing bot_token → RuntimeError raised."""
    mock_vault = MagicMock()
    validator = BootValidator(mock_vault)

    identity = {
        "platforms": {
            "telegram": {
                "default_chat_id": "123456789",
            }
        }
    }

    with pytest.raises(RuntimeError, match="no valid bot token"):
        validator.validate(identity)


def test_validate_raises_on_unresolved_placeholder_token():
    """Unresolved ${ENV_VAR} placeholder token → RuntimeError raised."""
    mock_vault = MagicMock()
    validator = BootValidator(mock_vault)

    identity = {
        "platforms": {
            "telegram": {
                "bot_token": "${TELEGRAM_BOT_TOKEN}",
                "default_chat_id": "123456789",
            }
        }
    }

    with pytest.raises(RuntimeError, match="unresolved env var placeholder"):
        validator.validate(identity)


def test_validate_raises_on_missing_chat_id():
    """Missing default_chat_id → RuntimeError raised."""
    mock_vault = MagicMock()
    validator = BootValidator(mock_vault)

    identity = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
            }
        }
    }

    with pytest.raises(RuntimeError, match="no default_chat_id"):
        validator.validate(identity)
