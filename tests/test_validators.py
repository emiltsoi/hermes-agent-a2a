import pytest
from src.validators import BootValidator


class TestBootValidator:
    def test_missing_token_raises(self):
        validator = BootValidator(None)
        with pytest.raises(RuntimeError, match="no valid bot token"):
            validator._check_bot_token({"platforms": {"telegram": {}}})

    def test_empty_token_raises(self):
        validator = BootValidator(None)
        with pytest.raises(RuntimeError, match="no valid bot token"):
            validator._check_bot_token({"platforms": {"telegram": {"bot_token": "   "}}})

    def test_unresolved_env_placeholder_raises(self):
        validator = BootValidator(None)
        with pytest.raises(RuntimeError, match="unresolved env var"):
            validator._check_bot_token({"platforms": {"telegram": {"bot_token": "${A2A_V2_BOT_TOKEN}"}}})

    def test_missing_chat_id_raises(self):
        validator = BootValidator(None)
        with pytest.raises(RuntimeError, match="no default_chat_id"):
            validator._check_chat_id({"platforms": {"telegram": {"bot_token": "123456:ABC"}}})

    def test_valid_identity_passes_check_bot_token(self):
        validator = BootValidator(None)
        identity = {"platforms": {"telegram": {"bot_token": "123456:ABC", "default_chat_id": "7945905361"}}}
        validator._check_bot_token(identity)  # should not raise
        validator._check_chat_id(identity)  # should not raise

    def test_validate_calls_all_checks(self):
        validator = BootValidator(None)
        identity = {"platforms": {"telegram": {"bot_token": "123456:ABC", "default_chat_id": "7945905361"}}}
        validator.validate(identity)  # should not raise
