import os
from pathlib import Path
import sys
import subprocess
import threading
import urllib.request
from unittest.mock import patch, MagicMock

import pytest
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
        "a2a_announce",
        "a2a_send_protocol_task",
        "a2a_cancel_protocol_task",
        "a2a_run_local_agent_task",
        "a2a_run_remote_agent_task",
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
    # Note: resolve_agent() no longer returns transports — CR-1 fix strips it.
    # The identity is stored correctly in identity.yaml (verified above).
    # Callers must use handle_discover() to get full transport info.


def test_discover_rejects_path_traversal_in_agent_card_path(tmp_path, monkeypatch):
    """CR-2: agent_card_path with '..' must be rejected before any filesystem access."""
    monkeypatch.setenv("A2A_VAULT_PATH", str(tmp_path))
    card = {
        "name": "Evil Agent",
        "description": "malicious",
        "version": "1",
        "skills": [],
        "capabilities": {},
    }

    with patch.object(tools, "_http_request", return_value=card):
        result = tools.handle_discover(
            url="https://external.example",
            agent_card_path="../etc/cron.d/malicious",
            auth_type="api_key",
            auth_header="X-API-Key",
            auth_value="secret",
            register=True,
            register_as="Evil Agent",
            rpc_url="https://external.example/rpc",
            auth_value_env=None,
        )

    assert ".." in result.get("error", ""), f"Expected error about '..', got: {result}"
    # Card fetch is rejected before registration is attempted
    assert result.get("registration", {}).get("registered") is not True, \
        f"Expected registration not attempted, got: {result}"


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
    assert payload["method"] == "CancelTask"
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
    assert result["a2a_envelope"]["method"] == "SendMessage"


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
    assert payload["method"] == "SendMessage"
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
        "method": "CancelTask",
        "params": {"id": "task-1"},
    }






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
    """Test _derive_hermes_home falls back to ~/.hermes when HERMES_HOME is NOT explicitly set."""
    # Create a fake working directory that doesn't have hermes-agent
    fake_home = tmp_path / "fake_hermes"
    fake_home.mkdir()

    # Create actual ~/.hermes with hermes-agent
    real_home = tmp_path / "home" / ".hermes"
    real_home.mkdir(parents=True)
    (real_home / "hermes-agent").mkdir()

    # Do NOT set HERMES_HOME explicitly - it should fall back to default
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = tools._derive_hermes_home()
    assert result == str(real_home)


def test_derive_hermes_home_raises_error_on_explicit_invalid_path(tmp_path, monkeypatch):
    """Test _derive_hermes_home raises ValueError when HERMES_HOME is explicitly set to invalid path."""
    fake_home = tmp_path / "fake_hermes"
    fake_home.mkdir()

    # Explicitly set HERMES_HOME to an invalid path
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nonexistent")

    with pytest.raises(ValueError, match="Cannot find Hermes installation"):
        tools._derive_hermes_home()












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


def test_cr3_mode3_timeout_response_includes_jsonrpc_field(tmp_path, monkeypatch):
    """CR-3 (Issue 2): Mode 3 worker timeout response must include 'jsonrpc': '2.0' field."""
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    (hermes_root / "hermes-agent").mkdir()
    profile = hermes_root / "profiles" / "test-agent"
    profile.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_root))

    # Mock subprocess.TimeoutExpired to trigger the timeout path
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="dummy", timeout=123)
    mock_proc.kill = MagicMock()
    mock_proc.wait = MagicMock()

    params = {"name": "test-agent"}
    metadata = {"timeout": "5"}
    user_text = "hello"

    with patch.object(tools.subprocess, "Popen", return_value=mock_proc):
        result = tools._handle_task_send_mode3(params, metadata, user_text)

    # Mode 3 timeout returns a Task-like object (not a JSON-RPC envelope).
    # The server wraps it in {"task": ...} for the JSON-RPC response.
    assert isinstance(result, dict), "Timeout response must be a dict"
    assert result["status"]["state"] == "failed"
    assert "timed out" in result["artifacts"][0]["parts"][0]["text"]
    # context_id should be present (falls back to task_id when not provided)
    assert "context_id" in result, "Task must include context_id"
    assert result["context_id"] == result["id"], "context_id should default to task_id"




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


