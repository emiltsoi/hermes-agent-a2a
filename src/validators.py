"""Boot-time health checks for HermesA2A v2.

Plugin refuses to start if identity is invalid, instead of failing silently on first float.
"""
import logging

logger = logging.getLogger(__name__)


class BootValidator:
    """Validates identity at boot time — fail fast, not silently."""

    def __init__(self, vault_resolver):
        self.vault = vault_resolver

    def validate(self, identity: dict) -> None:
        """Run health checks. Raises RuntimeError on any failure."""
        self._check_bot_token(identity)
        self._check_chat_id(identity)
        logger.info("[BootValidator] all checks passed")

    def _check_bot_token(self, identity: dict) -> None:
        """Verify bot token is present, non-empty, and not an unresolved placeholder."""
        telegram = identity.get("platforms", {}).get("telegram", {})
        token = str(telegram.get("bot_token", "")).strip()

        if not token:
            raise RuntimeError(
                "A2A identity error: no valid bot token found in fleet or agent vault. "
                "Set A2A_TELEGRAM_BOT_TOKEN env var or configure bot_token in vault.yaml."
            )

        if token.startswith("${") or "}" in token:
            raise RuntimeError(
                f"A2A identity error: bot_token appears to be an unresolved env var placeholder: {token}"
            )

    def _check_chat_id(self, identity: dict) -> None:
        """Verify default_chat_id is present and non-null."""
        telegram = identity.get("platforms", {}).get("telegram", {})
        chat_id = telegram.get("default_chat_id")

        if not chat_id and chat_id != 0:
            raise RuntimeError(
                "A2A identity error: no default_chat_id found. "
                "Set A2A_OWNER_CHAT_ID env var or configure default_chat_id in vault.yaml."
            )

        # Validate chat_id is a non-zero integer or integer string
        try:
            parsed = int(chat_id)
        except (ValueError, TypeError):
            raise RuntimeError(
                f"A2A identity error: default_chat_id must be a non-zero integer, "
                f"got {repr(chat_id)}."
            )
        if parsed == 0:
            raise RuntimeError(
                f"A2A identity error: default_chat_id must be non-zero, got {repr(chat_id)}."
            )

    def validate_token_with_telegram(self, token: str) -> None:
        """Ping Telegram /getMe to verify token is live. Raises on 401."""
        import urllib.request
        import json
        import urllib.error

        url = f"https://api.telegram.org/bot{token}/getMe"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
                if not resp.get("ok"):
                    raise RuntimeError(f"Telegram /getMe returned ok=false: {resp}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError(
                    f"A2A identity error: bot token rejected by Telegram API (401). "
                    f"Check that the bot token is correct and active."
                )
            logger.warning(
                f"[BootValidator] transient Telegram API error ({e.code}) during token "
                f"verification — boot continues. This may indicate rate-limiting or an "
                f"upstream outage. Token will be re-verified on next boot."
            )
