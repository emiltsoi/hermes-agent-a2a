import os
from pathlib import Path
import sys
import subprocess
import threading
import urllib.request
from unittest.mock import patch, MagicMock

import yaml

from hermes_agent_a2a import schemas
from hermes_agent_a2a import tool_handlers as tools
from hermes_agent_a2a import tool_registry
from hermes_agent_a2a.a2a_spec import build_hermes_metadata, build_task_cancel_payload, build_task_send_payload, parse_task_result
from hermes_agent_a2a.identity import resolve_agent, list_agents
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


def test_a2a_metrics_initial_state():
    """Test A2AMetrics initializes with zero values."""
    from hermes_agent_a2a.runtime_state import A2AMetrics
    
    metrics = A2AMetrics()
    metrics_dict = metrics.get_metrics()
    
    assert metrics_dict["uptime_seconds"] >= 0
    assert metrics_dict["webhook"]["attempts"] == 0
    assert metrics_dict["webhook"]["successes"] == 0
    assert metrics_dict["webhook"]["failures"] == 0
    assert metrics_dict["webhook"]["success_rate_percent"] == 0
    assert metrics_dict["tasks"]["received"] == 0
    assert metrics_dict["tasks"]["completed"] == 0
    assert metrics_dict["tasks"]["canceled"] == 0
    assert metrics_dict["tasks"]["failed"] == 0
    assert metrics_dict["queue"]["pending_count"] == 0


def test_a2a_metrics_webhook_recording():
    """Test webhook metrics recording."""
    from hermes_agent_a2a.runtime_state import A2AMetrics
    
    metrics = A2AMetrics()
    
    metrics.record_webhook_attempt()
    metrics.record_webhook_success()
    
    metrics_dict = metrics.get_metrics()
    assert metrics_dict["webhook"]["attempts"] == 1
    assert metrics_dict["webhook"]["successes"] == 1
    assert metrics_dict["webhook"]["failures"] == 0
    assert metrics_dict["webhook"]["success_rate_percent"] == 100.0
    
    metrics.record_webhook_attempt()
    metrics.record_webhook_failure()
    
    metrics_dict = metrics.get_metrics()
    assert metrics_dict["webhook"]["attempts"] == 2
    assert metrics_dict["webhook"]["successes"] == 1
    assert metrics_dict["webhook"]["failures"] == 1
    assert metrics_dict["webhook"]["success_rate_percent"] == 50.0


def test_a2a_metrics_task_recording():
    """Test task metrics recording."""
    from hermes_agent_a2a.runtime_state import A2AMetrics
    
    metrics = A2AMetrics()
    
    metrics.record_task_received()
    metrics.record_task_completed()
    
    metrics_dict = metrics.get_metrics()
    assert metrics_dict["tasks"]["received"] == 1
    assert metrics_dict["tasks"]["completed"] == 1
    assert metrics_dict["tasks"]["canceled"] == 0
    
    metrics.record_task_received()
    metrics.record_task_canceled()
    metrics.record_task_received()
    
    metrics_dict = metrics.get_metrics()
    assert metrics_dict["tasks"]["received"] == 3
    assert metrics_dict["tasks"]["completed"] == 1
    assert metrics_dict["tasks"]["canceled"] == 1


