import os
from pathlib import Path
import sys
import subprocess
import urllib.request
from unittest.mock import patch

import yaml

from hermes_agent_a2a import schemas
from hermes_agent_a2a import tool_handlers as tools
from hermes_agent_a2a import tool_registry
from hermes_agent_a2a.a2a_spec import build_hermes_metadata, build_task_cancel_payload, build_task_send_payload, parse_task_result
from hermes_agent_a2a.identity import resolve_agent
from hermes_agent_a2a.worker_registry import cancel_worker, cleanup_zombie_processes, register_worker, unregister_worker


class FakeRegistry:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


def test_registers_current_a2a_tools():
    registry = FakeRegistry()

    tool_registry.register(registry)

    assert set(registry.tools) == {
        "a2a_help",
        "a2a_discover",
        "a2a_list",
        "a2a_send_protocol_task",
        "a2a_cancel_protocol_task",
        "a2a_run_local_agent_task",
        "a2a_run_remote_agent_task",
        "a2a_send_session_message",
        "a2a_get_metrics",
    }
    assert all(entry["toolset"] == "a2a" for entry in registry.tools.values())


def test_v3_tool_handlers_do_not_import_internal_platform_modules():
    source = Path(tools.__file__).read_text()
    assert "from .platforms" not in source
    assert "TelegramHandler" not in source


def test_help_exposes_registration_security_and_troubleshooting_topics():
    result = tools.handle_help("overview")

    assert "register_external" in result["topics"]
    assert "security" in result["topics"]
    assert "troubleshooting" in result["topics"]


def test_discover_can_register_external_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_VAULT_PATH", str(tmp_path))
    card = {
        "name": "External Demo",
        "description": "demo",
        "version": "1",
        "skills": [{"name": "chat", "description": "Chat"}],
        "capabilities": {"streaming": False},
    }

    with patch.object(tools, "_http_request", return_value=card):
        result = tools.handle_discover(
            url="https://external.example",
            agent_card_path="agent-card.json",
            auth_type="api_key",
            auth_header="X-API-Key",
            auth_value="runtime-secret",
            register=True,
            register_as="External Demo",
            rpc_url="https://external.example/rpc",
            auth_value_env="EXTERNAL_DEMO_KEY",
        )

    assert result["registration"]["registered"] is True
    identity_path = tmp_path / "a2a" / "agents" / "external-demo" / "identity.yaml"
    identity = yaml.safe_load(identity_path.read_text())
    assert identity["transports"]["a2a_rpc"]["url"] == "https://external.example/rpc"
    assert identity["transports"]["a2a_rpc"]["auth"] == {
        "type": "api_key",
        "header": "X-API-Key",
        "value_env": "EXTERNAL_DEMO_KEY",
    }
    assert resolve_agent("external-demo")["transports"]["agent_card"]["path"] == "/agent-card.json"


def test_direct_auth_headers_support_bearer_api_key_and_custom_header():
    assert tools._auth_headers({"type": "bearer", "token": "t"}) == {"Authorization": "Bearer t"}
    assert tools._auth_headers({"type": "api_key", "header": "X-Key", "value": "v"}) == {"X-Key": "v"}
    assert tools._auth_headers({"type": "custom_header", "header": "X-Auth", "value": "v"}) == {"X-Auth": "v"}


def test_named_loopback_requires_explicit_allow_loopback():
    agent = {"transports": {"a2a_rpc": {"url": "http://127.0.0.1:8081"}}}
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent):
        result = tools.handle_send_protocol_task(name="local", message="hello")
    assert "loopback" in result["error"]

    agent["transports"]["a2a_rpc"]["allow_loopback"] = True
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent), patch.object(
        tools,
        "_http_request",
        return_value={
            "result": {
                "id": "task",
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"type": "text", "text": "ok"}]}],
            }
        },
    ):
        result = tools.handle_send_protocol_task(name="local", message="hello")
    assert result["response"] == "ok"


def test_schemas_include_external_registration_parameters():
    props = schemas.A2A_DISCOVER["parameters"]["properties"]

    for key in ["register", "register_as", "rpc_url", "auth_token_env", "auth_value_env", "register_overwrite"]:
        assert key in props


