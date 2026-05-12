import pytest
from unittest.mock import MagicMock, patch
import urllib.error
from src.plugin import HermesA2AV2Plugin


class MockUrlopen:
    """Mock urllib.request.urlopen for Telegram API calls."""

    def __init__(self, valid_tokens=None):
        self.valid_tokens = valid_tokens or {"123:ABC"}

    def __call__(self, req, timeout=None):
        token = req.full_url.split("bot")[1].split("/")[0] if "bot" in req.full_url else ""
        if token in self.valid_tokens:
            return MagicMock(
                __enter__=MagicMock(
                    return_value=MagicMock(
                        read=MagicMock(return_value=b'{"ok": true, "result": {"id": 123456789, "is_bot": true, "first_name": "Test"}}')
                    )
                ),
                __exit__=MagicMock(return_value=None),
            )
        else:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized",
                {}, None
            )


class TestOnBoot:
    """on_boot lifecycle tests."""

    def test_on_boot_valid_vault_starts_cleanly(self, tmp_vault_dir):
        """on_boot with valid vault completes without raising."""
        from src.schema import apply_defaults

        vault_file = tmp_vault_dir / "vault.yaml"
        vault_file.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    bot_token: '123:ABC'\n"
            "    default_chat_id: '123456789'\n"
            "defaults:\n"
            "  platform: telegram\n"
            "  chat_type: dm\n"
        )

        profile_root = str(tmp_vault_dir.parent)
        config = apply_defaults({
            "owner_chat_id": "123456789",
            "agent_profile_path": profile_root,
            "platforms": {"telegram": {"bot_token": "123:ABC"}}
        })
        plugin = HermesA2AV2Plugin(config)

        with patch("urllib.request.urlopen", MockUrlopen(valid_tokens={"123:ABC"})):
            plugin.on_boot()

    def test_on_boot_invalid_token_refuses_start(self, tmp_vault_dir):
        """on_boot raises RuntimeError when Telegram rejects the token."""
        from src.schema import apply_defaults

        vault_file = tmp_vault_dir / "vault.yaml"
        vault_file.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    bot_token: '999:DEAD'\n"
            "    default_chat_id: '123456789'\n"
            "defaults:\n"
            "  platform: telegram\n"
            "  chat_type: dm\n"
        )

        profile_root = str(tmp_vault_dir.parent)
        config = apply_defaults({
            "owner_chat_id": "123456789",
            "agent_profile_path": profile_root,
            "platforms": {"telegram": {"bot_token": "999:DEAD"}}
        })
        plugin = HermesA2AV2Plugin(config)

        with pytest.raises(RuntimeError) as exc:
            with patch("urllib.request.urlopen", MockUrlopen(valid_tokens={"123:ABC"})):
                plugin.on_boot()

        assert "401" in str(exc.value)
        assert "Telegram" in str(exc.value)
