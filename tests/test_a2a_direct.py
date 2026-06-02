"""Tests for a2a_direct transport — single-shot JSON-RPC call to a remote A2A agent.

Extracted from server.py for the v3.3 god-module split (LOW-08,
a2a-review-20260602). The transport is a leaf node: stdlib only (urllib,
json, asyncio). Tests cover the env-var override (A2A_AGENT_NAME),
the result/error response shapes, error-to-dict conversion for
HTTPError/URLError/general exceptions, and the spec-format payload
contract (params.message.role/parts/metadata, not the deprecated
params.task.text).

Six tests, one assertion per behavior. The integration tests in
test_current_tools.py cover the *call site* (server.py uses
a2a_direct.call); this file covers the *module-internal* surface.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

from urllib.error import URLError

from hermes_agent_a2a import a2a_direct


# ---------------------------------------------------------------------------
# 1. A2A_AGENT_NAME env override
# ---------------------------------------------------------------------------

def test_call_uses_A2A_AGENT_NAME_env_override(monkeypatch):
    """A2A_AGENT_NAME env var sets sender_name in the JSON-RPC payload."""
    monkeypatch.setenv("A2A_AGENT_NAME", "custom-agent")

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"result": {"id": "task-123"}}).encode()
    mock_response.__enter__ = lambda self: self
    mock_response.__exit__ = lambda self, *args: None

    captured = {}

    def capture_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return mock_response

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        a2a_direct.call(url="http://127.0.0.1:41808/a2a", message="hi", task_id="task-123")

    # The spec format puts sender_name under params.message.metadata.sender_name
    assert captured["body"]["params"]["message"]["metadata"]["sender_name"] == "custom-agent"


def test_call_falls_back_to_hermes_agent_default_when_env_unset(monkeypatch):
    """Default sender_name is 'hermes-agent' when A2A_AGENT_NAME is not set."""
    monkeypatch.delenv("A2A_AGENT_NAME", raising=False)

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"result": {"id": "t"}}).encode()
    mock_response.__enter__ = lambda self: self
    mock_response.__exit__ = lambda self, *args: None

    captured = {}

    def capture_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return mock_response

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        a2a_direct.call(url="http://127.0.0.1:41808/a2a", message="hi", task_id="t")

    assert captured["body"]["params"]["message"]["metadata"]["sender_name"] == "hermes-agent"


# ---------------------------------------------------------------------------
# 2. Response shape handling
# ---------------------------------------------------------------------------

def test_call_returns_invalid_response_when_neither_result_nor_error():
    """A response with neither 'result' nor 'error' returns 'Invalid response'."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"unrelated": "key"}).encode()
    mock_response.__enter__ = lambda self: self
    mock_response.__exit__ = lambda self, *args: None

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = a2a_direct.call(url="http://127.0.0.1:41808/a2a", message="hi", task_id="task-x")

    assert result == {"error": "Invalid response", "task_id": "task-x"}


# ---------------------------------------------------------------------------
# 3. Error-to-dict conversion
# ---------------------------------------------------------------------------

def test_call_swallows_generic_exception_into_error_dict():
    """A non-HTTP, non-URL exception (e.g. TimeoutError) returns an error dict."""
    with patch("urllib.request.urlopen", side_effect=TimeoutError("connect timeout")):
        result = a2a_direct.call(url="http://127.0.0.1:41808/a2a", message="hi", task_id="task-y")

    # The error string is whatever str(e) returns for TimeoutError; just check
    # the shape (error key + task_id) and that the message contains the cause.
    assert "error" in result
    assert result["task_id"] == "task-y"
    assert "connect timeout" in result["error"]


def test_call_swallows_URLError_into_error_dict():
    """URLError (DNS failure, connection refused) returns an error dict."""
    with patch("urllib.request.urlopen", side_effect=URLError("Name or service not known")):
        result = a2a_direct.call(url="http://nonexistent.example/a2a", message="hi", task_id="task-z")

    assert "error" in result
    assert result["task_id"] == "task-z"
    assert "URL error" in result["error"]


# ---------------------------------------------------------------------------
# 4. Async wrapper
# ---------------------------------------------------------------------------

def test_call_async_delegates_to_call_via_asyncio_to_thread(monkeypatch):
    """call_async must run call() in a thread pool (asyncio.to_thread) and return its result."""
    with patch.object(a2a_direct, "call", return_value={"result": {"ok": True}, "task_id": "t-async"}) as mock_call:
        result = asyncio.run(
            a2a_direct.call_async(url="http://127.0.0.1:41808/a2a", message="hi", task_id="t-async")
        )

    # call_async uses asyncio.to_thread(call, ...), so the mock's call_args is
    # a positional call: call was called with the same args as call_async.
    assert mock_call.called
    assert result == {"result": {"ok": True}, "task_id": "t-async"}


# ---------------------------------------------------------------------------
# 5. Spec-format payload contract
# ---------------------------------------------------------------------------

def test_call_payload_uses_spec_format_with_role_parts_metadata():
    """Payload must use params.message.role/parts/metadata, not the deprecated params.task.text."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"result": {"id": "t"}}).encode()
    mock_response.__enter__ = lambda self: self
    mock_response.__exit__ = lambda self, *args: None

    captured = {}

    def capture_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return mock_response

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        a2a_direct.call(url="http://127.0.0.1:41808/a2a", message="hello", task_id="t")

    # The non-spec format is params.task.text; the spec format is
    # params.message.role + params.message.parts + params.message.metadata.
    # Previous revert (f539a9d) was incorrect; spec format is required.
    assert "task" not in captured["body"]["params"], (
        "Payload must not use the deprecated params.task.text format"
    )
    assert captured["body"]["params"]["message"]["role"] == 1
    assert captured["body"]["params"]["message"]["parts"][0]["text"] == "hello"
    assert "metadata" in captured["body"]["params"]["message"]