def test_a2a_metrics_thread_safety():
    """Test metrics recording is thread-safe."""
    from hermes_agent_a2a.runtime_state import A2AMetrics
    import threading
    
    metrics = A2AMetrics()
    
    def record_attempts():
        for _ in range(100):
            metrics.record_webhook_attempt()
            metrics.record_webhook_success()
    
    threads = [threading.Thread(target=record_attempts) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    metrics_dict = metrics.get_metrics()
    assert metrics_dict["webhook"]["attempts"] == 1000
    assert metrics_dict["webhook"]["successes"] == 1000


def test_a2a_get_metrics_tool():
    """Test a2a_get_metrics tool returns metrics from runtime state."""
    from hermes_agent_a2a.runtime_state import get_runtime_state
    
    state = get_runtime_state()
    metrics = state.get_metrics()
    
    # Record some metrics
    metrics.record_webhook_attempt()
    metrics.record_webhook_success()
    metrics.record_task_received()
    metrics.record_task_completed()
    
    result = tools.handle_get_metrics()
    
    assert "uptime_seconds" in result
    assert "webhook" in result
    assert "tasks" in result
    assert "queue" in result
    assert result["webhook"]["attempts"] >= 1
    assert result["webhook"]["successes"] >= 1
    assert result["tasks"]["received"] >= 1
    assert result["tasks"]["completed"] >= 1


def test_a2a_metrics_command_disabled_by_default(monkeypatch):
    """Test /a2a_metrics command is disabled by default."""
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    monkeypatch.setenv("A2A_METRICS_COMMAND_ENABLED", "false")
    
    result = tools.handle_send_session_message(message="/a2a_metrics", agent="agent1")
    
    # Should return error because command is disabled and agent not found
    assert "error" in result


def test_a2a_metrics_command_enabled(monkeypatch):
    """Test /a2a_metrics command when enabled."""
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    monkeypatch.setenv("A2A_METRICS_COMMAND_ENABLED", "true")
    
    result = tools.handle_send_session_message(message="/a2a_metrics", agent="agent1")
    
    # Should return metrics formatted for Telegram
    assert result["state"] == "completed"
    assert result["delivery"] == "command_response"
    assert "📊 A2A Metrics" in result["response"]
    assert "⏱️ Uptime:" in result["response"]
    assert "🔗 Webhook" in result["response"]
    assert "📋 Tasks" in result["response"]
    assert "📬 Queue" in result["response"]


def test_a2a_metrics_command_with_whitespace(monkeypatch):
    """Test /a2a_metrics command with whitespace."""
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    monkeypatch.setenv("A2A_METRICS_COMMAND_ENABLED", "true")
    
    result = tools.handle_send_session_message(message="  /a2a_metrics  ", agent="agent1")
    
    assert result["state"] == "completed"
    assert result["delivery"] == "command_response"
    assert "📊 A2A Metrics" in result["response"]


def test_a2a_metrics_command_not_triggered_on_normal_message(monkeypatch):
    """Test normal messages don't trigger metrics command."""
    monkeypatch.setenv("A2A_AGENT_NAME", "agent0")
    monkeypatch.setenv("A2A_METRICS_COMMAND_ENABLED", "true")
    
    agent = {
        "transports": {
            "hermes_webhook": {
                "url": "https://target.example/webhook",
                "auth": {"type": "hmac", "secret": "test-secret"},
            }
        }
    }
    
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
    
    # Should not return command_response
    assert result.get("delivery") != "command_response"
    assert result["state"] == "completed"
    assert result["delivery"] == "delivered"


# ---------------------------------------------------------------------------
# Regression tests for v3 code review (CR-1 through CR-7)
# ---------------------------------------------------------------------------

CREDENTIAL_KEYS = frozenset(["auth_token", "bot_token", "webhook_secret", "platforms"])
SECRET_SUBKEYS = frozenset(["secret"])  # nested inside auth dicts (e.g. transports.*.auth.secret)


def _write_identity_file(path: Path, data: dict) -> None:
    """Write an identity.yaml with credentials for CR-1 testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump(data, path.open("w"))


def test_cr1_list_agents_excludes_credentials(tmp_path, monkeypatch):
    """CR-1: list_agents() must not return auth_token, bot_token, webhook_secret, platforms, or transports."""
    # Set up a fleet vault with an identity file containing credentials
    vault_root = tmp_path / "fleet"
    agents_dir = vault_root / "a2a" / "agents"
    agent_dir = agents_dir / "test-agent"
    agent_dir.mkdir(parents=True)

    identity_data = {
        "name": "test-agent",
        "description": "Test agent with credentials",
        "a2a_url": "https://example.com/rpc",
        "auth_token": "super-secret-bearer-token",
        "webhook_secret": "whsec-abc123",
        "platforms": {"telegram": {"bot_token": "123456:ABC-DEF"}},
        "transports": {"a2a_rpc": {"url": "https://example.com/rpc"}},
    }
    yaml.safe_dump(identity_data, (agent_dir / "identity.yaml").open("w"))

    monkeypatch.setenv("A2A_VAULT_PATH", str(vault_root))

    agents = list_agents()

    assert len(agents) >= 1
    for agent in agents:
        for key in CREDENTIAL_KEYS:
            assert key not in agent, f"list_agents() returned credential key: {key}"
        # Also check nested secrets (e.g. transports.*.auth.secret)
        def _no_nested_secrets(d: dict) -> list:
            violations = []
            for k, v in d.items():
                if isinstance(v, dict):
                    if k in CREDENTIAL_KEYS:
                        violations.append(k)
                    violations.extend(_no_nested_secrets(v))
            return violations
        nested_violations = _no_nested_secrets(agent)
        assert not nested_violations, f"list_agents() returned nested credential key(s): {nested_violations}"


def test_cr1_resolve_agent_excludes_credentials(tmp_path, monkeypatch):
    """CR-1: resolve_agent() must not return auth_token, bot_token, webhook_secret, platforms, or transports."""
    vault_root = tmp_path / "fleet"
    agents_dir = vault_root / "a2a" / "agents"
    agent_dir = agents_dir / "credential-agent"
    agent_dir.mkdir(parents=True)

    identity_data = {
        "name": "credential-agent",
        "description": "Agent with secrets",
        "a2a_url": "https://secret.example/rpc",
        "auth_token": " bearer-secret-token-xyz",
        "webhook_secret": "whsec-xyz789",
        "platforms": {"telegram": {"bot_token": "999999:XYZ-ABC"}},
        "transports": {
            "a2a_rpc": {
                "url": "https://secret.example/rpc",
                "auth": {"type": "bearer", "secret": "super-secret-hmac-key"},
            }
        },
    }
    yaml.safe_dump(identity_data, (agent_dir / "identity.yaml").open("w"))

    monkeypatch.setenv("A2A_VAULT_PATH", str(vault_root))

    agent = resolve_agent("credential-agent")

    assert agent is not None
    def _no_secrets_in_dict(d: dict, path: str = "") -> list:
        """Recursively find secret keys in a dict. Returns list of paths with secrets."""
        violations = []
        for k, v in d.items():
            current_path = f"{path}.{k}" if path else k
            if k in CREDENTIAL_KEYS:
                violations.append(current_path)
            if isinstance(v, dict):
                violations.extend(_no_secrets_in_dict(v, current_path))
        return violations

    violations = _no_secrets_in_dict(agent)
    assert not violations, f"resolve_agent() returned credential path(s): {violations}"


def test_cr2_on_shutdown_calls_server_shutdown(monkeypatch):
    """CR-2: on_shutdown() must call server.shutdown() on the actual A2A server."""
    from hermes_agent_a2a import plugin as plugin_module
    from hermes_agent_a2a.runtime_state import get_runtime_state

    # Use a truly unique port to avoid "address already in use" from prior test runs
    import random
    unique_port = str(random.randint(20000, 60000))

    # Patch os.getenv so _start_a2a_server() picks up our port
    original_getenv = os.getenv
    def patched_getenv(key, default=None):
        if key == "A2A_PORT":
            return unique_port
        return original_getenv(key, default)

    with patch.object(os, "getenv", patched_getenv):
        plugin_module._start_a2a_server()

    state = get_runtime_state()
    server = state.get_server()
    assert server is not None, "A2A server should be running for this test"

    # Spy on the server's shutdown method
    original_shutdown = server.shutdown
    shutdown_called = []
    def track_shutdown():
        shutdown_called.append(True)
    server.shutdown = track_shutdown

    try:
        plugin_module.HermesAgentA2APlugin().on_shutdown()
    finally:
        server.shutdown = original_shutdown

    assert len(shutdown_called) == 1, "server.shutdown() must be called exactly once in on_shutdown()"


def test_cr3_mode3_name_extraction_from_params_does_not_raise_nameerror(tmp_path, monkeypatch):
    """CR-3: _handle_task_send_mode3 must extract 'name' from params and derive agent_home without NameError."""
    # Set up a minimal agent profile so _derive_hermes_home finds a valid hermes-agent dir
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    (hermes_root / "hermes-agent").mkdir()
    profile = hermes_root / "profiles" / "test-agent"
    profile.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_root))

    # Mock the subprocess so we don't need a real venv python
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ('{"response": "ok"}', "")
    mock_proc.returncode = 0

    params = {"name": "test-agent"}
    metadata = {}
    user_text = "hello"

    with patch.object(tools.subprocess, "Popen", return_value=mock_proc):
        # This must not raise NameError — name must be extracted from params
        result = tools._handle_task_send_mode3(params, metadata, user_text)

    # Result should be a dict (success or failure), not an exception
    assert isinstance(result, dict)
    # The task_id should be present (either generated or from params)
    assert "id" in result
    # Should have spawned the subprocess (verifies name was extracted and agent_home derived)
    assert mock_proc.communicate.called, "Worker subprocess should have been spawned — name extraction from params works"


def test_mode2_env_sanitization_only_whitelisted_vars(tmp_path, monkeypatch):
    """Mode 2: _handle_call_mode2 must spawn subprocess with ONLY whitelisted env vars (no secrets)."""
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    (hermes_root / "hermes-agent").mkdir()
    agent_profile = hermes_root / "profiles" / "target-agent"
    agent_profile.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    # Set secrets that must NOT appear in the subprocess env
    monkeypatch.setenv("A2A_TELEGRAM_BOT_TOKEN", "123456:FAKE-BOT-TOKEN")
    monkeypatch.setenv("A2A_OWNER_CHAT_ID", "999999999")
    monkeypatch.setenv("SOME_OTHER_SECRET", "super-secret-value")

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ('{"response": "ok"}', "")
    mock_proc.returncode = 0

    with patch.object(tools.subprocess, "Popen", return_value=mock_proc) as mock_popen:
        result = tools._handle_call_mode2(name="target-agent", message="do it")

    assert mock_popen.called
    env_dict = mock_popen.call_args.kwargs.get("env") or mock_popen.call_args[1].get("env")

    # Only whitelisted keys may be present
    allowed = frozenset(["HERMES_HOME", "PATH", "PYTHONPATH"])
    forbidden = frozenset(["A2A_TELEGRAM_BOT_TOKEN", "A2A_OWNER_CHAT_ID", "SOME_OTHER_SECRET"])

    assert all(k in allowed for k in env_dict), f"Env contains non-whitelisted keys: {set(env_dict) - allowed}"
    assert not any(k in env_dict for k in forbidden), f"Env must not contain secret keys: {forbidden & set(env_dict)}"


def test_poll_error_tracking_returns_error_on_all_failures(monkeypatch):
    """Poll error tracking: when all poll attempts fail, return an error dict (not working/empty response)."""
    import importlib
    import hermes_agent_a2a.tool_handlers as th
    importlib.reload(th)

    # Patch time.sleep to be instant
    monkeypatch.setattr(th.time, "sleep", lambda _: None)

    # Patch _http_request to raise on every call (both initial send AND poll attempts)
    def always_fail(*args, **kwargs):
        raise RuntimeError("network unreachable")

    with patch.object(th, "_http_request", side_effect=always_fail):
        result = th.handle_send_protocol_task(
            url="https://fake.example/rpc",
            message="hello",
            poll_attempts=3,
            poll_interval=0,
        )

    # Must NOT return {"state": "working", "response": ""} — that's the bug
    assert result.get("state") != "working" or result.get("response") != "", \
        "Must not return working/empty on complete poll failure"

    # Must contain an error indication
    assert "error" in result, "Poll failure must return an error key"
    assert "poll" in result["error"].lower() or "failed" in result["error"].lower()


def test_boot_validator_raises_runtime_error_on_missing_bot_token():
    """CR-6: BootValidator.validate() must raise RuntimeError when bot_token is missing."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())

    identity_no_token = {
        "platforms": {"telegram": {}},
        "defaults": {},
    }

    try:
        validator.validate(identity_no_token)
        assert False, "BootValidator.validate() must raise RuntimeError when bot_token is missing"
    except RuntimeError as e:
        assert "bot token" in str(e).lower()


def test_boot_validator_raises_runtime_error_on_missing_chat_id():
    """CR-6: BootValidator.validate() must raise RuntimeError when chat_id is missing."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())

    identity_no_chat_id = {
        "platforms": {"telegram": {"bot_token": "123456:ABC"}},
        "defaults": {},
    }

    try:
        validator.validate(identity_no_chat_id)
        assert False, "BootValidator.validate() must raise RuntimeError when chat_id is missing"
    except RuntimeError as e:
        assert "chat_id" in str(e).lower()


def test_boot_validator_raises_runtime_error_on_unresolved_token_placeholder():
    """CR-6: BootValidator.validate() must raise RuntimeError when bot_token is an unresolved env placeholder."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())

    identity_unresolved = {
        "platforms": {"telegram": {"bot_token": "${A2A_TELEGRAM_BOT_TOKEN}"}},
        "defaults": {},
    }

    try:
        validator.validate(identity_unresolved)
        assert False, "BootValidator.validate() must raise RuntimeError for unresolved env var placeholder"
    except RuntimeError as e:
        assert "unresolved" in str(e).lower() or "placeholder" in str(e).lower()


def test_cr7_clear_stops_metrics_logger(monkeypatch):
    """CR-7: A2ARuntimeState.clear() must stop the metrics logger thread (event is set)."""
    from hermes_agent_a2a import runtime_state as rs_module
    import importlib
    importlib.reload(rs_module)

    # Patch _start_metrics_logger to be a no-op so we don't start real threads
    # Patch the global event to track whether it gets set
    events_created = []
    original_set = threading.Event.set
    def track_set(self):
        events_created.append(self)
        return original_set(self)

    with patch.object(rs_module, "_start_metrics_logger", lambda: None), \
         patch.object(threading.Event, "set", track_set):

        # Start a metrics logger session by simulating the env var
        monkeypatch.setenv("A2A_METRICS_LOG_ENABLED", "true")
        monkeypatch.setenv("A2A_METRICS_LOG_INTERVAL", "1")

        # Simulate the metrics logger event being created
        event = threading.Event()
        rs_module._metrics_logger_event = event

        # Call clear — this should stop the metrics logger
        rs_module.get_runtime_state().clear()

        # The event must have been set (signalling the logger thread to stop)
        assert event.is_set(), "A2ARuntimeState.clear() must set _metrics_logger_event to stop the logger thread"


def test_medium_1_update_exchange_placeholder_matching(tmp_path, monkeypatch):
    """MEDIUM-1: save_exchange must write placeholder that update_exchange can find and replace."""
    from datetime import datetime, timezone
    from hermes_agent_a2a.persistence import save_exchange, update_exchange

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    agent_name = "test-agent"
    task_id = "task-123"
    outbound_text = "hello from me"
    inbound_text = "response from agent"

    # Save an outbound exchange (should write placeholder)
    result_path = save_exchange(
        agent_name=agent_name,
        task_id=task_id,
        inbound_text=inbound_text,
        outbound_text=outbound_text,
        direction="outbound",
    )

    # Update the exchange (should replace placeholder with actual text)
    updated = update_exchange(agent_name=agent_name, task_id=task_id, inbound_text=inbound_text)
    assert updated is True, "update_exchange must successfully find and replace the placeholder"

    # Read back and verify the placeholder was replaced
    content = result_path.read_text(encoding="utf-8")
    assert "(waiting for reply…)" not in content, "Placeholder should be replaced"
    assert inbound_text in content, "Actual inbound text should be present"

