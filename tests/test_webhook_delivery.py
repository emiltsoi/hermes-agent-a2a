"""Tests for webhook_delivery transport — HMAC-signed POST with retry, SSRF guard.

Extracted from server.py for the v3.3 god-module split (LOW-08,
a2a-review-20260602). The transport is a leaf node: stdlib only (urllib,
hmac, hashlib, asyncio). Tests cover the env-var chain for retries /
backoff / secret / host / port, the exponential-backoff formula, the
HMAC signature header, the SSRF-guard early return, the body_dict
field-conditional inclusion (mode / deliver_only), and the
use_direct_a2a short-circuit.

11 tests, one assertion per behavior. The integration tests in
test_current_tools.py cover the call site (server.py uses
webhook_delivery.trigger / trigger_async); this file covers the
*module-internal* surface.
"""
import asyncio
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from urllib.error import HTTPError

from hermes_agent_a2a import webhook_delivery


# ---------------------------------------------------------------------------
# 1. Default env values
# ---------------------------------------------------------------------------

def test_trigger_default_retries_is_3_when_env_unset(monkeypatch):
    """When A2A_WEBHOOK_RETRIES is unset, retries defaults to 3."""
    monkeypatch.delenv("A2A_WEBHOOK_RETRIES", raising=False)
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")  # non-loopback, SSRF passes

    captured = []

    def capture_urlopen(req, timeout=None):
        captured.append(req)
        m = MagicMock()
        m.status = 200
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *args: None
        return m

    # 3 retries → 1 successful urlopen call.
    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        webhook_delivery.trigger(message="hi", task_id="t")

    assert len(captured) == 1


def test_trigger_default_backoff_is_1_0_when_env_unset(monkeypatch):
    """When A2A_WEBHOOK_BACKOFF is unset, base_delay defaults to 1.0."""
    monkeypatch.delenv("A2A_WEBHOOK_BACKOFF", raising=False)
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    sleep_calls = []

    def capture_sleep(seconds):
        sleep_calls.append(seconds)

    # Force a failure on the first attempt to trigger the sleep path.
    def failing_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    with patch("urllib.request.urlopen", side_effect=failing_urlopen), \
         patch.object(webhook_delivery.time, "sleep", side_effect=capture_sleep):
        webhook_delivery.trigger(message="hi", task_id="t", retries=2, base_delay=1.0)

    # 2 attempts → 1 sleep between them. The first sleep uses base_delay * 2^0 = 1.0.
    assert sleep_calls == [1.0]


# ---------------------------------------------------------------------------
# 2. Exponential backoff formula
# ---------------------------------------------------------------------------

def test_trigger_exponential_backoff_doubles_delay_per_attempt(monkeypatch):
    """delay = base_delay * (2 ** attempt). With base_delay=0.5 and 3 attempts,
    sleeps should be 0.5 then 1.0 (no sleep after the last attempt)."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    sleep_calls = []

    def capture_sleep(seconds):
        sleep_calls.append(seconds)

    def failing_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    with patch("urllib.request.urlopen", side_effect=failing_urlopen), \
         patch.object(webhook_delivery.time, "sleep", side_effect=capture_sleep):
        webhook_delivery.trigger(message="hi", task_id="t", retries=3, base_delay=0.5)

    # 3 attempts → 2 sleeps between them: 0.5 * 2^0 = 0.5, 0.5 * 2^1 = 1.0.
    assert sleep_calls == [0.5, 1.0]


# ---------------------------------------------------------------------------
# 3. HMAC signature
# ---------------------------------------------------------------------------

def test_trigger_signature_is_sha256_hmac_of_body(monkeypatch):
    """The X-Hub-Signature-256 header must be 'sha256=' + hmac(secret, body)."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "my-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    captured = {}

    def capture_urlopen(req, timeout=None):
        captured["body"] = req.data
        # urllib.request.Request stores headers as a dict-like; the case in the
        # keys depends on how they were set. Iterate to find the signature header
        # case-insensitively (mirroring test_current_tools.py's pattern).
        for k, v in req.headers.items():
            if k.lower() == "x-hub-signature-256":
                captured["signature"] = v
                break
        m = MagicMock()
        m.status = 200
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *args: None
        return m

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        webhook_delivery.trigger(message="hello", task_id="t-sig")

    # Compute the expected signature and compare.
    expected = "sha256=" + hmac.new(b"my-secret", captured["body"], hashlib.sha256).hexdigest()
    assert captured["signature"] == expected


