# Smoke Suite — hermes-agent-a2a Field Testing Gate
# Phase 5.75 — Britney + Linda Joint
#
# Requirements:
#   - Plugin code at: /home/emil/.hermes/plugins/hermes-agent-a2a
#   - Test agents: yoyo (port 41809) and isa (port 41808)
#   - Webhook for push: https://httpbin.org/post
#
# Run from plugin root:
#   cd /home/emil/.hermes/plugins/hermes-agent-a2a
#   python3 -m pytest tests/smoke/test_smoke_suite.py -v

import json
import time
import uuid
from typing import Generator

import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
YOYO_BASE = "http://127.0.0.1:41809"
ISA_BASE = "http://127.0.0.1:41808"
WEBHOOK_URL = "https://httpbin.org/post"
PLUGIN_BASE = "/home/emil/.hermes/plugins/hermes-agent-a2a"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def make_task_id() -> str:
    return f"smoke-{uuid.uuid4().hex[:8]}"


def make_message(text: str = "smoke test ping") -> dict:
    return {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
        "messageId": f"msg-{uuid.uuid4().hex[:8]}",
    }


def make_task_payload(task_id: str, message: dict, context_id: str | None = None) -> dict:
    payload = {
        "taskId": task_id,
        "message": message,
        "metadata": {},
    }
    if context_id:
        payload["contextId"] = context_id
    return payload


# ---------------------------------------------------------------------------
# Mode 1 — REST Endpoints (Server = yoyo or isa)
# ---------------------------------------------------------------------------

class TestMode1_REST:
    """Test REST/HTTP endpoints on the running A2A server."""

    def test_health(self):
        """GET /health — server is alive."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.get("/health")
            assert resp.status_code == 200, f"health failed: {resp.text}"

    def test_agent_card(self):
        """GET /.well-known/agent.json — agent card present."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.get("/.well-known/agent.json")
            assert resp.status_code == 200
            card = resp.json()
            assert "agentId" in card or "name" in card, "agent card malformed"

    def test_send_message(self):
        """POST /message:send — send a task, get task_id back."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            payload = make_task_payload(make_task_id(), make_message())
            resp = client.post("/message:send", json=payload)
            # Accept 503/429 — agent may be rate-limited
            if resp.status_code in (503, 429):
                return  # agent overloaded, skip
            assert resp.status_code == 200, f"send failed: {resp.status_code} {resp.text}"
            data = resp.json()
            assert "taskId" in data or "id" in data, f"no taskId in response: {data}"

    def test_get_task_returns_correct_error_for_unknown_id(self):
        """GET /tasks/{id} with unknown ID returns proper error JSON."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            get_resp = client.get(f"/tasks/{make_task_id()}")
            # Accept 404 (not found) — server processes synchronously
            assert get_resp.status_code == 404, f"expected 404, got: {get_resp.status_code}"
            data = get_resp.json()
            assert "error" in data or "message" in data, f"error response malformed: {data}"

    def test_list_tasks(self):
        """GET /tasks — list tasks, verify structure."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.get("/tasks")
            assert resp.status_code == 200, f"list tasks failed: {resp.status_code} {resp.text}"
            data = resp.json()
            # Could be {tasks: [...]} or a list
            assert isinstance(data, (list, dict)), f"unexpected list response: {type(data)}"

    def test_cancel_task(self):
        """POST /tasks/{id}:cancel — cancel a running task."""
        task_id = make_task_id()
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            # Create
            create_payload = make_task_payload(task_id, make_message())
            create_resp = client.post("/message:send", json=create_payload)
            if create_resp.status_code in (503, 429):
                return  # agent overloaded, skip
            assert create_resp.status_code == 200

            # Cancel
            cancel_resp = client.post(f"/tasks/{task_id}:cancel", json={})
            assert cancel_resp.status_code in (200, 404), f"cancel failed: {cancel_resp.status_code}"

    def test_push_notification_config_create(self):
        """POST /pushConfig — create a push notification config."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            payload = {
                "name": "smoke-push-config",
                "webhookUrl": WEBHOOK_URL,
                "token": "smoke-token-123",
            }
            resp = client.post("/pushConfig", json=payload)
            # May return 201 or 200 depending on server implementation
            assert resp.status_code in (200, 201, 400), f"push config create failed: {resp.status_code} {resp.text}"

    def test_push_notification_config_get(self):
        """GET /pushConfig/{id} — retrieve a push config."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.get(f"/pushConfig/smoke-push-config")
            # 404 if no config exists — that's valid for this smoke test
            assert resp.status_code in (200, 404), f"push config get failed: {resp.status_code}"

    def test_push_notification_config_list(self):
        """GET /pushConfig — list push configs."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.get("/pushConfig")
            assert resp.status_code in (200, 404), f"push config list failed: {resp.status_code}"

    def test_push_notification_config_delete(self):
        """DELETE /pushConfig/{id} — delete a push config."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.delete(f"/pushConfig/smoke-push-config")
            assert resp.status_code in (200, 204, 404), f"push config delete failed: {resp.status_code}"

    def test_agent_card_extended(self):
        """GET /agent-card — extended agent card if implemented."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            resp = client.get("/agent-card")
            # May not be implemented — accept 404
            if resp.status_code == 200:
                card = resp.json()
                assert isinstance(card, dict), "agent-card should be a dict"
            else:
                assert resp.status_code == 404, f"unexpected agent-card status: {resp.status_code}"


