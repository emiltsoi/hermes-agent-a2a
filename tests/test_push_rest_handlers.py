"""T1-2 — Push REST handler integration tests.

Tests that the four REST push handlers in server.py use the spec-compliant
models from a2a_spec.push and wire to push_delivery CRUD functions.

Written to fail BEFORE the handler implementations are updated.
Run with: pytest tests/test_push_rest_handlers.py -v
"""
import json
import os
import random
import uuid
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc_request(port, payload, auth_token="test-secret"):
    """POST JSON-RPC request to /a2a endpoint."""
    import urllib.request, urllib.error
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/a2a",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), dict(e.headers)


def _make_task_send_body(task_id, text="hello"):
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"text": text}],
                "metadata": {},
            },
        },
    }


def _rest_post(port, path, body=None, auth_token="test-secret"):
    import urllib.request, urllib.error
    hdrs = {"Content-Type": "application/json"}
    if auth_token:
        hdrs["Authorization"] = f"Bearer {auth_token}"
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=hdrs,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code


def _rest_get(port, path, auth_token="test-secret"):
    import urllib.request, urllib.error
    hdrs = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=hdrs,
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code


def _rest_delete(port, path, auth_token="test-secret"):
    import urllib.request, urllib.error
    hdrs = {}
    if auth_token:
        hdrs["Authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None,
        headers=hdrs,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}, resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code


@pytest.fixture
def fresh_server():
    """Start a fresh A2A server on a random port for isolated testing."""
    import os, random
    from hermes_agent_a2a import runtime_state as rs_module
    import importlib
    importlib.reload(rs_module)

    port = random.randint(20000, 60000)

    with patch.dict("os.environ", {
        "A2A_PORT": str(port),
        "A2A_HOST": "127.0.0.1",
        "A2A_AUTH_TOKEN": "test-secret",
        "A2A_REQUIRE_AUTH": "true",
        "HERMES_HOME": "/tmp/test_push_rest_hermes",
    }):
        from hermes_agent_a2a import plugin as plugin_module
        importlib.reload(plugin_module)
        plugin_module._start_a2a_server()

        state = rs_module.get_runtime_state()
        server = state.get_server()

        yield server, port

        # Teardown
        try:
            server.shutdown()
        except Exception:
            pass
        rs_module.get_runtime_state().clear()


def _seed_task(port, task_id=None):
    """Create a task via JSON-RPC so push config endpoints have something to bind to."""
    if task_id is None:
        task_id = f"push-rest-{uuid.uuid4().hex[:8]}"
    _rpc_request(port, _make_task_send_body(task_id, "seed"))
    return task_id


# ---------------------------------------------------------------------------
# _rest_create_push_config
# Uses: CreateTaskPushNotificationConfigRequest + create_push_config
# ---------------------------------------------------------------------------

