"""Unit tests for A2A tool handlers — Phase 3."""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


# ----------------------------------------------------------------------
# Helpers to run tools with mocked HTTP / VaultResolver
# ----------------------------------------------------------------------


class MockResponse:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self._status = status

    def read(self, n: int = -1):
        return json.dumps(self._data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockUrlopen:
    def __init__(self, response: MockResponse):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *args):
        pass


def mock_urlopen_factory(response_data: dict):
    """Return a mock urlopen that returns response_data."""
    def urlopen(req, timeout=None):
        return MockUrlopen(MockResponse(response_data))
    return urlopen


# ----------------------------------------------------------------------
# handle_discover
# ----------------------------------------------------------------------

def test_handle_discover_by_name(monkeypatch):
    """VaultResolver.resolve_agent resolves name to URL; card is fetched and returned."""
    from src.tools import handle_discover

    agent_card = {
        "name": "yoyo",
        "description": "A fun agent",
        "version": "1.0.0",
        "skills": [{"name": "dance", "description": "Dances"}],
        "capabilities": {"streaming": False},
    }

    resolved = {"a2a_url": "http://yoyo:8081", "auth_token": "secret-token"}

    with patch("src.tools._resolve_agent_by_name", return_value=resolved):
        with patch("src.tools._http_request", return_value=agent_card) as mock_http:
            result = handle_discover(name="yoyo")

    assert result["agent_name"] == "yoyo"
    assert result["description"] == "A fun agent"
    assert result["url"] == "http://yoyo:8081"
    assert result["version"] == "1.0.0"
    mock_http.assert_called_once()
    call_url = mock_http.call_args[0][1]
    assert call_url == "http://yoyo:8081/.well-known/agent.json"


def test_handle_discover_by_url(monkeypatch):
    """Direct URL discover: fetches card from the provided URL, no VaultResolver needed."""
    from src.tools import handle_discover

    agent_card = {
        "name": "daji",
        "description": "Data agent",
        "version": "2.0.0",
        "skills": [],
        "capabilities": {},
    }

    with patch("src.tools._http_request", return_value=agent_card) as mock_http:
        result = handle_discover(url="http://daji.example.com:8081")

    assert result["agent_name"] == "daji"
    assert result["url"] == "http://daji.example.com:8081"
    mock_http.assert_called_once()
    call_args = mock_http.call_args
    assert call_args[0][1] == "http://daji.example.com:8081/.well-known/agent.json"
    assert call_args[1]["headers"] == {}


def test_handle_discover_unknown_agent(monkeypatch):
    """VaultResolver.resolve_agent returns None → error dict."""
    from src.tools import handle_discover

    with patch("src.tools._resolve_agent_by_name", return_value=None):
        result = handle_discover(name="nonexistent")

    assert "error" in result
    assert "nonexistent" in result["error"]


def test_handle_discover_no_args(monkeypatch):
    """Neither name nor url → error dict."""
    from src.tools import handle_discover

    result = handle_discover()
    assert "error" in result


def test_handle_discover_loopback_rejected(monkeypatch):
    """Direct URL to localhost is rejected by SSRF protection."""
    from src.tools import handle_discover

    result = handle_discover(url="http://127.0.0.1:8081/.well-known/agent.json")
    assert "error" in result
    assert "loopback" in result["error"].lower() or "A2A URL" in result["error"]


# ----------------------------------------------------------------------
# handle_list
# ----------------------------------------------------------------------

def test_handle_list(monkeypatch):
    """VaultResolver.list_agents returns agent list; handle_list returns it with count."""
    from src.tools import handle_list

    agents = [
        {"name": "yoyo", "a2a_url": "http://yoyo:8081", "auth_token": "tok1", "description": "Yoyo agent"},
        {"name": "daji", "a2a_url": "http://daji:8081", "auth_token": "", "description": "Data agent"},
    ]

    with patch("src.tools._list_agents", return_value=agents):
        result = handle_list()

    assert result["count"] == 2
    assert len(result["agents"]) == 2
    assert result["agents"][0]["name"] == "yoyo"


def test_handle_list_empty(monkeypatch):
    """No agents registered → empty list."""
    from src.tools import handle_list

    with patch("src.tools._list_agents", return_value=[]):
        result = handle_list()

    assert result["count"] == 0
    assert result["agents"] == []


# ----------------------------------------------------------------------
# handle_call
# ----------------------------------------------------------------------

def test_handle_call_mode1_success(monkeypatch):
    """Mode 1: POST succeeds, response is extracted and returned."""
    from src.tools import handle_call

    rpc_response = {
        "jsonrpc": "2.0",
        "id": "abc",
        "result": {
            "id": "task-123",
            "status": {"state": "completed"},
            "artifacts": [
                {"parts": [{"type": "text", "text": "Hello from yoyo"}], "index": 0}
            ],
        },
    }

    with patch("src.tools._resolve_agent_by_name", return_value={"a2a_url": "http://yoyo:8081", "auth_token": ""}):
        with patch("src.tools._http_request", return_value=rpc_response):
            result = handle_call(name="yoyo", message="say hello")

    assert "error" not in result
    assert result["response"] == "Hello from yoyo"
    assert result["state"] == "completed"