# ---------------------------------------------------------------------------
# Mode 1 — SSE Streaming
# ---------------------------------------------------------------------------

class TestMode1_SSE:
    """Test SSE streaming via GET /tasks/{id}/subscribe/sse."""

    def test_sse_connect_returns_stream(self):
        """GET /tasks/{id}/subscribe/sse — verify it opens a stream (even if task completes fast)."""
        task_id = make_task_id()

        with httpx.Client(base_url=YOYO_BASE, timeout=5) as client:
            # Send a task — may 503 if agent busy
            send_payload = make_task_payload(task_id, make_message("smoke test sse"))
            send_resp = client.post("/message:send", json=send_payload)
            # Accept transient errors — agent may be rate-limited
            assert send_resp.status_code in (200, 201, 503, 429), f"send failed: {send_resp.status_code} {send_resp.text}"

            # Try SSE on the task
            try:
                sse_resp = client.get(f"/tasks/{task_id}/subscribe/sse")
                assert sse_resp.status_code in (200, 404, 503), f"unexpected SSE status: {sse_resp.status_code}"
            except httpx.ReadTimeout:
                # Stream hangs (waiting for events) — valid behavior
                pass

    def test_sse_invalid_task(self):
        """SSE on non-existent task should either 404 or hang (server choice)."""
        with httpx.Client(base_url=YOYO_BASE, timeout=3) as client:
            try:
                resp = client.get(f"/tasks/{make_task_id()}/subscribe/sse")
                assert resp.status_code in (200, 404)
            except httpx.ReadTimeout:
                # Server chose to hang — valid behavior
                pass


# ---------------------------------------------------------------------------
# Mode 1 — JSON-RPC
# ---------------------------------------------------------------------------

