"""Tests for telegram_float.send — the Telegram float transport.

Extracted from tool_handlers.py for the v3.3 god-module split (LOW-08,
a2a-review-20260602). The transport is a leaf node: stdlib only. Tests
cover the env-var chain, never-raises contract, URL/payload construction,
and error swallowing.

Six tests, one assertion per behavior. Skipped the optional 7th test
(`ensure_ascii=False` for Chinese chars) — covered indirectly by the
HTML payload test, and `ensure_ascii=True` would be a different test
about encoding, not float behavior.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from hermes_agent_a2a.telegram_float import send


# ---------------------------------------------------------------------------
# 1. Env-var chain resolution: HERMES → A2A → TELEGRAM
# ---------------------------------------------------------------------------

def test_send_resolves_bot_token_from_env_chain(monkeypatch):
    """HERMES_TELEGRAM_BOT_TOKEN wins over A2A_*, which wins over TELEGRAM_*."""
    monkeypatch.delenv("HERMES_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("A2A_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", raising=False)
    monkeypatch.delenv("A2A_TELEGRAM_DEFAULT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)

    captured_urls = []

    def fake_urlopen(req, timeout):
        captured_urls.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: False
        m.read = lambda: json.dumps({"ok": True}).encode()
        return m

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        # HERMES wins
        monkeypatch.setenv("HERMES_TELEGRAM_BOT_TOKEN", "hermes-bot")
        monkeypatch.setenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", "111")
        send(text="msg-a", sender_name="britney")
        # A2A fallback
        monkeypatch.delenv("HERMES_TELEGRAM_BOT_TOKEN")
        monkeypatch.delenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID")
        monkeypatch.setenv("A2A_TELEGRAM_BOT_TOKEN", "a2a-bot")
        monkeypatch.setenv("A2A_TELEGRAM_DEFAULT_CHAT_ID", "222")
        send(text="msg-b", sender_name="britney")
        # TELEGRAM fallback
        monkeypatch.delenv("A2A_TELEGRAM_BOT_TOKEN")
        monkeypatch.delenv("A2A_TELEGRAM_DEFAULT_CHAT_ID")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tele-bot")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "333")
        send(text="msg-c", sender_name="britney")

    assert captured_urls == [
        "https://api.telegram.org/bothermes-bot/sendMessage",
        "https://api.telegram.org/bota2a-bot/sendMessage",
        "https://api.telegram.org/bottele-bot/sendMessage",
    ], f"Env-var chain precedence broken: {captured_urls}"


# ---------------------------------------------------------------------------
# 2. Never-raises contract when credentials are absent
# ---------------------------------------------------------------------------

def test_send_skips_when_credentials_absent(monkeypatch):
    """If bot_token or chat_id is empty, send() does nothing. No exception."""
    monkeypatch.delenv("HERMES_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("A2A_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", raising=False)
    monkeypatch.delenv("A2A_TELEGRAM_DEFAULT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)

    urlopen_called = []

    def fake_urlopen(req, timeout):
        urlopen_called.append(True)
        return MagicMock()

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        # Should not raise, should not call urlopen
        send(text="msg", sender_name="britney")

    assert urlopen_called == [], "send() called urlopen despite missing credentials"


def test_send_skips_when_only_bot_present(monkeypatch):
    """If only bot is set (no chat_id), send() is a no-op."""
    monkeypatch.setenv("HERMES_TELEGRAM_BOT_TOKEN", "bot-only")
    monkeypatch.delenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", raising=False)
    monkeypatch.delenv("A2A_TELEGRAM_DEFAULT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)

    urlopen_called = []

    def fake_urlopen(req, timeout):
        urlopen_called.append(True)
        return MagicMock()

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        send(text="msg", sender_name="britney")

    assert urlopen_called == [], "send() called urlopen with chat_id missing"


# ---------------------------------------------------------------------------
# 3. URL construction
# ---------------------------------------------------------------------------

def test_send_posts_to_correct_telegram_url(monkeypatch):
    """URL is exactly https://api.telegram.org/bot<token>/sendMessage."""
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["timeout"] = timeout
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: False
        m.read = lambda: json.dumps({"ok": True}).encode()
        return m

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        send(text="hello", sender_name="britney", bot_token="abc123", chat_id="999")

    assert captured["url"] == "https://api.telegram.org/botabc123/sendMessage"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 10, "Default timeout should be 10s per project convention"


# ---------------------------------------------------------------------------
# 4. HTML payload construction with sender_name
# ---------------------------------------------------------------------------

def test_send_builds_html_payload(monkeypatch):
    """Payload uses parse_mode=HTML, includes sender_name and chat_id."""
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: False
        m.read = lambda: json.dumps({"ok": True}).encode()
        return m

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        send(text="hi there", sender_name="daji", bot_token="tok", chat_id="42")

    body = json.loads(captured["data"].decode("utf-8"))
    assert body["chat_id"] == "42", f"chat_id mismatch: {body}"
    assert body["parse_mode"] == "HTML", f"parse_mode must be HTML: {body}"
    assert "daji" in body["text"], f"sender_name missing from text: {body}"
    assert "hi there" in body["text"], f"text missing from payload: {body}"
    # The britney inline code had a hardcoded 'britney' literal; the new
    # transport doesn't assume who's calling. This assertion would have
    # failed with the old default.
    assert body["text"].startswith("\u25e1 <b>daji:</b>"), (
        f"Expected HTML-formatted sender prefix: {body['text']!r}"
    )


# ---------------------------------------------------------------------------
# 5. urllib error swallowing (HTTPError, URLError, timeout, etc.)
# ---------------------------------------------------------------------------

def test_send_swallows_urllib_errors(monkeypatch):
    """Connection errors, timeouts, DNS failures — all swallowed, no raise."""
    import urllib.error

    monkeypatch.setenv("HERMES_TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", "123")

    with patch(
        "hermes_agent_a2a.telegram_float.urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        # Must not raise
        send(text="msg", sender_name="britney")

    with patch(
        "hermes_agent_a2a.telegram_float.urllib.request.urlopen",
        side_effect=TimeoutError("read timed out"),
    ):
        send(text="msg", sender_name="britney")


# ---------------------------------------------------------------------------
# 6. HTTP-error response (5xx, 4xx) swallowing
# ---------------------------------------------------------------------------

def test_send_swallows_5xx_response(monkeypatch):
    """HTTP 5xx from Telegram: log + swallow, no raise."""
    import urllib.error

    monkeypatch.setenv("HERMES_TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", "123")

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {}, None,
        )

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        # Must not raise
        send(text="msg", sender_name="britney")


def test_send_swallows_ok_false_response(monkeypatch):
    """Telegram returns 200 but ok=false: log + swallow, no raise."""
    monkeypatch.setenv("HERMES_TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID", "123")

    def fake_urlopen(req, timeout):
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: False
        m.read = lambda: json.dumps({"ok": False, "description": "rate-limited"}).encode()
        return m

    with patch("hermes_agent_a2a.telegram_float.urllib.request.urlopen", fake_urlopen):
        # Must not raise
        send(text="msg", sender_name="britney")
