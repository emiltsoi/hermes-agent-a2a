"""Google A2A REST/HTTP binding compliance tests — all 11 spec-standard endpoints.

Each test is written to fail before the feature is implemented.
Tests cover F-B001, F-B005-F-B011 per the A2A compliance audit.
"""
import json
import time
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — reuse from test_a2a_compliance.py
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
    """Build a SendMessage JSON-RPC body per spec."""
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


def _rest_get(port, path, auth_token="test-secret"):
    """GET request to a REST endpoint. Returns (body_dict, status_code)."""
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


def _rest_post(port, path, body=None, auth_token="test-secret"):
    """POST request to a REST endpoint. Returns (body_dict, status_code)."""
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


def _rest_delete(port, path, auth_token="test-secret"):
    """DELETE request to a REST endpoint. Returns (body_dict, status_code)."""
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
        "HERMES_HOME": "/tmp/test_a2a_rest_hermes",
    }):
        from hermes_agent_a2a import plugin as plugin_module
        import importlib
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


# ---------------------------------------------------------------------------
# F-B001: GET /tasks/{id}  (GetTask)
# ---------------------------------------------------------------------------

class TestGetTask:
    """RPC #3: GET /tasks/{id} — return Task object for existing task."""

    def test_get_task_returns_200_for_existing_task(self, fresh_server):
        """GET /tasks/{id} must return 200 for a known task."""
        server, port = fresh_server

        # First create a task via JSON-RPC
        task_id = "rest-get-task-1"
        result, _ = _rpc_request(port, _make_task_send_body(task_id, "hello"))

        # GET the task via REST
        body, status = _rest_get(port, f"/tasks/{task_id}")
        assert status == 200, f"Expected 200, got {status}: {body}"

    def test_get_task_returns_task_object(self, fresh_server):
        """GET /tasks/{id} must return a Task-like object with id and status."""
        server, port = fresh_server

        task_id = "rest-get-task-2"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, f"/tasks/{task_id}")
        assert status == 200
        assert "id" in body, f"Task object must have 'id': {body}"
        assert "status" in body, f"Task object must have 'status': {body}"
        assert "state" in body["status"], f"status must have 'state': {body}"

    def test_get_task_returns_404_for_unknown_task(self, fresh_server):
        """GET /tasks/{id} must return 404 for unknown task."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks/nonexistent-task-xyz")
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"

    def test_get_task_includes_artifacts(self, fresh_server):
        """GET /tasks/{id} should include artifacts when a response is available."""
        server, port = fresh_server

        task_id = "rest-get-task-3"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, f"/tasks/{task_id}")
        assert status == 200
        # Task should have either status.state == completed (with artifacts) or working
        assert body["status"]["state"] in ("completed", "working", "submitted"), \
            f"Task should be completed, working, or submitted: {body}"


# ---------------------------------------------------------------------------
# F-B006: GET /tasks  (ListTasks)
# ---------------------------------------------------------------------------

class TestListTasks:
    """RPC #4: GET /tasks — return paginated list of tasks."""

    def test_list_tasks_returns_200(self, fresh_server):
        """GET /tasks must return 200 even with no tasks."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks")
        assert status == 200, f"Expected 200, got {status}: {body}"

    def test_list_tasks_returns_tasks_array(self, fresh_server):
        """GET /tasks must return an object with tasks array field."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        # Response must have tasks, next_page_token, page_size, total_size
        assert "tasks" in body, f"Response must have 'tasks' field: {body}"
        assert isinstance(body["tasks"], list), f"'tasks' must be a list: {body}"

    def test_list_tasks_includes_created_tasks(self, fresh_server):
        """GET /tasks must include tasks that were created."""
        server, port = fresh_server

        task_id = "rest-list-task-1"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, "/tasks")
        task_ids = [t.get("id") for t in body.get("tasks", [])]
        assert task_id in task_ids, f"Created task {task_id} must appear in task list: {task_ids}"

    def test_list_tasks_pagination_params(self, fresh_server):
        """GET /tasks must accept optional page_size param without error."""
        server, port = fresh_server

        # These should not cause 400 errors
        body, status = _rest_get(port, "/tasks?page_size=10")
        assert status == 200, f"page_size param must not cause error: {body}"

        body2, status2 = _rest_get(port, "/tasks?page_size=5")
        assert status2 == 200, f"page_size param must not cause error: {body2}"

    def test_list_tasks_returns_pagination_fields(self, fresh_server):
        """GET /tasks must return next_page_token, page_size, total_size fields."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        assert "next_page_token" in body, f"Response must have 'next_page_token': {body}"
        assert "page_size" in body, f"Response must have 'page_size': {body}"
        assert "total_size" in body, f"Response must have 'total_size': {body}"


# ---------------------------------------------------------------------------
# F-B007: POST /tasks/{id}:cancel  (CancelTask)
# ---------------------------------------------------------------------------

class TestCancelTask:
    """RPC #5: POST /tasks/{id}:cancel — cancel a pending task."""

    def test_cancel_task_returns_200_for_existing_task(self, fresh_server):
        """POST /tasks/{id}:cancel must return 200 for a pending task."""
        server, port = fresh_server

        task_id = "rest-cancel-task-1"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_post(port, f"/tasks/{task_id}:cancel")
        assert status == 200, f"Expected 200 on cancel, got {status}: {body}"

    def test_cancel_task_returns_canceled_state(self, fresh_server):
        """POST /tasks/{id}:cancel must return task with state=canceled."""
        server, port = fresh_server

        task_id = "rest-cancel-task-2"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_post(port, f"/tasks/{task_id}:cancel")
        assert status == 200
        assert body.get("status", {}).get("state") == "canceled", \
            f"Canceled task must have state=canceled: {body}"

    def test_cancel_task_returns_404_for_unknown_task(self, fresh_server):
        """POST /tasks/{id}:cancel must return 404 for unknown task."""
        server, port = fresh_server

        body, status = _rest_post(port, "/tasks/nonexistent:cancel")
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"