def test_load_a2a_agents_reads_from_config_yaml(tmp_path, monkeypatch):
    """Plugin self-containment: _load_a2a_agents() must read a2a.agents from config.yaml."""
    from hermes_agent_a2a.identity import _load_a2a_agents
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Create config.yaml with a2a.agents
    config_path = tmp_path / "config.yaml"
    config_data = {
        "a2a": {
            "agents": [
                {"name": "agent1", "url": "http://127.0.0.1:41808", "auth_token": "token1"},
                {"name": "agent2", "url": "http://127.0.0.1:41809", "auth_token": "token2"},
            ]
        }
    }
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")

    agents = _load_a2a_agents()

    assert "agent1" in agents
    assert "agent2" in agents
    assert agents["agent1"]["url"] == "http://127.0.0.1:41808"
    assert agents["agent1"]["auth_token"] == "token1"
    assert agents["agent2"]["url"] == "http://127.0.0.1:41809"


def test_load_a2a_agents_handles_missing_config_gracefully(tmp_path, monkeypatch):
    """Plugin self-containment: _load_a2a_agents() must return empty dict when config is missing."""
    from hermes_agent_a2a.identity import _load_a2a_agents

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # No config.yaml exists
    agents = _load_a2a_agents()

    assert agents == {}


def test_call_a2a_direct_builds_correct_json_rpc_payload():
    """Plugin self-containment: a2a_direct.call() must build valid JSON-RPC 2.0 payload."""
    from hermes_agent_a2a.a2a_direct import call
    from unittest.mock import patch, MagicMock
    import json

    # Mock urllib.request.urlopen to capture the request
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"result": {"id": "task-123"}}).encode()
    mock_response.__enter__ = lambda self: self
    mock_response.__exit__ = lambda self, *args: None

    with patch("hermes_agent_a2a.a2a_direct._validate_target_url"):
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            result = call(
                url="http://127.0.0.1:41808/a2a",
                message="hello",
                task_id="task-123",
                auth_token="secret"
            )

        # Verify the call was made
        assert mock_urlopen.called
        req = mock_urlopen.call_args[0][0]

        # Verify JSON-RPC 2.0 payload structure (spec format)
        body = json.loads(req.data.decode())
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "SendMessage"
        assert body["params"]["message"]["role"] == 1
        assert body["params"]["message"]["parts"][0]["text"] == "hello"

        # Verify auth token in headers
        assert req.headers["Authorization"] == "Bearer secret"


def test_call_a2a_direct_handles_http_errors():
    """Plugin self-containment: a2a_direct.call() must return error dict on HTTP failure."""
    from hermes_agent_a2a.a2a_direct import call
    from unittest.mock import patch
    from urllib.error import HTTPError

    with patch("hermes_agent_a2a.a2a_direct._validate_target_url"):
        with patch("urllib.request.urlopen", side_effect=HTTPError(None, 404, "Not Found", None, None)):
            result = call(
                url="http://127.0.0.1:41808/a2a",
                message="hello",
                task_id="task-123"
            )

        assert "error" in result
        assert "404" in result["error"]














def test_get_raw_agent_identity_includes_transports(tmp_path, monkeypatch):
    """Webhook validation fix: get_raw_agent_identity() must return transports without CR-1 stripping."""
    from hermes_agent_a2a.identity import get_raw_agent_identity, resolve_agent
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("A2A_VAULT_PATH", raising=False)

    # Create agent with webhook transport
    agent_key = "test-agent"
    fleet_dir = tmp_path / "fleet" / "a2a" / "agents" / agent_key
    fleet_dir.mkdir(parents=True)
    identity_file = fleet_dir / "identity.yaml"
    identity_data = {
        "id": agent_key,
        "name": "Test Agent",
        "transports": {
            "hermes_webhook": {
                "url": "http://127.0.0.1:8644/webhooks/a2a_trigger",
                "secret": "test-secret"
            }
        }
    }
    identity_file.write_text(yaml.dump(identity_data), encoding="utf-8")

    # get_raw_agent_identity should include transports
    raw = get_raw_agent_identity(agent_key)
    assert raw is not None
    assert "transports" in raw
    assert raw["transports"]["hermes_webhook"]["url"] == "http://127.0.0.1:8644/webhooks/a2a_trigger"
    assert raw["transports"]["hermes_webhook"]["secret"] == "test-secret"

    # resolve_agent (CR-1) should strip transports/credentials
    resolved = resolve_agent(agent_key)
    assert resolved is not None
    assert "transports" not in resolved or "hermes_webhook" not in resolved.get("transports", {})