def test_protocol_polling_working_then_completed(monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _: None)
    responses = [
        {"result": {"id": "task-1", "status": {"state": "working"}}},
        {"result": {"id": "task-1", "status": {"state": "completed"}, "artifacts": [{"parts": [{"type": "text", "text": "done"}]}]}},
    ]

    with patch.object(tools, "_http_request", side_effect=responses) as http:
        result = tools.handle_send_protocol_task(url="https://external.example/rpc", message="hello", poll_interval=0)

    assert result["state"] == "completed"
    assert result["response"] == "done"
    assert http.call_count == 2


def test_protocol_json_rpc_error_is_reported():
    with patch.object(tools, "_http_request", return_value={"error": {"message": "nope"}}):
        result = tools.handle_send_protocol_task(url="https://external.example/rpc", message="hello")

    assert result["error"] == "Remote agent error: nope"


def test_protocol_extracts_status_message_parts():
    with patch.object(
        tools,
        "_http_request",
        return_value={"result": {"id": "task", "status": {"state": "completed", "message": {"parts": [{"type": "text", "text": "status text"}]}}}},
    ):
        result = tools.handle_send_protocol_task(url="https://external.example/rpc", message="hello")

    assert result["response"] == "status text"


def test_protocol_skill_metadata_and_validation():
    agent = {
        "metadata": {"skills": [{"name": "summarize", "description": "Summarize"}]},
        "transports": {"a2a_rpc": {"url": "https://external.example/rpc"}},
    }
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent), patch.object(
        tools,
        "_http_request",
        return_value={"result": {"id": "task", "status": {"state": "completed"}, "artifacts": [{"parts": [{"type": "text", "text": "ok"}]}]}},
    ) as http:
        result = tools.handle_send_protocol_task(name="external", message="hello", skill="summarize")

    assert result["response"] == "ok"
    payload = http.call_args.kwargs["json_body"]
    assert payload["params"]["message"]["metadata"]["skill"] == "summarize"
    assert payload["params"]["metadata"]["skill"] == "summarize"

    with patch.object(tools, "_resolve_agent_by_name", return_value=agent):
        result = tools.handle_send_protocol_task(name="external", message="hello", skill="translate")

    assert "not found" in result["error"]
    assert result["available_skills"] == ["summarize"]


def test_cancel_protocol_task_posts_tasks_cancel():
    with patch.object(
        tools,
        "_http_request",
        return_value={"result": {"id": "task-1", "status": {"state": "canceled"}}},
    ) as http, patch("hermes_agent_a2a.tool_handlers.cancel_worker", return_value=False) as mock_cancel:
        result = tools.handle_cancel_protocol_task(url="https://external.example/rpc", task_id="task-1")

    assert result["state"] == "canceled"
    assert result["local_canceled"] is False
    mock_cancel.assert_called_once_with("task-1")
    payload = http.call_args.kwargs["json_body"]
    assert payload["method"] == "tasks/cancel"
    assert payload["params"]["id"] == "task-1"


def test_cancel_protocol_task_local_only_no_remote():
    with patch("hermes_agent_a2a.tool_handlers.cancel_worker", return_value=True) as mock_cancel:
        result = tools.handle_cancel_protocol_task(task_id="local-task-1")

    mock_cancel.assert_called_once_with("local-task-1")
    assert result["local_canceled"] is True
    assert result["state"] == "canceled"
    assert result["response"] == "canceled local worker"


def test_remote_worker_uses_hermes_remote_subprocess_metadata():
    agent = {"transports": {"a2a_rpc": {"url": "https://worker.example/rpc"}}}
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent), patch.object(
        tools,
        "_http_request",
        return_value={"result": {"id": "task-1", "status": {"state": "completed"}, "artifacts": [{"parts": [{"type": "text", "text": "done"}]}]}},
    ) as http:
        result = tools.handle_run_remote_agent_task(name="worker", message="do it", task_id="task-1")

    assert result["response"] == "done"
    payload = http.call_args.kwargs["json_body"]
    hermes = payload["params"]["message"]["metadata"]["hermes"]
    assert hermes["route"] == "worker"
    assert hermes["execution"] == "remote_subprocess"
    assert payload["params"]["message"]["metadata"]["worker_at"] == "target"


