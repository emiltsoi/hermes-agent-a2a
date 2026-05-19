"""TDD tests for GET /tasks (ListTasks) pagination — F-B006.

These tests are written FIRST (failing), then _rest_list_tasks() is implemented.
"""
import json
import base64
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_rest_endpoints.py for self-contained tests)
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
    """Build a tasks/send JSON-RPC body."""
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
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
        "HERMES_HOME": "/tmp/test_list_tasks_hermes",
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
# F-B006: GET /tasks — paginated task list
# ---------------------------------------------------------------------------

class TestListTasksPagination:
    """Pagination tests for GET /tasks."""

    def test_list_tasks_returns_has_more_field(self, fresh_server):
        """Response must include 'hasMore' boolean field."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        assert "hasMore" in body, f"Response must include 'hasMore' field: {body}"
        assert isinstance(body["hasMore"], bool), f"'hasMore' must be a bool: {body}"

    def test_list_tasks_returns_next_page_token_field(self, fresh_server):
        """Response must include 'nextPageToken' string field (may be null)."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        assert "nextPageToken" in body, f"Response must include 'nextPageToken' field: {body}"

    def test_list_tasks_returns_items_array(self, fresh_server):
        """Response must include 'items' array of task objects."""
        server, port = fresh_server

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        assert "items" in body, f"Response must include 'items' array: {body}"
        assert isinstance(body["items"], list), f"'items' must be a list: {body}"

    def test_list_tasks_item_has_task_id_field(self, fresh_server):
        """Each task item must include 'task_id' field."""
        server, port = fresh_server

        task_id = "list-task-t1"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        items = body.get("items", [])
        task_ids = [t.get("task_id") for t in items]
        assert task_id in task_ids, f"Created task {task_id} must appear in items: {task_ids}"

    def test_list_tasks_item_has_context_id_field(self, fresh_server):
        """Each task item must include 'context_id' field."""
        server, port = fresh_server

        task_id = "list-task-t2"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        items = body.get("items", [])
        assert len(items) > 0, "Must have at least one task item"
        assert "context_id" in items[0], f"Task item must have 'context_id': {items[0]}"

    def test_list_tasks_item_has_status_field(self, fresh_server):
        """Each task item must include 'status' with state/message/timestamp."""
        server, port = fresh_server

        task_id = "list-task-t3"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        items = body.get("items", [])
        assert len(items) > 0, "Must have at least one task item"
        item = items[0]
        assert "status" in item, f"Task item must have 'status': {item}"
        assert "state" in item["status"], f"status must have 'state': {item}"

    def test_list_tasks_item_has_created_at_field(self, fresh_server):
        """Each task item must include 'created_at' field."""
        server, port = fresh_server

        task_id = "list-task-t4"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        items = body.get("items", [])
        assert len(items) > 0, "Must have at least one task item"
        assert "created_at" in items[0], f"Task item must have 'created_at': {items[0]}"

    def test_list_tasks_default_page_size_is_20(self, fresh_server):
        """Default page_size should be 20 — all tasks fit when count <= 20."""
        server, port = fresh_server

        # Create 5 tasks (well under default page_size=20)
        for i in range(5):
            _rpc_request(port, _make_task_send_body(f"list-task-dps-{i}", "hello"))

        body, status = _rest_get(port, "/tasks")
        assert status == 200
        items = body.get("items", [])
        # With 5 tasks and default page_size=20, all 5 should be returned
        assert len(items) == 5, f"Expected all 5 tasks (default page_size=20), got {len(items)}: {[t['task_id'] for t in items]}"
        assert body["hasMore"] is False, f"hasMore should be False when 5 tasks fit in default page_size=20: {body}"

    def test_list_tasks_page_size_param(self, fresh_server):
        """page_size query param controls number of returned items."""
        server, port = fresh_server

        # Create 10 tasks
        for i in range(10):
            _rpc_request(port, _make_task_send_body(f"list-task-ps-{i}", "hello"))

        body, status = _rest_get(port, "/tasks?page_size=3")
        assert status == 200
        items = body.get("items", [])
        assert len(items) == 3, f"With page_size=3, expected 3 items, got {len(items)}"

    def test_list_tasks_has_more_false_when_no_more(self, fresh_server):
        """hasMore must be False when all items fit in one page."""
        server, port = fresh_server

        # Create 3 tasks
        for i in range(3):
            _rpc_request(port, _make_task_send_body(f"list-task-hm-{i}", "hello"))

        body, status = _rest_get(port, "/tasks?page_size=10")
        assert status == 200
        assert body["hasMore"] is False, f"hasMore should be False when all items fit: {body}"

    def test_list_tasks_next_page_token_is_valid_base64(self, fresh_server):
        """nextPageToken must be valid base64 when hasMore is True."""
        server, port = fresh_server

        # Create 5 tasks with page_size=2
        for i in range(5):
            _rpc_request(port, _make_task_send_body(f"list-task-npt-{i}", "hello"))

        body, status = _rest_get(port, "/tasks?page_size=2")
        assert status == 200
        if body["hasMore"]:
            token = body.get("nextPageToken", "")
            assert token, "nextPageToken must be non-empty when hasMore is True"
            try:
                decoded = base64.b64decode(token)
                # Should decode to an integer offset string
                offset = int(decoded.decode())
                assert offset >= 0, "Decoded offset must be non-negative"
            except Exception as e:
                pytest.fail(f"nextPageToken must be valid base64-encoded offset: {e}")

    def test_list_tasks_continuation_token_returns_next_page(self, fresh_server):
        """Passing nextPageToken from page 1 should return page 2 (different items)."""
        server, port = fresh_server

        # Create 5 tasks with distinct IDs
        task_ids = [f"list-task-ct-{i}" for i in range(5)]
        for tid in task_ids:
            _rpc_request(port, _make_task_send_body(tid, "hello"))

        # Get first page
        body1, status1 = _rest_get(port, "/tasks?page_size=2")
        assert status1 == 200
        page1_ids = [t["task_id"] for t in body1.get("items", [])]

        # Get second page using continuation token
        token = body1.get("nextPageToken", "")
        assert token, "nextPageToken must be present for 5 tasks with page_size=2"
        body2, status2 = _rest_get(port, f"/tasks?page_size=2&continuation_token={token}")
        assert status2 == 200
        page2_ids = [t["task_id"] for t in body2.get("items", [])]

        # Pages should not overlap
        assert set(page1_ids).isdisjoint(set(page2_ids)), \
            f"Page 1 and page 2 should not overlap. Page1={page1_ids}, Page2={page2_ids}"

    def test_list_tasks_sorted_by_created_at_descending(self, fresh_server):
        """Tasks should be sorted by created_at in descending order (newest first)."""
        server, port = fresh_server

        # Create tasks sequentially
        task_ids = []
        for i in range(3):
            tid = f"list-task-sort-{i}"
            task_ids.append(tid)
            _rpc_request(port, _make_task_send_body(tid, "hello"))

        body, status = _rest_get(port, "/tasks?page_size=10")
        assert status == 200
        items = body.get("items", [])
        created_ats = [t["created_at"] for t in items if t["task_id"] in task_ids]
        assert created_ats == sorted(created_ats, reverse=True), \
            f"Tasks should be sorted newest-first: {created_ats}"