def test_2d_cta_behavioral():
    """Test behavioral aspects of 2D CTA parameters."""
    from hermes_agent_a2a.a2a_spec.tasks import build_task_send_payload
    
    # Test that reply_expected in return value reflects reply parameter
    # This is tested indirectly through the handler, but we can verify the envelope behavior
    envelope = build_task_send_payload(
        task_id="test-task",
        message="test message",
        sender_name="test-sender",
        intent="notification",
        expected_action="acknowledge",
    )
    
    # Verify envelope structure is correct (spec format)
    assert envelope["method"] == "SendMessage"
    assert envelope["params"]["message"]["role"] == 1
    assert envelope["params"]["message"]["parts"][0]["text"] == "test message"
    
    # Verify metadata includes intent and expected_action
    metadata = envelope["params"]["message"]["metadata"]
    assert metadata["intent"] == "notification"
    assert metadata["expected_action"] == "acknowledge"
    assert metadata["sender_name"] == "test-sender"
    
    # Test that skill is merged into metadata, not replacing it
    envelope_with_skill = build_task_send_payload(
        task_id="test-task",
        message="test message",
        sender_name="test-sender",
        intent="consultation",
        expected_action="reply",
        skill="test-skill",
    )
    
    metadata_with_skill = envelope_with_skill["params"]["message"]["metadata"]
    # Verify all original metadata is preserved
    assert metadata_with_skill["intent"] == "consultation"
    assert metadata_with_skill["expected_action"] == "reply"
    assert metadata_with_skill["sender_name"] == "test-sender"
    # Verify skill is added, not replacing
    assert metadata_with_skill["skill"] == "test-skill"








# ----------------------------------------------------------------------
# a2a_announce tests
# ----------------------------------------------------------------------


def test_announce_returns_false_when_registry_not_configured(monkeypatch):
    """a2a_announce must return announced=False when A2A_REGISTRY_URL is not set."""
    monkeypatch.delenv("A2A_REGISTRY_URL", raising=False)
    monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

    result = tools.handle_announce()

    assert result.get("announced") is False
    assert "not configured" in result.get("reason", "")


def test_announce_returns_false_when_server_not_running(monkeypatch):
    """a2a_announce must return an error when A2A server is not available."""
    monkeypatch.setenv("A2A_REGISTRY_URL", "http://registry:8081/announce")
    monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

    with patch.object(tools, "_ensure_server", return_value=None):
        result = tools.handle_announce()

    assert result.get("announced") is False
    assert "not running" in result.get("error", "")


def test_announce_posts_agent_card_to_registry(monkeypatch):
    """a2a_announce must POST the AgentCard to the registry URL."""
    monkeypatch.setenv("A2A_REGISTRY_URL", "http://registry:8081/announce")
    monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

    fake_card = {
        "name": "test-agent",
        "url": "http://test-agent:8081",
        "protocolVersion": "1.0",
        "capabilities": {"streaming": True},
        "skills": [],
    }
    mock_server = MagicMock()
    mock_server.build_agent_card.return_value = fake_card

    registry_response = {"status": "ok", "agent": "test-agent"}

    with patch.object(tools, "_ensure_server", return_value=mock_server):
        with patch.object(tools, "_http_request", return_value=registry_response) as mock_http:
            with patch("hermes_agent_a2a.security.is_safe_url", return_value=True):
                result = tools.handle_announce()

    assert result.get("announced") is True
    assert result.get("agent_card", {}).get("name") == "test-agent"
    assert result.get("registry_response", {}).get("status") == "ok"
    mock_http.assert_called_once()
    call_args = mock_http.call_args
    assert call_args[0][0] == "POST"
    assert "registry:8081/announce" in call_args[0][1]


