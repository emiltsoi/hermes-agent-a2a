import os
from unittest.mock import patch

import yaml

from hermes_agent_a2a import schemas
from hermes_agent_a2a import tool_handlers as tools
from hermes_agent_a2a import tool_registry
from hermes_agent_a2a.a2a_spec import build_hermes_metadata, build_task_cancel_payload, build_task_send_payload, parse_task_result
from hermes_agent_a2a.identity import resolve_agent


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
        "a2a_run_local_agent_task",
        "a2a_run_remote_agent_task",
        "a2a_send_session_message",
    }
    assert all(entry["toolset"] == "a2a" for entry in registry.tools.values())


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