class TestListTasksJsonRpc:
    """JSON-RPC tasks/list handler tests."""

    def test_jsonrpc_tasks_list_method(self, fresh_server):
        """tasks/list JSON-RPC method should return paginated task list."""
        server, port = fresh_server

        task_id = "jsonrpc-list-task-1"
        _rpc_request(port, _make_task_send_body(task_id, "hello"))

        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/list",
            "params": {},
        }
        result, headers = _rpc_request(port, payload)
        assert "result" in result or "error" not in result, f"tasks/list should succeed: {result}"
        # Result should be the list body
        body = result.get("result", {})
        assert "items" in body, f"tasks/list result must have 'items': {body}"
        task_ids = [t.get("task_id") for t in body.get("items", [])]
        assert task_id in task_ids, f"Created task must appear in tasks/list: {task_ids}"

    def test_jsonrpc_tasks_list_with_page_size(self, fresh_server):
        """tasks/list should support pageSize param."""
        server, port = fresh_server

        for i in range(5):
            _rpc_request(port, _make_task_send_body(f"jsonrpc-list-ps-{i}", "hello"))

        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/list",
            "params": {"pageSize": 2},
        }
        result, _ = _rpc_request(port, payload)
        body = result.get("result", {})
        items = body.get("items", [])
        assert len(items) == 2, f"With pageSize=2, expected 2 items, got {len(items)}"

    def test_jsonrpc_tasks_list_with_continuation_token(self, fresh_server):
        """tasks/list should support continuationToken param."""
        server, port = fresh_server

        for i in range(4):
            _rpc_request(port, _make_task_send_body(f"jsonrpc-list-ct-{i}", "hello"))

        # Get first page
        payload1 = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/list",
            "params": {"pageSize": 2},
        }
        result1, _ = _rpc_request(port, payload1)
        body1 = result1.get("result", {})
        token = body1.get("nextPageToken", "")

        # Get second page
        payload2 = {
            "jsonrpc": "2.0",
            "id": "2",
            "method": "tasks/list",
            "params": {"pageSize": 2, "continuationToken": token},
        }
        result2, _ = _rpc_request(port, payload2)
        body2 = result2.get("result", {})
        page2_ids = [t["task_id"] for t in body2.get("items", [])]

        # Should have different items from page 1
        page1_ids = [t["task_id"] for t in body1.get("items", [])]
        assert set(page1_ids).isdisjoint(set(page2_ids)), \
            f"Pages should not overlap. P1={page1_ids}, P2={page2_ids}"
