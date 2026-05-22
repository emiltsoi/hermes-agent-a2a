"""T1-4: Emit TaskArtifactUpdateEvent over SSE when artifacts are generated.

Tests written to fail BEFORE implementation (TDD).
"""
import json
import threading
import time
from unittest.mock import patch, MagicMock
import urllib.request
import urllib.error

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
        "A2A_REQUIRE_AUTH": "false",
        "HERMES_HOME": "/tmp/test_artifact_event_hermes",
    }):
        from hermes_agent_a2a import plugin as plugin_module
        import importlib
        importlib.reload(plugin_module)
        plugin_module._start_a2a_server()

        state = rs_module.get_runtime_state()
        server = state.get_server()

        yield server, port

        try:
            server.shutdown()
        except Exception:
            pass
        rs_module.get_runtime_state().clear()


def _rpc_request(port, payload, auth_token="test-secret"):
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), dict(e.headers)


# ---------------------------------------------------------------------------
# T1-4: TaskArtifactUpdateEvent dataclass (a2a_spec/tasks.py)
# ---------------------------------------------------------------------------

class TestTaskArtifactUpdateEventDataclass:
    """T1-4.2: TaskArtifactUpdateEvent dataclass exists in a2a_spec/tasks.py."""

    def test_task_artifact_update_event_dataclass_exists(self):
        """TaskArtifactUpdateEvent dataclass must be importable from a2a_spec.tasks."""
        from hermes_agent_a2a.a2a_spec.tasks import TaskArtifactUpdateEvent

    def test_task_artifact_update_event_has_required_fields(self):
        """TaskArtifactUpdateEvent must have context_id, task_id, artifact fields."""
        from hermes_agent_a2a.a2a_spec.tasks import TaskArtifactUpdateEvent

        evt = TaskArtifactUpdateEvent(
            context_id="ctx-1",
            task_id="task-1",
            artifact={"parts": [{"text": "hello"}]},
        )
        assert evt.context_id == "ctx-1"
        assert evt.task_id == "task-1"
        assert evt.artifact == {"parts": [{"text": "hello"}]}

    def test_task_artifact_update_event_optional_metadata(self):
        """TaskArtifactUpdateEvent must accept optional metadata field."""
        from hermes_agent_a2a.a2a_spec.tasks import TaskArtifactUpdateEvent

        evt = TaskArtifactUpdateEvent(
            context_id="ctx-1",
            task_id="task-1",
            artifact={"parts": [{"text": "hello"}]},
            metadata={"index": 0},
        )
        assert evt.metadata == {"index": 0}


# ---------------------------------------------------------------------------
# T1-4.3: emit_artifact_event() in sse_handler.py
# ---------------------------------------------------------------------------

class TestEmitArtifactEvent:
    """T1-4.3: emit_artifact_event() function in sse_handler.py."""

    def test_emit_artifact_event_function_exists(self):
        """emit_artifact_event must be importable from sse_handler."""
        from hermes_agent_a2a.sse_handler import emit_artifact_event

    def test_emit_artifact_event_returns_sse_event(self):
        """emit_artifact_event returns an SSEEvent with kind='artifact'."""
        from hermes_agent_a2a.sse_handler import emit_artifact_event

        result = emit_artifact_event(
            task_id="task-1",
            context_id="ctx-1",
            artifact={"parts": [{"text": "hello"}]},
            metadata=None,
        )
        assert result.kind == "artifact"
        assert result.task_id == "task-1"
        assert result.context_id == "ctx-1"
        assert result.artifact == {"parts": [{"text": "hello"}]}

    def test_emit_artifact_event_with_metadata(self):
        """emit_artifact_event passes metadata through."""
        from hermes_agent_a2a.sse_handler import emit_artifact_event

        result = emit_artifact_event(
            task_id="task-1",
            context_id="ctx-1",
            artifact={"parts": [{"text": "hello"}]},
            metadata={"index": 0},
        )
        assert result.metadata == {"index": 0}

    def test_emit_artifact_event_sse_line_format(self):
        """The SSE line for artifact must match the TaskArtifactUpdateEvent format.

        Per a2a.proto:775-787, TaskArtifactUpdateEvent has:
        - contextId (REQUIRED)
        - taskId (REQUIRED)
        - artifact (REQUIRED)
        - metadata (optional)

        The field 'kind' is NOT part of the spec — the oneof discriminator is the
        field name itself ('artifact_update'), not a 'kind' string.
        """
        from hermes_agent_a2a.sse_handler import emit_artifact_event

        result = emit_artifact_event(
            task_id="task-1",
            context_id="ctx-1",
            artifact={"parts": [{"text": "hello"}]},
            metadata={"index": 0},
        )
        sse_line = result.to_sse_line()
        # Must NOT contain spurious 'kind' field
        assert '"kind"' not in sse_line, "kind field is not in A2A spec TaskArtifactUpdateEvent"
        assert '"contextId":"ctx-1"' in sse_line or '"contextId": "ctx-1"' in sse_line
        assert '"taskId":"task-1"' in sse_line or '"taskId": "task-1"' in sse_line
        assert '"artifact"' in sse_line