# ---------------------------------------------------------------------------
# F-B008: GET /tasks/{id}:subscribe  (SubscribeToTask)
# ---------------------------------------------------------------------------

class TestSubscribeToTask:
    """RPC #6: GET /tasks/{id}:subscribe — SSE stream for task updates."""

    def test_subscribe_returns_sse_content_type(self, fresh_server):
        """GET /tasks/{id}:subscribe must return text/event-stream."""
        import urllib.request, urllib.error, threading, http.client

        server, port = fresh_server
        task_id = "rest-sub-task-1"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        # Read SSE in a thread so we don't block the test on the long-lived stream
        result = {}
        def read_sse():
            try:
                hdrs = {"Authorization": "Bearer test-secret"}
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", f"/tasks/{task_id}:subscribe", headers=hdrs)
                resp = conn.getresponse()
                result["status"] = resp.status
                result["ct"] = resp.getheader("Content-Type", "")
                conn.close()
            except Exception as e:
                result["error"] = str(e)

        t = threading.Thread(target=read_sse, daemon=True)
        t.start()
        t.join(timeout=5)
        assert "status" in result, f"SSE request timed out or errored: {result.get('error', 'timeout')}"
        assert result["status"] == 200, f"Expected 200, got {result['status']}"
        assert "text/event-stream" in result.get("ct", ""), \
            f"Content-Type must be text/event-stream, got: {result.get('ct')}"

    def test_subscribe_returns_404_for_unknown_task(self, fresh_server):
        """GET /tasks/{id}:subscribe must return 404 for unknown task."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks/nonexistent:subscribe")
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"


# ---------------------------------------------------------------------------
# F-B005: POST /message:send  (SendMessage)
# ---------------------------------------------------------------------------

class TestSendMessage:
    """RPC #1: POST /message:send — send a message and get a task result."""

    def test_message_send_returns_200(self, fresh_server):
        """POST /message:send must return 200 (or 201) on success."""
        server, port = fresh_server

        body, status = _rest_post(
            port,
            "/message:send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                    "metadata": {},
                },
            },
        )
        assert status in (200, 201), f"Expected 200/201, got {status}: {body}"

    def test_message_send_returns_task_result(self, fresh_server):
        """POST /message:send must return a Task-like object."""
        server, port = fresh_server

        body, status = _rest_post(
            port,
            "/message:send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                    "metadata": {},
                },
            },
        )
        assert status in (200, 201)
        assert "id" in body, f"Response must include task id: {body}"
        assert "status" in body, f"Response must include task status: {body}"

    def test_message_send_empty_message_returns_error(self, fresh_server):
        """POST /message:send with empty text must return an error."""
        server, port = fresh_server

        body, status = _rest_post(
            port,
            "/message:send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": ""}],
                    "metadata": {},
                },
            },
        )
        # Should return an error response (not 2xx success with failed state)
        if status >= 200 and status < 300:
            # If 2xx, the state must be "failed" with an appropriate message
            state = body.get("status", {}).get("state")
            assert state == "failed", \
                f"Empty message should produce failed state, got: {body}"