def test_local_worker_returns_hermes_local_subprocess_envelope(tmp_path, monkeypatch):
    hermes_home = tmp_path
    agent_home = hermes_home / "profiles" / "agent1"
    agent_home.mkdir(parents=True)
    (hermes_home / "hermes-agent").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home / "profiles" / "agent0"))

    from unittest.mock import MagicMock
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ('{"response": "local done"}', "")
    mock_proc.returncode = 0
    with patch.object(tools.subprocess, "Popen", return_value=mock_proc):
        result = tools.handle_run_local_agent_task(name="agent1", message="do local", task_id="local-task-1")

    assert result["task_id"] == "local-task-1"
    assert result["state"] == "completed"
    assert result["response"] == "local done"
    assert result["hermes"]["route"] == "worker"
    assert result["hermes"]["execution"] == "local_subprocess"
    assert result["hermes"]["isolation"] == "local_profile"
    assert result["a2a_envelope"]["method"] == "tasks/send"


def test_a2a_spec_builds_protocol_task_payload_with_hermes_metadata():
    payload = build_task_send_payload(
        task_id="task-1",
        message="hello",
        sender_name="agent0",
        skill="summarize",
        hermes=build_hermes_metadata(route="protocol", execution="remote_a2a"),
        request_id="rpc-1",
    )

    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "tasks/send"
    assert payload["id"] == "rpc-1"
    assert payload["params"]["message"]["metadata"]["skill"] == "summarize"
    assert payload["params"]["message"]["metadata"]["hermes"]["route"] == "protocol"


def test_a2a_spec_parses_artifacts_status_message_and_cancel_payload():
    parsed = parse_task_result(
        {
            "id": "task-1",
            "status": {"state": "completed", "message": {"parts": [{"type": "text", "text": "status"}]}},
            "artifacts": [{"parts": [{"type": "text", "text": "artifact"}]}],
        }
    )

    assert parsed["task_id"] == "task-1"
    assert parsed["state"] == "completed"
    assert parsed["response"] == "artifact\nstatus"
    assert build_task_cancel_payload("task-1", request_id="rpc-cancel") == {
        "jsonrpc": "2.0",
        "id": "rpc-cancel",
        "method": "tasks/cancel",
        "params": {"id": "task-1"},
    }


def test_session_schema_and_help_are_explicitly_one_way():
    description = schemas.A2A_TELEGRAM["description"]
    help_text = "\n".join(tools.handle_help("sessions")["guidance"])

    assert "one-way" in description
    assert "delivery/relay status only" in description
    assert "one-way" in help_text
    assert "does not wait for or guarantee" in help_text


def test_session_message_returns_a2a_shaped_delivery_ack(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"delivery_id": "delivery-1"}'

    agent = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
                "auth": {"type": "hmac", "secret": "secret"},
            }
        }
    }
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent), patch.object(
        urllib.request,
        "urlopen",
        return_value=FakeResponse(),
    ):
        result = tools.handle_send_session_message(message="hello", agent="agent1", task_id="task-123456789")

    assert result["task_id"] == "task-123456789"
    assert result["state"] == "completed"
    assert result["delivery"] == "delivered"
    assert result["reply_expected"] is False
    assert result["hermes"]["route"] == "session"
    assert result["hermes"]["delivery"] == "one_way"
    assert result["a2a_envelope"]["method"] == "tasks/send"
    assert result["a2a_envelope"]["params"]["message"]["metadata"]["expected_action"] == "acknowledge"


def test_worker_registry_cancels_running_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        register_worker("task-cancel", proc)
        assert cancel_worker("task-cancel") is True
        assert proc.poll() is not None
    finally:
        unregister_worker("task-cancel")
        if proc.poll() is None:
            proc.kill()


def test_worker_registry_returns_false_for_unknown_task():
    assert cancel_worker("missing-task") is False


def test_derive_hermes_home_from_root_path(tmp_path, monkeypatch):
    """Test _derive_hermes_home when HERMES_HOME points to root directory."""
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    (hermes_root / "hermes-agent").mkdir()
    
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    
    result = tools._derive_hermes_home()
    assert result == str(hermes_root)


def test_derive_hermes_home_from_profile_path(tmp_path, monkeypatch):
    """Test _derive_hermes_home when HERMES_HOME points to profile directory."""
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    (hermes_root / "hermes-agent").mkdir()
    profile_path = hermes_root / "profiles" / "agent0"
    profile_path.mkdir(parents=True)
    
    monkeypatch.setenv("HERMES_HOME", str(profile_path))
    
    result = tools._derive_hermes_home()
    assert result == str(hermes_root)