# ---------------------------------------------------------------------------
# T1-4.4: Integration — SSE stream includes artifact events
# ---------------------------------------------------------------------------

class TestArtifactEventIntegration:
    """T1-4.4: Artifact events appear in SSE stream when task artifacts are generated."""

    def test_artifact_event_appears_in_sse_pending_buffer(self):
        """Pushing an artifact event to SSEStreamer should produce a valid SSE line."""
        from hermes_agent_a2a.sse_handler import get_sse_streamer, emit_artifact_event, SSEStreamer

        # Reset singleton for clean state
        import hermes_agent_a2a.sse_handler as sh
        sh._streamer = None

        streamer = get_sse_streamer()
        stream_id = streamer.open_stream("task-artifact-1")

        event = emit_artifact_event(
            task_id="task-artifact-1",
            context_id="ctx-artifact-1",
            artifact={"parts": [{"text": "artifact content"}], "index": 0},
            metadata={"index": 0},
        )
        streamer.push_event(stream_id, event)

        pending = streamer.get_pending(stream_id)
        assert len(pending) == 1
        sse_line = pending[0]
        # Verify the SSE line contains the required fields (no 'kind' per spec)
        assert '"kind"' not in sse_line, "kind field is not in A2A spec"
        assert '"taskId":"task-artifact-1"' in sse_line or '"taskId": "task-artifact-1"' in sse_line
        assert '"contextId":"ctx-artifact-1"' in sse_line or '"contextId": "ctx-artifact-1"' in sse_line
        assert '"artifact"' in sse_line

        streamer.close_stream(stream_id)

    def test_sse_streamer_push_and_retrieve_artifact_event(self, fresh_server):
        """Simulate the full flow: create a task, open SSE stream, push artifact event, retrieve it."""
        server, port = fresh_server
        task_id = f"artifact-integration-{int(time.time() * 1000)}"

        # First create the task via tasks/send so it exists
        resp, _ = _rpc_request(port, {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "params": {
                "id": task_id,
                "message": {
                    "message_id": "msg-1",
                    "role": "user",
                    "parts": [{"text": "hello"}],
                },
            },
        })
        # Note: task will not complete (gateway unavailable in test), but it exists

        # Now subscribe to the task via SSE
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", port))

        req_text = (
            f"POST /a2a HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer test-secret\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(json.dumps({'jsonrpc': '2.0', 'method': 'SubscribeToTask', 'params': {'taskId': task_id}, 'id': 1}))}\r\n"
            f"\r\n"
            + json.dumps({"jsonrpc": "2.0", "method": "SubscribeToTask", "params": {"taskId": task_id}, "id": 1})
        )
        sock.sendall(req_text.encode())

        # Read the SSE response headers + initial event
        response_data = b""
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b"\n\n" in response_data:
                    break
            except socket.timeout:
                break

        decoded_headers = response_data.decode(errors="replace")
        stream_id = None
        for line in decoded_headers.split("\n"):
            if line.startswith("X-Stream-Id:"):
                stream_id = line.split(":", 1)[1].strip()
                break

        sock.close()
        assert stream_id is not None, f"Expected X-Stream-Id header, got: {decoded_headers[:500]}"

        # Now push an artifact event to the stream via SSEStreamer
        from hermes_agent_a2a.sse_handler import get_sse_streamer, emit_artifact_event
        streamer = get_sse_streamer()
        evt = emit_artifact_event(
            task_id=task_id,
            context_id=task_id,
            artifact={"parts": [{"text": "integration test artifact"}], "index": 0},
            metadata={"index": 0},
        )
        streamer.push_event(stream_id, evt)

        # Verify the event was queued
        pending = streamer.get_pending(stream_id)
        assert len(pending) == 1
        sse_line = pending[0]
        assert '"kind"' not in sse_line, "kind field is not in A2A spec"
        assert '"taskId":"' + task_id + '"' in sse_line or '"taskId": "' + task_id + '"' in sse_line

        streamer.close_stream(stream_id)