def test_handle_call_mode1_working_then_completed(monkeypatch):
    """Mode 1: remote returns 'working', tool polls until 'completed'."""
    from src.tools import handle_call

    calls = []

    def fake_http(method, url, json_body=None, headers=None):
        calls.append(json_body)
        if "tasks/get" in (json_body or {}).get("method", ""):
            return {
                "jsonrpc": "2.0", "id": "x",
                "result": {
                    "id": "task-123",
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"type": "text", "text": "Polled result"}], "index": 0}],
                },
            }
        return {
            "jsonrpc": "2.0", "id": "x",
            "result": {"id": "task-123", "status": {"state": "working"}},
        }

    with patch("src.tools._resolve_agent_by_name", return_value={"a2a_url": "http://yoyo:8081", "auth_token": ""}):
        with patch("src.tools._http_request", side_effect=fake_http):
            with patch("src.tools._POLL_INTERVAL", 0.001):
                result = handle_call(name="yoyo", message="do work")

    assert "error" not in result
    assert result["response"] == "Polled result"


def test_handle_call_mode1_connection_error(monkeypatch):
    """Mode 1: connection error → error dict."""
    from src.tools import handle_call

    with patch("src.tools._resolve_agent_by_name", return_value={"a2a_url": "http://yoyo:8081", "auth_token": ""}):
        with patch("src.tools._http_request", side_effect=ConnectionError("Connection refused")):
            result = handle_call(name="yoyo", message="hello")

    assert "error" in result
    assert "Cannot connect" in result["error"]


def test_handle_call_mode1_unknown_agent(monkeypatch):
    """Mode 1: unknown agent name → error dict."""
    from src.tools import handle_call

    with patch("src.tools._resolve_agent_by_name", return_value=None):
        result = handle_call(name="unknown-agent", message="hello")

    assert "error" in result


def test_handle_call_mode1_no_message(monkeypatch):
    """Mode 1: no message → error dict."""
    from src.tools import handle_call

    result = handle_call(name="yoyo", message="")
    assert "error" in result


def test_handle_call_mode1_no_target(monkeypatch):
    """Mode 1: neither name nor url → error dict."""
    from src.tools import handle_call

    result = handle_call(message="hello")
    assert "error" in result


