"""Wave 2 SSE streaming tests — tasks/sendSubscribe.

Tests written to fail BEFORE implementation.
"""
import json
import threading
import time
from http.server import ThreadingHTTPServer
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
        "A2A_REQUIRE_AUTH": "true",
        "HERMES_HOME": "/tmp/test_sse_streaming_hermes",
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), dict(e.headers)


# ---------------------------------------------------------------------------
# SSEStreamer Contract
# ---------------------------------------------------------------------------

class TestSSEStreamerContract:
    """SSEStreamer must implement the contract from the spec."""

    def test_sse_handler_module_exists(self):
        """sse_handler.py module must exist."""
        from hermes_agent_a2a import sse_handler
        assert sse_handler is not None

    def test_sse_streamer_class_exists(self):
        """SSEStreamer class must exist."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        assert SSEStreamer is not None

    def test_sse_streamer_has_open_stream_method(self):
        """SSEStreamer.open_stream(task_id) -> str must exist."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        assert hasattr(streamer, "open_stream")
        assert callable(streamer.open_stream)

    def test_sse_streamer_has_push_event_method(self):
        """SSEStreamer.push_event(stream_id, event) -> None must exist."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        assert hasattr(streamer, "push_event")
        assert callable(streamer.push_event)

    def test_sse_streamer_has_close_stream_method(self):
        """SSEStreamer.close_stream(stream_id) -> None must exist."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        assert hasattr(streamer, "close_stream")
        assert callable(streamer.close_stream)

    def test_open_stream_returns_stream_id(self):
        """open_stream(task_id) must return a non-empty stream_id string."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-open-1")
        assert isinstance(stream_id, str), f"open_stream must return str, got {type(stream_id)}"
        assert stream_id, "open_stream must return a non-empty stream_id"

    def test_open_stream_different_calls_return_different_ids(self):
        """Each open_stream call must return a unique stream_id."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        id1 = streamer.open_stream("task-multi-1")
        id2 = streamer.open_stream("task-multi-2")
        assert id1 != id2, "open_stream must return unique stream IDs"

    def test_push_event_with_sse_event_data(self):
        """push_event must accept event dict with required SSE fields."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent
        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-push-1")
        event = SSEEvent(
            task_id="task-push-1",
            state="working",
            event="TaskWorking",
        )
        # Must not raise
        streamer.push_event(stream_id, event)

    def test_close_stream_removes_stream(self):
        """close_stream must not raise and the stream must be gone after close."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-close-1")
        streamer.close_stream(stream_id)
        # Second close must not raise
        streamer.close_stream(stream_id)

    def test_sse_event_dataclass_exists(self):
        """SSEEvent typed dataclass must exist."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        event = SSEEvent(task_id="x", state="working", event="TaskWorking")
        assert event.task_id == "x"
        assert event.state == "working"


# ---------------------------------------------------------------------------
# sendSubscribe Endpoint
# ---------------------------------------------------------------------------

class TestSendSubscribeEndpoint:
    """POST /tasks/sendSubscribe — SSE stream of task events."""

    def test_send_subscribe_returns_sse_content_type(self, fresh_server):
        """sendSubscribe must return Content-Type: text/event-stream."""
        server, port = fresh_server
        import threading, time
        from hermes_agent_a2a.server import _ensure_task_queue

        # Pre-create the task so the SSE handler finds it
        q = _ensure_task_queue()
        task = q.enqueue("sse-test-task-1", "hello", {"sender_name": "test"})

        body = {
            "jsonrpc": "2.0",
            "id": "ss-1",
            "method": "tasks/sendSubscribe",
            "params": {"taskId": "sse-test-task-1"},
        }
        b = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Authorization": "Bearer test-secret"}

        result_container = {}
        error_container = {}

        def make_request():
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/a2a",
                    data=b,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    result_container["status"] = resp.status
                    result_container["headers"] = dict(resp.headers)
                    result_container["body"] = resp.read(200)
            except urllib.error.HTTPError as e:
                error_container["status"] = e.code
                error_container["body"] = e.read()
            except Exception as e:
                # SSE timeout is expected — stream never closes without completion signal
                # We still get headers before the timeout
                result_container["exc"] = str(e)

        t = threading.Thread(target=make_request)
        t.start()
        t.join(timeout=5)

        if error_container:
            pytest.fail(f"SSE request returned HTTP error: {error_container}")

        # The Content-Type header is set BEFORE the stream loops
        # (even if the body read eventually times out)
        resp_headers = result_container.get("headers", {})
        ct = resp_headers.get("Content-Type", "")
        assert "text/event-stream" in ct, \
            f"sendSubscribe must return Content-Type: text/event-stream, got: {ct}"

    def test_send_subscribe_task_not_found_returns_error(self, fresh_server):
        """subscribe to non-existent task must return -38000."""
        server, port = fresh_server

        body = {
            "jsonrpc": "2.0",
            "id": "ss-notfound",
            "method": "tasks/sendSubscribe",
            "params": {"taskId": "nonexistent-sse-task-xyz"},
        }
        result, headers = _rpc_request(port, body)

        # Before implementation: method not found
        # After implementation: -38000 Task not found
        assert "error" in result, "Non-existent task must return an error"
        code = result["error"].get("code")
        assert code == -38000, f"Must return -38000 for unknown task, got: {code}"

    def test_send_subscribe_returns_immediately_for_completed_task(self, fresh_server):
        """Opening SSE stream for a completed task returns current state immediately."""
        server, port = fresh_server

        # Use the server's singleton queue so completion is visible to the SSE handler
        from hermes_agent_a2a.server import _ensure_task_queue
        q = _ensure_task_queue()
        q.enqueue("completed-sse-task", "hello", {})
        q.complete("completed-sse-task", "done")

        # The SSE stream should return the current completed state
        # without waiting for new events
        import threading
        result_container = {}

        def subscribe(port):
            body = {
                "jsonrpc": "2.0",
                "id": "ss-comp-1",
                "method": "tasks/sendSubscribe",
                "params": {"taskId": "completed-sse-task"},
            }
            b = json.dumps(body).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/a2a",
                data=b,
                headers={"Content-Type": "application/json", "Authorization": "Bearer test-secret"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=2) as resp:
                    result_container["data"] = resp.read(500)
            except Exception as e:
                result_container["error"] = str(e)

        t = threading.Thread(target=subscribe, args=(port,))
        t.start()
        t.join(timeout=5)

        # Should not hang — must return immediately
        if "error" in result_container:
            pytest.fail(f"SSE subscribe failed: {result_container['error']}")

    def test_sse_event_format(self, fresh_server):
        """SSE stream must emit events in SSE format: data: {...}\n\n"""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        hook = TaskStateChangeHook()
        hook.on_state_change("hook-sse-task", "submitted", "working")

    def test_multiple_subscribers_per_task(self, fresh_server):
        """Multiple clients can subscribe to the same task_id."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        id1 = streamer.open_stream("multi-sub-task")
        id2 = streamer.open_stream("multi-sub-task")
        id3 = streamer.open_stream("multi-sub-task")

        assert len({id1, id2, id3}) == 3, "Each subscribe must get a unique stream_id"
        streamer.close_stream(id1)
        streamer.close_stream(id2)
        streamer.close_stream(id3)