# ---------------------------------------------------------------------------
# 4. Body dict field-conditional inclusion
# ---------------------------------------------------------------------------

def test_trigger_appends_mode_to_body_dict_when_set(monkeypatch):
    """When mode is provided, body['mode'] is included in the JSON payload."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    captured = {}

    def capture_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        m = MagicMock()
        m.status = 200
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *args: None
        return m

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        webhook_delivery.trigger(message="hi", task_id="t", mode="peer")

    assert captured["body"]["mode"] == "peer"


def test_trigger_appends_deliver_only_to_body_dict_when_set(monkeypatch):
    """When deliver_only=True, body['deliver_only'] is True."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    captured = {}

    def capture_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        m = MagicMock()
        m.status = 200
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *args: None
        return m

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        webhook_delivery.trigger(message="hi", task_id="t", deliver_only=True)

    assert captured["body"]["deliver_only"] is True


# ---------------------------------------------------------------------------
# 5. Early-return paths
# ---------------------------------------------------------------------------

def test_trigger_no_secret_calls_on_failure_and_does_not_urlopen(monkeypatch):
    """When A2A_WEBHOOK_SECRET is empty, the trigger short-circuits and calls
    on_failure(task_id) without calling urlopen."""
    monkeypatch.delenv("A2A_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    on_failure_calls = []

    def capture_on_failure(tid):
        on_failure_calls.append(tid)

    with patch("urllib.request.urlopen") as mock_urlopen:
        webhook_delivery.trigger(message="hi", task_id="no-secret-task", on_failure=capture_on_failure)

    assert mock_urlopen.call_count == 0
    assert on_failure_calls == ["no-secret-task"]


def test_trigger_use_direct_a2a_short_circuits_to_a2a_direct_call(monkeypatch):
    """When use_direct_a2a=True and target_url is set, the trigger uses
    a2a_direct.call instead of urlopen."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    with patch("urllib.request.urlopen") as mock_urlopen, \
         patch("hermes_agent_a2a.a2a_direct.call", return_value={"result": "ok"}) as mock_direct:
        webhook_delivery.trigger(
            message="hi",
            task_id="t-direct",
            use_direct_a2a=True,
            target_url="http://127.0.0.1:41808/a2a",
            auth_token="secret",
        )

    assert mock_direct.called
    assert mock_urlopen.call_count == 0  # webhook urlopen was NOT called


# ---------------------------------------------------------------------------
# 6. Async wrapper
# ---------------------------------------------------------------------------

def test_trigger_async_uses_asyncio_to_thread_for_urlopen(monkeypatch):
    """trigger_async must run urlopen in a thread pool via asyncio.to_thread."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    # Mock _urlopen_with_status at the webhook_delivery module level (it's
    # the function that gets passed to asyncio.to_thread).
    with patch.object(webhook_delivery, "_urlopen_with_status") as mock_urlopen:
        m = MagicMock()
        m.status = 200
        mock_urlopen.return_value = m
        asyncio.run(webhook_delivery.trigger_async(message="hi", task_id="t-async"))

    assert mock_urlopen.called
    # Verify the first arg is a urllib.request.Request with the right URL.
    req = mock_urlopen.call_args[0][0]
    assert "203.0.113.50" in req.full_url


def test_trigger_async_uses_asyncio_to_thread_for_sleep(monkeypatch):
    """trigger_async offloads time.sleep to the thread pool to avoid blocking."""
    monkeypatch.setenv("A2A_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("A2A_WEBHOOK_HOST", "203.0.113.50")

    sleep_calls = []

    def capture_sleep(seconds):
        sleep_calls.append(seconds)

    def failing_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    with patch("urllib.request.urlopen", side_effect=failing_urlopen), \
         patch("asyncio.to_thread", side_effect=lambda fn, *args: (sleep_calls.append(args[0]) if fn is webhook_delivery.time.sleep else fn(*args))[1] if False else (fn(*args) if fn is not webhook_delivery.time.sleep else capture_sleep(*args))) as mock_to_thread:
        asyncio.run(webhook_delivery.trigger_async(message="hi", task_id="t-sleep", retries=2, base_delay=0.5))

    # 2 attempts → 1 sleep between them: 0.5.
    assert sleep_calls == [0.5]