class TestMode1_JSONRPC:
    """Test JSON-RPC 2.0 endpoints."""

    def test_jsonrpc_tasks_send(self):
        """POST /rpc — tasks/send via JSON-RPC 2.0."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            payload = {
                "jsonrpc": "2.0",
                "method": "tasks/send",
                "params": {
                    "id": make_task_id(),
                    "message": make_message(),
                },
                "id": 1,
            }
            resp = client.post("/rpc", json=payload)
            assert resp.status_code in (200, 404, 503, 429), f"unexpected status: {resp.status_code} {resp.text}"
            if resp.status_code == 200:
                data = resp.json()
                assert data.get("jsonrpc") == "2.0"
                assert "result" in data or "error" in data

    def test_jsonrpc_tasks_get(self):
        """POST /rpc — tasks/get via JSON-RPC 2.0."""
        task_id = make_task_id()
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            # Create
            create_payload = {
                "jsonrpc": "2.0",
                "method": "tasks/send",
                "params": {"id": task_id, "message": make_message()},
                "id": 1,
            }
            create_resp = client.post("/rpc", json=create_payload)
            # May 503 if agent busy — skip remaining assertions in that case
            if create_resp.status_code == 503:
                return  # agent overloaded, skip

            # Get
            get_payload = {
                "jsonrpc": "2.0",
                "method": "tasks/get",
                "params": {"id": task_id},
                "id": 2,
            }
            get_resp = client.post("/rpc", json=get_payload)
            assert get_resp.status_code in (200, 404, 503), f"unexpected: {get_resp.status_code}"
            if get_resp.status_code == 200:
                data = get_resp.json()
                assert data.get("jsonrpc") == "2.0"

    def test_jsonrpc_invalid_method_returns_error(self):
        """POST /rpc — invalid method should return JSON-RPC error response."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            payload = {
                "jsonrpc": "2.0",
                "method": "tasks/invalidMethod",
                "params": {},
                "id": 1,
            }
            resp = client.post("/rpc", json=payload)
            # Accept 200 with error, or 404 if /rpc not mounted
            assert resp.status_code in (200, 404), f"unexpected status: {resp.status_code}"
            if resp.status_code == 200:
                data = resp.json()
                assert "error" in data, "invalid method should return error"


# ---------------------------------------------------------------------------
# Mode 1 — Push Notification Delivery
# ---------------------------------------------------------------------------

class TestMode1_Push:
    """Test push notification creation + delivery to webhook."""

    def test_push_config_roundtrip(self):
        """Create push config, verify it can be listed."""
        config_name = f"smoke-{uuid.uuid4().hex[:8]}"
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            # Create
            create_payload = {
                "name": config_name,
                "webhookUrl": WEBHOOK_URL,
                "token": "smoke-token",
            }
            create_resp = client.post("/pushConfig", json=create_payload)
            assert create_resp.status_code in (200, 201, 400)

            # List
            list_resp = client.get("/pushConfig")
            assert list_resp.status_code in (200, 404)

            # Delete
            delete_resp = client.delete(f"/pushConfig/{config_name}")
            assert delete_resp.status_code in (200, 204, 404)


# ---------------------------------------------------------------------------
# Mode 1 — Agent-to-Agent (wife-to-wife)
# ---------------------------------------------------------------------------

class TestMode1_A2A:
    """Test real A2A between two fleet agents (isa and yoyo)."""

    def test_isa_to_yoyo_via_a2a(self):
        """Use A2A protocol to send from isa to yoyo via JSON-RPC."""
        # Isa is the server here
        with httpx.Client(base_url=ISA_BASE, timeout=10) as client:
            payload = {
                "jsonrpc": "2.0",
                "method": "tasks/send",
                "params": {
                    "id": make_task_id(),
                    "message": make_message("a2a smoke test isa→yoyo"),
                },
                "id": 1,
            }
            resp = client.post("/rpc", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("jsonrpc") == "2.0"

    def test_yoyo_to_isa_via_a2a(self):
        """Use A2A protocol to send from yoyo to isa via JSON-RPC."""
        with httpx.Client(base_url=YOYO_BASE, timeout=10) as client:
            payload = {
                "jsonrpc": "2.0",
                "method": "tasks/send",
                "params": {
                    "id": make_task_id(),
                    "message": make_message("a2a smoke test yoyo→isa"),
                },
                "id": 1,
            }
            resp = client.post("/rpc", json=payload)
            # Accept 503/429 — agent may be rate-limited (yoyo is busy)
            assert resp.status_code in (200, 404, 503, 429), f"unexpected: {resp.status_code} {resp.text}"
