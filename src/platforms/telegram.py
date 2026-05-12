"""Telegram platform handler for HermesA2A v2."""
import json
import urllib.request
import urllib.error


class TelegramHandler:
    """Sends messages via the Telegram Bot API."""

    name = "telegram"

    def send_message(self, token: str, chat_id: str, text: str, **kwargs) -> dict:
        """Send a message via the Telegram Bot API."""
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": kwargs.get("parse_mode", "Markdown"),
            **{k: v for k, v in kwargs.items() if k not in ("parse_mode",)}
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            return {"ok": False, "error": body, "code": e.code}

    def get_me(self, token: str) -> dict:
        """Get bot info via /getMe."""
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            return {"ok": False, "error": body, "code": e.code}
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"JSONDecodeError: {e}"}