def test_derive_hermes_home_fallback_to_default(tmp_path, monkeypatch):
    """Test _derive_hermes_home falls back to ~/.hermes when validation fails."""
    # Create a fake HERMES_HOME that doesn't have hermes-agent
    fake_home = tmp_path / "fake_hermes"
    fake_home.mkdir()
    
    # Create actual ~/.hermes with hermes-agent
    real_home = tmp_path / "home" / ".hermes"
    real_home.mkdir(parents=True)
    (real_home / "hermes-agent").mkdir()
    
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    
    result = tools._derive_hermes_home()
    assert result == str(real_home)


def test_derive_hermes_home_raises_error_on_invalid_path(tmp_path, monkeypatch):
    """Test _derive_hermes_home raises ValueError when no valid path exists."""
    fake_home = tmp_path / "fake_hermes"
    fake_home.mkdir()
    
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nonexistent")
    
    try:
        tools._derive_hermes_home()
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Cannot find Hermes installation" in str(e)


def test_validate_agent_webhook_config_valid():
    """Test _validate_agent_webhook_config with valid configuration."""
    agent_info = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
                "auth": {"type": "hmac", "secret": "test-secret"},
            }
        }
    }
    
    is_valid, error = tools._validate_agent_webhook_config(agent_info)
    assert is_valid is True
    assert error == ""


def test_validate_agent_webhook_config_missing_url():
    """Test _validate_agent_webhook_config with missing webhook URL."""
    agent_info = {
        "transports": {
            "hermes_webhook": {
                "auth": {"type": "hmac", "secret": "test-secret"},
            }
        }
    }
    
    is_valid, error = tools._validate_agent_webhook_config(agent_info)
    assert is_valid is False
    assert "no hermes_webhook.url" in error.lower()


def test_validate_agent_webhook_config_missing_secret():
    """Test _validate_agent_webhook_config with missing webhook secret."""
    agent_info = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
            }
        }
    }
    
    is_valid, error = tools._validate_agent_webhook_config(agent_info)
    assert is_valid is False
    assert "no hermes_webhook.secret" in error.lower()


def test_validate_agent_webhook_config_empty_secret():
    """Test _validate_agent_webhook_config with empty webhook secret."""
    agent_info = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
                "auth": {"type": "hmac", "secret": ""},
            }
        }
    }
    
    is_valid, error = tools._validate_agent_webhook_config(agent_info)
    assert is_valid is False
    assert "no hermes_webhook.secret" in error.lower()


def test_validate_agent_webhook_config_missing_transport():
    """Test _validate_agent_webhook_config with missing hermes_webhook transport."""
    agent_info = {
        "transports": {}
    }
    
    is_valid, error = tools._validate_agent_webhook_config(agent_info)
    assert is_valid is False
    assert "no hermes_webhook.url" in error.lower()


def test_task_queue_get_task_metadata_pending():
    """Test TaskQueue.get_task_metadata() for pending tasks."""
    from hermes_agent_a2a.server import TaskQueue
    
    queue = TaskQueue()
    task_id = "task-1"
    metadata = {"sender_name": "agent0", "original_text": "test message"}
    
    queue.enqueue(task_id, "test text", metadata)
    
    result = queue.get_task_metadata(task_id)
    assert result == metadata


def test_task_queue_get_task_metadata_completed():
    """Test TaskQueue.get_task_metadata() for completed tasks."""
    from hermes_agent_a2a.server import TaskQueue
    
    queue = TaskQueue()
    task_id = "task-1"
    metadata = {"sender_name": "agent0", "original_text": "test message"}
    
    queue.enqueue(task_id, "test text", metadata)
    queue.complete(task_id, "response")
    
    result = queue.get_task_metadata(task_id)
    assert result == metadata


def test_task_queue_get_task_metadata_nonexistent():
    """Test TaskQueue.get_task_metadata() for non-existent tasks."""
    from hermes_agent_a2a.server import TaskQueue
    
    queue = TaskQueue()
    
    result = queue.get_task_metadata("nonexistent")
    assert result == {}