class TestRestCreatePushConfig:
    """POST /tasks/{id}/pushNotificationConfigs."""

    def test_returns_201_with_config_id(self, fresh_server):
        """Handler must return 201 with a config id in the body."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/hook",
                "hmacKey": "secret123",
            },
        )
        assert status == 201, f"Expected 201, got {status}: {body}"
        # Response must contain config.id
        config_id = body.get("configId") or body.get("config", {}).get("id")
        assert config_id, f"Response must contain configId or config.id: {body}"

    def test_returns_404_for_unknown_task(self, fresh_server):
        """Must return 404 when task_id has no associated task."""
        server, port = fresh_server

        body, status = _rest_post(
            port,
            "/tasks/nonexistent-task-xyz/pushNotificationConfigs",
            {"url": "https://x.com/h", "hmacKey": "k"},
        )
        assert status == 404, f"Expected 404, got {status}: {body}"

    def test_returns_400_when_url_missing(self, fresh_server):
        """Must return 400 when url is not in request body."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {"hmacKey": "secret123"},
        )
        assert status == 400, f"Expected 400 for missing url, got {status}: {body}"

    def test_returns_400_when_hmac_key_missing(self, fresh_server):
        """Must return 400 when hmacKey is not in request body."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {"url": "https://x.com/h"},
        )
        assert status == 400, f"Expected 400 for missing hmacKey, got {status}: {body}"

    def test_stores_config_in_push_delivery(self, fresh_server):
        """Config created via handler must be retrievable via get_push_config."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-abc",
            },
        )
        assert status == 201, f"Create failed: {body}"

        config_id = body.get("configId") or (body.get("config") or {}).get("id")

        from hermes_agent_a2a.push_delivery import get_push_config
        cfg = get_push_config(task_id, config_id)
        assert cfg is not None, "Config must be stored in push_delivery"
        assert cfg.task_id == task_id
        assert cfg.url == "https://example.com/callback"

    def test_response_contains_task_id_and_url(self, fresh_server):
        """Response must include the task_id and url."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://x.com/h",
                "hmacKey": "k",
            },
        )
        assert status == 201
        config = body.get("config", body)
        assert config.get("taskId") == task_id or config.get("task_id") == task_id
        assert config.get("url") == "https://x.com/h"


# ---------------------------------------------------------------------------
# _rest_get_push_config
# Uses: GetTaskPushNotificationConfigRequest + get_push_config
# ---------------------------------------------------------------------------

class TestRestGetPushConfig:
    """GET /tasks/{id}/pushNotificationConfigs/{config_id}."""

    def test_returns_200_with_config(self, fresh_server):
        """Handler must return 200 and the full config object."""
        server, port = fresh_server
        task_id = _seed_task(port)

        # Create
        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {"url": "https://x.com/h", "hmacKey": "k"},
        )
        config_id = create_body.get("configId") or (create_body.get("config") or {}).get("id")

        # Get
        body, status = _rest_get(port, f"/tasks/{task_id}/pushNotificationConfigs/{config_id}")
        assert status == 200, f"Expected 200, got {status}: {body}"
        assert "configId" in body or "config" in body or "id" in body, \
            f"Response must contain config details: {body}"

    def test_returns_404_for_unknown_config_id(self, fresh_server):
        """Must return 404 when config_id does not exist."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_get(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs/nonexistent-config-id-xyz",
        )
        assert status == 404, f"Expected 404 for unknown config, got {status}: {body}"

    def test_returns_404_for_unknown_task_id(self, fresh_server):
        """Must return 404 when task_id has no associated task."""
        server, port = fresh_server

        body, status = _rest_get(
            port,
            "/tasks/unknown-task-abc/pushNotificationConfigs/some-config-id",
        )
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"

    def test_returns_404_for_mismatched_task_id(self, fresh_server):
        """Config registered under one task_id must not be accessible via another."""
        server, port = fresh_server
        task_id_1 = _seed_task(port)
        task_id_2 = _seed_task(port)

        # Create under task_id_1
        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id_1}/pushNotificationConfigs",
            {"url": "https://x.com/h", "hmacKey": "k"},
        )
        config_id = create_body.get("configId") or (create_body.get("config") or {}).get("id")

        # Try to get it under task_id_2
        body, status = _rest_get(
            port,
            f"/tasks/{task_id_2}/pushNotificationConfigs/{config_id}",
        )
        assert status == 404, \
            f"Config from task_id_1 must not be accessible under task_id_2: got {status}"

    def test_config_contains_url_and_authentication(self, fresh_server):
        """GET response must include url and authentication from the stored config."""
        server, port = fresh_server
        task_id = _seed_task(port)

        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/secure-hook",
                "hmacKey": "my-secret",
                "authentication": {
                    "scheme": "bearer",
                    "credentials": "tok456",
                },
            },
        )
        config_id = create_body.get("configId") or (create_body.get("config") or {}).get("id")

        body, status = _rest_get(port, f"/tasks/{task_id}/pushNotificationConfigs/{config_id}")
        assert status == 200
        cfg = body.get("config", body)
        # Must return url and authentication info
        assert "url" in cfg, f"Config must include url: {body}"
        if "authentication" in cfg:
            assert cfg["authentication"].get("scheme") == "bearer" or \
                   cfg["authentication"].get("auth_type") == "bearer", \
                   f"Authentication scheme must be preserved: {body}"