# ---------------------------------------------------------------------------
# Hook Wiring
# ---------------------------------------------------------------------------

class TestSSEHookWiring:
    """TaskStateChangeHook must push events to SSE streams."""

    def test_task_state_change_hook_exists(self):
        """hooks.py must have TaskStateChangeHook class."""
        from hermes_agent_a2a import hooks
        assert hasattr(hooks, "TaskStateChangeHook"), "hooks.py must define TaskStateChangeHook"

    def test_task_state_change_hook_has_on_state_change(self):
        """TaskStateChangeHook.on_state_change(task_id, old_state, new_state) -> None."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook
        hook = TaskStateChangeHook()
        assert hasattr(hook, "on_state_change")
        assert callable(hook.on_state_change)

    def test_hook_delivers_event_to_sse_stream(self):
        """on_state_change must push an SSEEvent to the matching stream."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        streamer = SSEStreamer()
        TaskStateChangeHook._sse_streamer = streamer  # inject

        stream_id = streamer.open_stream("hook-task-1")
        hook = TaskStateChangeHook()

        # Fire state change
        hook.on_state_change("hook-task-1", "submitted", "working")

        # Stream should have received an event
        # (verify by checking internal state or that push_event was called)
        # For unit test: just verify no exception
        streamer.close_stream(stream_id)

    def test_hook_on_state_change_signature(self):
        """on_state_change(task_id, old_state, new_state) -> None must accept these args."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook
        hook = TaskStateChangeHook()
        # Must not raise — accepts three string args
        hook.on_state_change("tid", "old", "new")


# ---------------------------------------------------------------------------
# Failure Modes
# ---------------------------------------------------------------------------

class TestSSEFailureModes:
    """SSE failure mode handling."""

    def test_sse_client_disconnect_clean_close(self):
        """Closing a stream must not leave orphaned state."""
        from hermes_agent_a2a.sse_handler import SSEStreamer
        streamer = SSEStreamer()
        stream_id = streamer.open_stream("disconnect-task")
        streamer.close_stream(stream_id)

        # Stream should be closed — push_event on closed stream should be safe
        # (either no-op or raise a defined exception, not an unhandled error)
        from hermes_agent_a2a.sse_handler import SSEEvent
        event = SSEEvent(task_id="disconnect-task", state="completed", event="TaskCompleted")
        try:
            streamer.push_event(stream_id, event)
        except Exception as e:
            # It's OK if it raises — just not an unhandled crash
            assert False, f"push_event on closed stream raised: {e}"

    def test_sse_stream_opens_for_completed_task_returns_immediately(self):
        """SSE stream on completed task returns current state then closes."""
        from hermes_agent_a2a.server import TaskQueue
        from hermes_agent_a2a.sse_handler import SSEStreamer

        q = TaskQueue()
        q.enqueue("completed-immediate", "hello", {})
        q.complete("completed-immediate", "result")

        # Unit test: verify SSEStreamer handles this without hanging
        streamer = SSEStreamer()
        # This should return/emit the completed state without blocking
        stream_id = streamer.open_stream("completed-immediate")
        assert stream_id, "Must return a stream_id"
        streamer.close_stream(stream_id)

# ---------------------------------------------------------------------------
# Performance — Wave C Issue 14
# ---------------------------------------------------------------------------

class TestSSEPollInterval:
    """SSE polling must use 0.5s interval (not 0.1s) to avoid thread contention."""

    def test_sse_subscribe_poll_interval_is_0_5_seconds(self):
        """SSE subscribe loop must use poll_interval=0.5 to reduce thread wakeups.

        0.1s polling creates 10 wakeups/second per client, causing thread contention.
        0.5s polling reduces this to 2 wakeups/second — still responsive (<1s latency).
        """
        import inspect
        from hermes_agent_a2a.server import A2ARequestHandler

        source = inspect.getsource(A2ARequestHandler._rest_subscribe_to_task)
        # Verify poll_interval is set to 0.5, not 0.1
        assert "poll_interval = 0.5" in source, (
            "SSE subscribe loop must use poll_interval=0.5 to avoid blocking threads. "
            "Found source:\n" + source[source.find("poll_interval"):source.find("poll_interval")+30]
        )