def test_task_queue_get_all_task_metadata():
    """Test TaskQueue.get_all_task_metadata() returns all tasks."""
    from hermes_agent_a2a.server import TaskQueue
    
    queue = TaskQueue()
    metadata1 = {"sender_name": "agent0", "original_text": "message1"}
    metadata2 = {"sender_name": "agent1", "original_text": "message2"}
    
    queue.enqueue("task-1", "text1", metadata1)
    queue.enqueue("task-2", "text2", metadata2)
    
    result = queue.get_all_task_metadata()
    assert result["task-1"] == metadata1
    assert result["task-2"] == metadata2


def test_task_queue_get_processing_tasks():
    """Test TaskQueue.get_processing_tasks() returns correct list."""
    from hermes_agent_a2a.server import TaskQueue
    
    queue = TaskQueue()
    queue.enqueue("task-1", "text1", {})
    queue.enqueue("task-2", "text2", {})
    
    queue.mark_processing("task-1")
    
    result = queue.get_processing_tasks()
    assert result == ["task-1"]


def test_task_queue_requeue_tasks():
    """Test TaskQueue.requeue_tasks() only adds tasks not in pending or processing."""
    from hermes_agent_a2a.server import TaskQueue
    
    queue = TaskQueue()
    queue.enqueue("task-1", "text1", {})
    
    # Try to re-queue a task that's already pending
    task = queue.drain_pending()[0]
    queue.requeue_tasks([task])
    
    # Should still only have 1 task (not duplicated)
    assert queue.pending_count() == 1
    
    # Mark as processing
    queue.mark_processing("task-1")
    
    # Try to re-queue a task that's processing
    queue.requeue_tasks([task])
    
    # Should still have 1 task (not added back since it's processing)
    assert queue.pending_count() == 1
    
    # Complete the task
    queue.complete("task-1", "done")
    
    # Now re-queue it
    queue.requeue_tasks([task])
    
    # Should have 1 pending task again
    assert queue.pending_count() == 1


def test_cleanup_zombie_processes_removes_finished():
    """Test cleanup_zombie_processes() removes finished processes."""
    proc = subprocess.Popen([sys.executable, "-c", "print('done')"])
    proc.wait()  # Process finishes immediately
    
    register_worker("zombie-task", proc)
    
    cleaned = cleanup_zombie_processes()
    assert cleaned == 1
    
    # Process should no longer be in registry
    assert cancel_worker("zombie-task") is False


def test_cleanup_zombie_processes_skips_active():
    """Test cleanup_zombie_processes() doesn't remove active processes."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    
    register_worker("active-task", proc)
    
    cleaned = cleanup_zombie_processes()
    assert cleaned == 0
    
    # Process should still be in registry
    assert cancel_worker("active-task") is True
    
    # Cleanup
    unregister_worker("active-task")
    if proc.poll() is None:
        proc.kill()


def test_cleanup_zombie_processes_empty_registry():
    """Test cleanup_zombie_processes() with empty registry."""
    cleaned = cleanup_zombie_processes()
    assert cleaned == 0


def test_session_message_rejects_invalid_webhook_config(monkeypatch):
    """Test handle_send_session_message rejects invalid webhook configuration."""
    agent = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
                # Missing secret
            }
        }
    }
    
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent):
        result = tools.handle_send_session_message(message="hello", agent="agent1")
    
    assert "error" in result
    assert "webhook configuration invalid" in result["error"].lower()


def test_session_message_rejects_missing_webhook_url(monkeypatch):
    """Test handle_send_session_message rejects missing webhook URL."""
    agent = {
        "transports": {
            "hermes_webhook": {
                "auth": {"type": "hmac", "secret": "test-secret"},
            }
        }
    }
    
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent):
        result = tools.handle_send_session_message(message="hello", agent="agent1")
    
    assert "error" in result
    assert "webhook configuration invalid" in result["error"].lower()


def test_session_message_validates_before_delivery(monkeypatch):
    """Test handle_send_session_message validates config before attempting delivery."""
    agent = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
                "auth": {"type": "hmac", "secret": "test-secret"},
            }
        }
    }
    
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return b'{"delivery_id": "delivery-1"}'
    
    with patch.object(tools, "_resolve_agent_by_name", return_value=agent), patch.object(
        urllib.request, "urlopen", return_value=FakeResponse()
    ):
        result = tools.handle_send_session_message(message="hello", agent="agent1")
    
    assert result["state"] == "completed"
    assert result["delivery"] == "delivered"