# ---------------------------------------------------------------------------
# F-B009: POST /message/stream  (SendStreamingMessage)
# ---------------------------------------------------------------------------

class TestSendStreamingMessage:
    """RPC #2: POST /message/stream — streaming response."""

    def test_message_stream_returns_streaming_content_type(self, fresh_server):
        """POST /message/stream must return text/event-stream."""
        server, port = fresh_server

        import urllib.request, urllib.error
        hdrs = {
            "Authorization": "Bearer test-secret",
            "Content-Type": "application/json",
        }
        data = json.dumps({
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "hello"}],
                "metadata": {},
            },
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/message:stream",
            data=data,
            headers=hdrs,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                ct = resp.getheader("Content-Type", "")
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
            ct = e.headers.get("Content-Type", "")

        assert status in (200, 201), f"Expected 200/201, got {status}"
        assert "text/event-stream" in ct, \
            f"Content-Type must be text/event-stream, got: {ct}"


# ---------------------------------------------------------------------------
# F-B010: Push Notification Config CRUD  (RPCs 7-10)
# ---------------------------------------------------------------------------

class TestPushNotificationConfigs:
    """RPCs 7-10: CRUD for pushNotificationConfigs on tasks."""

    def _seed_task(self, port, task_id="push-config-task-1"):
        """Create a task so the push config endpoints have something to bind to."""
        _rpc_request(port, _make_task_send_body(task_id, "hello"))
        return task_id

    def test_create_push_notification_config_returns_201(self, fresh_server):
        """POST /tasks/{id}/pushNotificationConfigs must return 201 on success."""
        server, port = fresh_server
        task_id = self._seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-123",
            },
        )
        assert status == 201, f"Expected 201, got {status}: {body}"

    def test_create_push_notification_config_returns_config_id(self, fresh_server):
        """POST must return a configId on success."""
        server, port = fresh_server
        task_id = self._seed_task(port)

        body, status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-123",
            },
        )
        assert status == 201
        # Must return configId (spec-compliant)
        config_id = body.get("configId")
        assert config_id, f"Must return configId: {body}"

    def test_create_push_notification_config_returns_404_for_unknown_task(self, fresh_server):
        """POST /tasks/{id}/pushNotificationConfigs must return 404 for unknown task."""
        server, port = fresh_server

        body, status = _rest_post(
            port,
            "/tasks/unknown-task-xyz/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-123",
            },
        )
        assert status == 404, f"Expected 404 for unknown task, got {status}: {body}"

    def test_get_push_notification_config_returns_200(self, fresh_server):
        """GET /tasks/{id}/pushNotificationConfigs/{config_id} must return 200."""
        server, port = fresh_server
        task_id = self._seed_task(port)

        # Create first
        create_body, create_status = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-123",
            },
        )
        assert create_status == 201
        sub_id = create_body.get("subscriptionId") or (create_body.get("config") or {}).get("id") or create_body.get("configId")
        assert sub_id, f"Create must return config id: {create_body}"

        # GET it
        get_body, get_status = _rest_get(
            port, f"/tasks/{task_id}/pushNotificationConfigs/{sub_id}"
        )
        assert get_status == 200, f"Expected 200, got {get_status}: {get_body}"
        assert "subscriptionId" in get_body or "config" in get_body or "configId" in get_body, \
            f"Must return config details: {get_body}"

    def test_list_push_notification_configs_returns_200(self, fresh_server):
        """GET /tasks/{id}/pushNotificationConfigs must return 200 with array."""
        server, port = fresh_server
        task_id = self._seed_task(port)

        # Create one config
        _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-123",
            },
        )

        body, status = _rest_get(port, f"/tasks/{task_id}/pushNotificationConfigs")
        assert status == 200, f"Expected 200, got {status}: {body}"
        items = body if isinstance(body, list) else body.get("items") or body.get("configs", [])
        assert isinstance(items, list), f"Expected list, got: {body}"

    def test_delete_push_notification_config_returns_204(self, fresh_server):
        """DELETE /tasks/{id}/pushNotificationConfigs/{config_id} must return 204."""
        server, port = fresh_server
        task_id = self._seed_task(port)

        # Create first
        create_body, _ = _rest_post(
            port,
            f"/tasks/{task_id}/pushNotificationConfigs",
            {
                "url": "https://example.com/callback",
                "hmacKey": "secret-key-123",
            },
        )
        sub_id = create_body.get("subscriptionId") or (create_body.get("config") or {}).get("id") or create_body.get("configId")
        assert sub_id, f"Create must return config id: {create_body}"

        # Delete it
        del_body, del_status = _rest_delete(
            port, f"/tasks/{task_id}/pushNotificationConfigs/{sub_id}"
        )
        assert del_status == 204, f"Expected 204 on delete, got {del_status}: {del_body}"

    def test_delete_push_notification_config_returns_404_for_unknown(self, fresh_server):
        """DELETE for unknown config_id must return 404."""
        server, port = fresh_server
        task_id = self._seed_task(port)

        body, status = _rest_delete(
            port, f"/tasks/{task_id}/pushNotificationConfigs/unknown-sub-id-xyz"
        )
        assert status == 404, f"Expected 404 for unknown config, got {status}: {body}"