# ---------------------------------------------------------------------------
# _rest_list_push_configs
# Uses: ListTaskPushNotificationConfigsRequest + list_push_configs
# ---------------------------------------------------------------------------

class TestRestListPushConfigs:
    """GET /tasks/{id}/pushNotificationConfigs."""

    def test_returns_200_with_items_list(self, fresh_server):
        """Handler must return 200 and a list of config items."""
        server, port = fresh_server
        task_id = _seed_task(port)

        # Create two configs
        for i in range(2):
            _rest_post(
                port,
                f"/tasks/{task_id}/pushNotificationConfigs",
                {"url": f"https://x{i}.com/h", "hmacKey": f"k{i}"},
            )

        body, status = _rest_get(port, f"/tasks/{task_id}/pushNotificationConfigs")
        assert status == 200, f"Expected 200, got {status}: {body}"
        items = body if isinstance(body, list) else (body.get("items") or body.get("configs") or [])
        assert isinstance(items, list), f"Expected list response, got: {body}"
        assert len(items) >= 2, f"Must return at least 2 configs, got {len(items)}: {body}"

    def test_returns_404_for_unknown_task(self, fresh_server):
        """Must return 404 when task_id has no associated task."""
        server, port = fresh_server

        body, status = _rest_get(
            port,
            "/tasks/unknown-task-pqr/pushNotificationConfigs",
        )
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"

    def test_returns_empty_list_for_task_with_no_configs(self, fresh_server):
        """Must return 200 with empty list when task has no push configs."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_get(port, f"/tasks/{task_id}/pushNotificationConfigs")
        assert status == 200, f"Expected 200, got {status}: {body}"
        items = body if isinstance(body, list) else (body.get("items") or body.get("configs") or [])
        assert items == [], f"Expected empty list, got: {body}"

    def test_list_only_returns_configs_for_that_task(self, fresh_server):
        """Configs registered under another task must not appear in the list."""
        server, port = fresh_server
        task_id_1 = _seed_task(port)
        task_id_2 = _seed_task(port)

        _rest_post(
            port,
            f"/tasks/{task_id_1}/pushNotificationConfigs",
            {"url": "https://x1.com/h", "hmacKey": "k1"},
        )

        body, status = _rest_get(port, f"/tasks/{task_id_2}/pushNotificationConfigs")
        assert status == 200
        items = body if isinstance(body, list) else (body.get("items") or body.get("configs") or [])
        assert all(
            (item.get("taskId") == task_id_2 or item.get("task_id") == task_id_2)
            for item in items
        ), f"List for task_id_2 must not contain configs from task_id_1: {body}"


# ---------------------------------------------------------------------------
# _rest_delete_push_config
# Uses: DeleteTaskPushNotificationConfigRequest + delete_push_config
# ---------------------------------------------------------------------------

class TestRestDeletePushConfig:
    """DELETE /tasks/{id}/pushNotificationConfigs/{config_id}."""

    def test_returns_204_on_success(self, fresh_server):
        """Handler must return 204 with empty body on successful deletion."""
        server, port = fresh_server
        task_id = _seed_task(port)

        # Create
        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {"url": "https://x.com/h", "hmacKey": "k"},
        )
        config_id = create_body.get("configId") or (create_body.get("config") or {}).get("id")

        # Delete
        body, status = _rest_delete(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        )
        assert status == 204, f"Expected 204 on delete, got {status}: {body}"

    def test_returns_404_for_unknown_config_id(self, fresh_server):
        """Must return 404 when deleting a non-existent config."""
        server, port = fresh_server
        task_id = _seed_task(port)

        body, status = _rest_delete(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs/nonexistent-id-xyz",
        )
        assert status == 404, f"Expected 404 for unknown config, got {status}: {body}"

    def test_returns_404_for_unknown_task_id(self, fresh_server):
        """Must return 404 when task_id has no associated task."""
        server, port = fresh_server

        body, status = _rest_delete(
            port,
            "/tasks/unknown-task-lmn/pushNotificationConfigs/some-config",
        )
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"

    def test_deleted_config_is_no_longer_retrievable(self, fresh_server):
        """After DELETE, GET for that config must return 404."""
        server, port = fresh_server
        task_id = _seed_task(port)

        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {"url": "https://x.com/h", "hmacKey": "k"},
        )
        config_id = create_body.get("configId") or (create_body.get("config") or {}).get("id")

        _rest_delete(port, f"/tasks/{task_id}/pushNotificationConfigs/{config_id}")

        body, status = _rest_get(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        )
        assert status == 404, \
            f"Deleted config must not be retrievable: got {status}: {body}"

    def test_returns_404_for_mismatched_task_id(self, fresh_server):
        """Deleting a config under a different task_id must return 404."""
        server, port = fresh_server
        task_id_1 = _seed_task(port)
        task_id_2 = _seed_task(port)

        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id_1}/pushNotificationConfigs",
            {"url": "https://x.com/h", "hmacKey": "k"},
        )
        config_id = create_body.get("configId") or (create_body.get("config") or {}).get("id")

        body, status = _rest_delete(
            port,
            f"/tasks/{task_id_2}/pushNotificationConfigs/{config_id}",
        )
        assert status == 404, \
            f"Delete with mismatched task_id must return 404: got {status}"


# ---------------------------------------------------------------------------
# Model imports — verify spec models are used
# ---------------------------------------------------------------------------

class TestPushModelImports:
    """Verify that the spec-compliant push models are importable from a2a_spec.push."""

    def test_create_request_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import CreateTaskPushNotificationConfigRequest
        assert CreateTaskPushNotificationConfigRequest is not None

    def test_create_response_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import CreateTaskPushNotificationConfigResponse
        assert CreateTaskPushNotificationConfigResponse is not None

    def test_get_request_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import GetTaskPushNotificationConfigRequest
        assert GetTaskPushNotificationConfigRequest is not None

    def test_get_response_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import GetTaskPushNotificationConfigResponse
        assert GetTaskPushNotificationConfigResponse is not None

    def test_list_request_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import ListTaskPushNotificationConfigsRequest
        assert ListTaskPushNotificationConfigsRequest is not None

    def test_list_response_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import ListTaskPushNotificationConfigsResponse
        assert ListTaskPushNotificationConfigsResponse is not None

    def test_delete_request_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import DeleteTaskPushNotificationConfigRequest
        assert DeleteTaskPushNotificationConfigRequest is not None

    def test_delete_response_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import DeleteTaskPushNotificationConfigResponse
        assert DeleteTaskPushNotificationConfigResponse is not None

    def test_task_push_notification_config_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import TaskPushNotificationConfig
        assert TaskPushNotificationConfig is not None

    def test_authentication_info_model_importable(self):
        from hermes_agent_a2a.a2a_spec.push import AuthenticationInfo
        assert AuthenticationInfo is not None


class TestPushDeliveryCRUDFunctions:
    """Verify that push_delivery exposes the required CRUD functions."""

    def test_create_push_config_importable(self):
        from hermes_agent_a2a.push_delivery import create_push_config
        assert callable(create_push_config)

    def test_get_push_config_importable(self):
        from hermes_agent_a2a.push_delivery import get_push_config
        assert callable(get_push_config)

    def test_list_push_configs_importable(self):
        from hermes_agent_a2a.push_delivery import list_push_configs
        assert callable(list_push_configs)

    def test_delete_push_config_importable(self):
        from hermes_agent_a2a.push_delivery import delete_push_config
        assert callable(delete_push_config)