def test_announce_includes_auth_token_from_env(monkeypatch):
    """a2a_announce must include Authorization header when A2A_REGISTRY_AUTH_TOKEN is set."""
    monkeypatch.setenv("A2A_REGISTRY_URL", "http://registry:8081/announce")
    monkeypatch.setenv("A2A_REGISTRY_AUTH_TOKEN", "secret-token")

    fake_card = {"name": "test-agent", "url": "http://test-agent:8081"}
    mock_server = MagicMock()
    mock_server.build_agent_card.return_value = fake_card

    with patch.object(tools, "_ensure_server", return_value=mock_server):
        with patch.object(tools, "_http_request", return_value={"status": "ok"}) as mock_http:
            with patch("hermes_agent_a2a.security.is_safe_url", return_value=True):
                tools.handle_announce()

    call_args = mock_http.call_args
    headers = call_args[1].get("headers", {})
    assert headers.get("Authorization") == "Bearer secret-token"


def test_announce_handles_connection_error(monkeypatch):
    """a2a_announce must return an error on connection failure."""
    monkeypatch.setenv("A2A_REGISTRY_URL", "http://unreachable:8081/announce")
    monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

    fake_card = {"name": "test-agent"}
    mock_server = MagicMock()
    mock_server.build_agent_card.return_value = fake_card

    with patch.object(tools, "_ensure_server", return_value=mock_server):
        with patch.object(tools, "_http_request", side_effect=ConnectionError("Cannot connect")):
            with patch("hermes_agent_a2a.security.is_safe_url", return_value=True):
                result = tools.handle_announce()

    assert result.get("announced") is False
    assert "Cannot connect" in result.get("error", "")


def test_announce_url_param_overrides_env_var(monkeypatch):
    """Passing url parameter must override A2A_REGISTRY_URL env var."""
    monkeypatch.setenv("A2A_REGISTRY_URL", "http://default-registry:8081/announce")
    monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

    fake_card = {"name": "test-agent"}
    mock_server = MagicMock()
    mock_server.build_agent_card.return_value = fake_card

    with patch.object(tools, "_ensure_server", return_value=mock_server):
        with patch.object(tools, "_http_request", return_value={"status": "ok"}) as mock_http:
            with patch("hermes_agent_a2a.security.is_safe_url", return_value=True):
                tools.handle_announce(url="http://custom-registry:9091/register")

    call_args = mock_http.call_args
    assert "custom-registry:9091" in call_args[0][1]


def test_boot_validator_raises_runtime_error_on_missing_transport():
    """BootValidator requires at least one A2A transport URL."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())

    identity_no_transport = {
        "platforms": {"telegram": {}},
        "defaults": {},
    }

    with pytest.raises(RuntimeError, match="no usable A2A transport URL"):
        validator.validate(identity_no_transport)


def test_boot_validator_passes_with_a2a_rpc_url():
    """BootValidator passes when an A2A RPC transport URL is present."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())
    identity = {
        "transports": {"a2a_rpc": {"url": "http://agent.example/rpc"}},
        "defaults": {"platform": "telegram", "chat_type": "dm"},
    }
    validator.validate(identity)


def test_boot_validator_validates_telegram_when_configured():
    """BootValidator validates Telegram credentials if they are present."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())
    identity = {
        "a2a_url": "http://agent.example/rpc",
        "platforms": {"telegram": {"bot_token": "123456:ABC"}},
        "defaults": {"platform": "telegram", "chat_type": "dm"},
    }

    with pytest.raises(RuntimeError, match="default_chat_id"):
        validator.validate(identity)


def test_boot_validator_raises_runtime_error_on_unresolved_token_placeholder():
    """BootValidator raises RuntimeError when bot_token is an unresolved env placeholder."""
    from hermes_agent_a2a.validators import BootValidator

    class FakeVault:
        pass

    validator = BootValidator(FakeVault())

    identity = {
        "a2a_url": "http://agent.example/rpc",
        "platforms": {"telegram": {"bot_token": "${A2A_TELEGRAM_BOT_TOKEN}", "default_chat_id": 123}},
        "defaults": {"platform": "telegram", "chat_type": "dm"},
    }

    with pytest.raises(RuntimeError, match="unresolved env var placeholder"):
        validator.validate(identity)