def test_handle_call_mode2(monkeypatch, tmp_path):
    """Mode 2: spawns subprocess with correct params and returns worker result."""
    import os as _os
    from src.tools import handle_call

    worker_result = {
        "task_id": "a2a-m2-abc",
        "state": "completed",
        "response": "Mode 2 says hi",
        "source": "ephemeral:yoyo",
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(worker_result)
    mock_proc.stderr = ""

    # Create the fake yoyo profile directory inside the temp HERMES_HOME
    fake_hermes_home = str(tmp_path)
    profile_dir = _os.path.join(fake_hermes_home, "profiles", "yoyo")
    _os.makedirs(profile_dir, exist_ok=True)

    # Also create the hermes-agent/venv/bin/python stub
    venv_bin = _os.path.join(fake_hermes_home, "hermes-agent", "venv", "bin")
    _os.makedirs(venv_bin, exist_ok=True)

    monkeypatch.setenv("HERMES_HOME", fake_hermes_home)

    mock_run = MagicMock(return_value=mock_proc)
    with patch("subprocess.run", mock_run):
        result = handle_call(name="yoyo", message="hello mode 2", worker_at="caller")

    assert "error" not in result
    assert result["response"] == "Mode 2 says hi"
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs["timeout"] == 300
    assert json.loads(call_kwargs["input"])["message"] == "hello mode 2"


def test_handle_call_mode2_unknown_agent(monkeypatch):
    """Mode 2: nonexistent profile directory → error dict."""
    from src.tools import handle_call

    fake_hermes_home = "/tmp/hermes-v3-dev"
    monkeypatch.setenv("HERMES_HOME", fake_hermes_home)

    # Profile directory does not exist → error dict
    result = handle_call(name="nonexistent", message="hello", worker_at="caller")

    assert "error" in result
    assert "not found" in result["error"]


def test_handle_call_mode3(monkeypatch):
    """Mode 3: POSTs to target with worker_at='target', returns worker result."""
    from src.tools import handle_call

    rpc_response = {
        "jsonrpc": "2.0", "id": "x",
        "result": {
            "id": "task-123",
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": "Mode 3 result"}], "index": 0}],
        },
    }

    with patch("src.tools._resolve_agent_by_name", return_value={"a2a_url": "http://yoyo:8081", "auth_token": "tok"}):
        with patch("src.tools._http_request", return_value=rpc_response):
            result = handle_call(name="yoyo", message="hello", worker_at="target")

    assert "error" not in result
    assert result["response"] == "Mode 3 result"
    assert result["mode"] == "3"


# ----------------------------------------------------------------------
# handle_telegram
# ----------------------------------------------------------------------

def test_handle_telegram(monkeypatch):
    """Telegram: bot_token from VaultResolver, API called with target chat_id."""
    from src.tools import handle_telegram

    own_vault = {
        "platforms": {
            "telegram": {
                "bot_token": "test-token-placeholder",
            }
        }
    }
    target_info = {
        "name": "yoyo",
        "a2a_url": "http://yoyo:8081",
        "platforms": {
            "telegram": {
                "default_chat_id": "123456789",
            }
        },
    }

    api_response = {
        "ok": True,
        "result": {"message_id": 42},
    }

    with patch("src.tools.VaultResolver") as MockVR:
        MockVR.return_value.resolve.return_value = own_vault
        with patch("src.tools._resolve_agent_by_name", return_value=target_info):
            with patch("src.tools.TelegramHandler") as MockTH:
                MockTH.return_value.send_message.return_value = api_response
                result = handle_telegram(agent="yoyo", message="hello telegram")

    assert result["status"] == "delivered"
    assert result["message_id"] == 42
    MockTH.return_value.send_message.assert_called_once()
    call_kwargs = MockTH.return_value.send_message.call_args[1]
    assert call_kwargs["token"] == "test-token-placeholder"
    assert call_kwargs["chat_id"] == "123456789"


def test_handle_telegram_unknown_agent(monkeypatch):
    """VaultResolver.resolve_agent returns None for unknown agent → error dict."""
    from src.tools import handle_telegram

    own_vault = {
        "platforms": {
            "telegram": {"bot_token": "test-token-placeholder"}
        }
    }

    with patch("src.tools.VaultResolver") as MockVR:
        MockVR.return_value.resolve.return_value = own_vault
        with patch("src.tools._resolve_agent_by_name", return_value=None):
            result = handle_telegram(agent="nobody", message="hello")

    assert "error" in result
    assert "nobody" in result["error"]


def test_handle_telegram_missing_bot_token(monkeypatch):
    """Own vault has no bot_token → error dict."""
    from src.tools import handle_telegram

    with patch("src.tools.VaultResolver") as MockVR:
        MockVR.return_value.resolve.return_value = {"platforms": {"telegram": {}}}
        result = handle_telegram(agent="yoyo", message="hello")

    assert "error" in result


def test_handle_telegram_api_failure(monkeypatch):
    """Telegram API returns ok=False → error dict."""
    from src.tools import handle_telegram

    own_vault = {
        "platforms": {
            "telegram": {"bot_token": "test-token-placeholder"}
        }
    }
    target_info = {
        "name": "yoyo",
        "platforms": {"telegram": {"default_chat_id": "123456789"}},
    }

    with patch("src.tools.VaultResolver") as MockVR:
        MockVR.return_value.resolve.return_value = own_vault
        with patch("src.tools._resolve_agent_by_name", return_value=target_info):
            with patch("src.tools.TelegramHandler") as MockTH:
                MockTH.return_value.send_message.return_value = {"ok": False, "error": "Chat not found"}
                result = handle_telegram(agent="yoyo", message="hello")

    assert "error" in result
    assert "Chat not found" in result["error"]


# ----------------------------------------------------------------------
# register()
# ----------------------------------------------------------------------

def test_register_adds_all_tools(monkeypatch):
    """register() calls registry.tools.register for all 4 tools."""
    from src.tools import register

    mock_registry = MagicMock()

    with patch("src.tools.logger"):
        register(mock_registry)

    assert mock_registry.tools.register.call_count == 4

    # register() is called with keyword args: name=, fn=, schema=
    registered_names = {
        call.kwargs["name"]
        for call in mock_registry.tools.register.call_args_list
    }
    assert "a2a_discover" in registered_names
    assert "a2a_list" in registered_names
    assert "a2a_call" in registered_names
    assert "a2a_telegram" in registered_names


# ----------------------------------------------------------------------
# Utility functions
# ----------------------------------------------------------------------

def test_normalize_url():
    from src.tools import _normalize_url
    assert _normalize_url("  http://foo.com/  ") == "http://foo.com"
    assert _normalize_url("http://foo.com///") == "http://foo.com"


def test_validate_target_url_rejects_loopback():
    from src.tools import _validate_target_url
    # IPv4 loopback and mapped addresses are rejected
    for url in ["http://localhost/x", "http://127.0.0.1/x", "http://0.0.0.0/x"]:
        with pytest.raises(ValueError, match="loopback|A2A URL"):
            _validate_target_url(url)


def test_validate_target_url_accepts_valid():
    from src.tools import _validate_target_url
    assert _validate_target_url("http://agent.example.com:8081") == "http://agent.example.com:8081"
    assert _validate_target_url("https://192.168.1.1:8081") == "https://192.168.1.1:8081"


def test_consume_rate_limit():
    from src.tools import _consume_rate_limit, _call_timestamps, _rate_lock
    # Should allow calls
    with _rate_lock:
        _call_timestamps.clear()
    assert _consume_rate_limit() is True
