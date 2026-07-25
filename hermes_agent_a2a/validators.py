"""Boot-time health checks for HermesA2A v3.

Plugin refuses to start if identity is invalid, instead of failing silently on first call.
"""
import logging

logger = logging.getLogger(__name__)


class BootValidator:
    """Validates identity at boot time — fail fast, not silently."""

    def __init__(self, vault_resolver):
        self.vault = vault_resolver

    def validate(self, identity: dict) -> None:
        """Run health checks. Raises RuntimeError on any failure."""
        self._check_has_transport(identity)
        self._check_telegram_if_present(identity)
        logger.info("[BootValidator] all checks passed")

    def _check_has_transport(self, identity: dict) -> None:
        """Verify the identity has at least one usable A2A transport URL."""
        a2a_url = identity.get("a2a_url", "")
        webhook_url = identity.get("webhook_url", "")
        transports = identity.get("transports", {})
        a2a_rpc_url = (transports.get("a2a_rpc") or {}).get("url", "")
        hermes_webhook_url = (transports.get("hermes_webhook") or {}).get("url", "")
        agent_card_url = (transports.get("agent_card") or {}).get("url", "")

        if a2a_url or webhook_url or a2a_rpc_url or hermes_webhook_url or agent_card_url:
            return

        raise RuntimeError(
            "A2A identity error: no usable A2A transport URL found. "
            "Configure transports.a2a_rpc.url, transports.hermes_webhook.url, "
            "or transports.agent_card.url in the identity vault."
        )

    def _check_telegram_if_present(self, identity: dict) -> None:
        """Validate Telegram credentials only when Telegram is configured."""
        telegram = identity.get("platforms", {}).get("telegram")
        if not isinstance(telegram, dict):
            return

        token = str(telegram.get("bot_token", "")).strip()
        chat_id = telegram.get("default_chat_id")

        if not token and chat_id is None:
            return

        if token.startswith("${") or "}" in token:
            raise RuntimeError(
                f"A2A identity error: bot_token appears to be an unresolved env var placeholder: {token}"
            )

        if not token:
            raise RuntimeError(
                "A2A identity error: default_chat_id is set but bot_token is missing."
            )

        if chat_id is None or chat_id == 0:
            raise RuntimeError(
                "A2A identity error: bot_token is set but default_chat_id is missing or zero."
            )

        if not (token.startswith("${") or "}" in token):
            self.validate_token_with_telegram(token)

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
                    "A2A identity error: bot token rejected by Telegram API (401). "
                    "Check that the bot token is correct and active."
                )
            logger.warning(
                "[BootValidator] transient Telegram API error (%d) during token "
                "verification — boot continues. This may indicate rate-limiting or "
                "an upstream outage. Token will be re-verified on next boot.",
                e.code,
            )
