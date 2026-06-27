"""Telegram float transport — fire-and-forget notification via api.telegram.org.

Extracted from `tool_handlers.py` for the v3.3 god-module split (LOW-08).
This module is a leaf node: it imports only stdlib. No plugin-internal
imports. Consumed by `tool_handlers.py` and `server.py` for output-leg
notifications.

The float is a post-handler side effect: a tool handler builds a response,
optionally fires a Telegram notification, returns the result. The notification
is best-effort — failures are logged at `logger.debug` and swallowed; the
tool result is the source of truth, not the float.

Naming convention: `telegram_float.send(text, ...)` — module is the transport,
verb is the action. The "delivery" word lives in the module name and docstrings
to match the project's established vocabulary (`_start_async_webhook_delivery`,
`deliver_only`, `delivery_id`, `push_delivery`).

(Low-08-spec.md, a2a-review-20260602 retrospective, MED-06 site.)
"""
from __future__ import annotations

import json as _json
import logging
import os
import urllib.error
import urllib.request


logger = logging.getLogger(__name__)


def _resolve_credentials() -> tuple[str, str]:
    """Resolve bot_token and chat_id from env vars. Returns ("", "") if absent.

    Env-var chain matches the inline code this extracted from: HERMES_* takes
    precedence, then A2A_*, then the bare TELEGRAM_* (a2a-float-hook doc:
    HERMES_TELEGRAM_BOT_TOKEN → A2A_TELEGRAM_BOT_TOKEN → TELEGRAM_BOT_TOKEN,
    same for chat_id).
    """
    bot = (
        os.getenv("HERMES_TELEGRAM_BOT_TOKEN")
        or os.getenv("A2A_TELEGRAM_BOT_TOKEN")
        or os.getenv("A2A_V2_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    chat = (
        os.getenv("HERMES_TELEGRAM_DEFAULT_CHAT_ID")
        or os.getenv("A2A_TELEGRAM_DEFAULT_CHAT_ID")
        or os.getenv("TELEGRAM_HOME_CHANNEL", "")
    )
    return bot, chat


def send(
    text: str,
    sender_name: str,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> None:
    """Fire-and-forget Telegram float.

    Resolves bot_token + chat_id from the env-var chain (or accepts overrides).
    Builds an HTML-formatted message, POSTs to api.telegram.org, logs the
    outcome at `logger.debug`. Never raises — float failures are diagnostic,
    not blocking.

    Args:
        text: The message body (already-padded message text from the caller).
        sender_name: Display name prefixed to the message. **Required** — the
            transport does not assume a sender. The britney-handler passes
            "britney"; YOYO/Daji/Jessie/etc. pass their own. Tests pass an
            explicit sender (test code that reads like production catches more
            bugs than test code that uses defaults).
        bot_token: Override the env-var chain. Useful for tests.
        chat_id: Override the env-var chain. Useful for tests.

    (MED-06 site, extracted for v3.3 god-module split.)
    """
    try:
        _bot = bot_token if bot_token is not None else _resolve_credentials()[0]
        _chat = chat_id if chat_id is not None else _resolve_credentials()[1]
        if _bot and _chat:
            _text = f"\u25e1 <b>{sender_name}:</b> {text}"
            _payload = _json.dumps(
                {"chat_id": str(_chat), "text": _text, "parse_mode": "HTML"},
                ensure_ascii=False,
            ).encode("utf-8")
            _req = urllib.request.Request(
                f"https://api.telegram.org/bot{_bot}/sendMessage",
                data=_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            logger.debug("sending telegram float: %s", _text[:60])
            with urllib.request.urlopen(_req, timeout=10) as _resp:
                _ok = _json.loads(_resp.read().decode()).get("ok", False)
                logger.debug("telegram float result: %s", _ok)
    except Exception as _e:
        logger.debug("telegram float exception: %s", _e)