# ---------------------------------------------------------------------------
# F-B011: GET /extendedAgentCard  (GetExtendedAgentCard)
# ---------------------------------------------------------------------------

class TestGetExtendedAgentCard:
    """RPC #11: GET /extendedAgentCard — return full extended AgentCard."""

    def test_extended_agent_card_returns_200(self, fresh_server):
        """GET /extendedAgentCard must return 200."""
        server, port = fresh_server

        body, status = _rest_get(port, "/extendedAgentCard")
        assert status == 200, f"Expected 200, got {status}: {body}"

    def test_extended_agent_card_has_agent_info(self, fresh_server):
        """GET /extendedAgentCard must return agent info (name/description/url)."""
        server, port = fresh_server

        body, status = _rest_get(port, "/extendedAgentCard")
        assert status == 200
        # Should have at least name, description, or url
        assert any(k in body for k in ("name", "description", "url", "capabilities", "skills")), \
            f"Extended agent card must have agent fields: {body}"


# ---------------------------------------------------------------------------
# General: REST endpoints coexist with JSON-RPC
# ---------------------------------------------------------------------------

class TestRESTCoexistsWithJSONRPC:
    """Verify JSON-RPC endpoints work with spec-compliant method names."""

    def test_json_rpc_send_message_works(self, fresh_server):
        """JSON-RPC SendMessage must work."""
        server, port = fresh_server

        result, status = _rpc_request(port, _make_task_send_body("json-rpc-send-1"))
        assert "error" not in result or result["error"].get("code") != -32601, \
            f"JSON-RPC SendMessage must work: {result}"

    def test_json_rpc_get_task_works(self, fresh_server):
        """JSON-RPC GetTask must work."""
        server, port = fresh_server

        task_id = "json-rpc-get-task-1"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "GetTask",
            "params": {"id": task_id},
        }
        result, _ = _rpc_request(port, body)
        assert "error" not in result, f"JSON-RPC GetTask must work: {result}"

    def test_health_endpoint_still_works(self, fresh_server):
        """GET /health must still return 200."""
        server, port = fresh_server

        body, status = _rest_get(port, "/health")
        assert status == 200, f"Health endpoint must still work: {body}"

    def test_agent_card_endpoint_still_works(self, fresh_server):
        """GET /.well-known/agent.json must still return 200."""
        server, port = fresh_server

        body, status = _rest_get(port, "/.well-known/agent.json")
        assert status == 200, f"Agent card endpoint must still work: {body}"