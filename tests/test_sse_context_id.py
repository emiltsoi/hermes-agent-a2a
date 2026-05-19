"""TDD tests for context_id SSE compliance — F-F016 / Google A2A spec.

These tests define the expected behaviour from the spec:
  TaskStatusUpdateEvent requires { taskId, contextId, status: { state, message, timestamp } }

Tests written to FAIL before the fix is implemented.
"""
import json
import time

import pytest


class TestPendingTaskContextId:
    """_PendingTask must store context_id."""

    def test_pending_task_has_context_id_slot(self):
        """_PendingTask.__slots__ must include context_id."""
        from hermes_agent_a2a.server import _PendingTask
        assert hasattr(_PendingTask, "__slots__"), "_PendingTask must have __slots__"
        assert "context_id" in _PendingTask.__slots__, (
            "_PendingTask.__slots__ must include 'context_id' but got: "
            f"{_PendingTask.__slots__}"
        )

    def test_pending_task_init_accepts_context_id(self):
        """_PendingTask.__init__ must accept context_id."""
        from hermes_agent_a2a.server import _PendingTask
        # Must not raise
        task = _PendingTask(
            task_id="t1",
            text="hello",
            metadata={},
            context_id="ctx-1",
        )
        assert task.context_id == "ctx-1"


class TestTaskQueueContextId:
    """TaskQueue.enqueue must accept and store context_id."""

    def test_enqueue_accepts_context_id(self):
        """enqueue() must accept a context_id parameter."""
        from hermes_agent_a2a.server import TaskQueue
        q = TaskQueue()
        # Must not raise
        task = q.enqueue(
            task_id="t-enq-1",
            text="hello",
            metadata={},
            context_id="ctx-enq-1",
        )
        assert task is not None, "enqueue must return a task"
        assert task.context_id == "ctx-enq-1", (
            f"task.context_id must be 'ctx-enq-1', got: {task.context_id!r}"
        )

    def test_enqueue_context_id_optional(self):
        """enqueue() without context_id must not raise."""
        from hermes_agent_a2a.server import TaskQueue
        q = TaskQueue()
        task = q.enqueue(task_id="t-no-ctx", text="hello", metadata={})
        assert task is not None
        # context_id should default to None or task_id
        assert hasattr(task, "context_id")


class TestSSEEventStructure:
    """SSEEvent.to_sse_line() must emit the Google A2A TaskStatusUpdateEvent spec structure."""

    def test_sse_event_has_context_id_field(self):
        """SSEEvent must have a context_id field."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        event = SSEEvent(
            task_id="t-sse-1",
            state="working",
            event="TaskWorking",
        )
        # Must have context_id attribute
        assert hasattr(event, "context_id"), "SSEEvent must have context_id field"

    def test_sse_event_to_sse_line_includes_context_id(self):
        """to_sse_line() must include contextId in the emitted payload."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        event = SSEEvent(
            task_id="t-sse-2",
            state="working",
            event="TaskWorking",
            context_id="ctx-sse-1",
        )
        line = event.to_sse_line()
        parsed = json.loads(line.split("data: ", 1)[1])
        assert "contextId" in parsed, f"payload must include contextId, got: {parsed}"
        assert parsed["contextId"] == "ctx-sse-1", (
            f"contextId must be 'ctx-sse-1', got: {parsed['contextId']!r}"
        )

    def test_sse_event_to_sse_line_nests_state_under_status(self):
        """to_sse_line() must nest 'state' inside a 'status' object."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        event = SSEEvent(
            task_id="t-sse-3",
            state="working",
            event="TaskWorking",
            context_id="ctx-sse-2",
        )
        line = event.to_sse_line()
        parsed = json.loads(line.split("data: ", 1)[1])

        assert "status" in parsed, f"payload must have 'status' key, got: {parsed}"
        assert isinstance(parsed["status"], dict), (
            f"status must be a dict, got: {type(parsed['status'])}"
        )
        assert "state" in parsed["status"], (
            f"status.state must exist, got: {parsed['status']}"
        )
        assert parsed["status"]["state"] == "working", (
            f"status.state must be 'working', got: {parsed['status']['state']!r}"
        )
        # state must NOT appear at top level
        assert "state" not in parsed, (
            f"'state' must NOT appear at top-level (must be nested in status), got: {parsed}"
        )

    def test_sse_event_to_sse_line_includes_timestamp_in_status(self):
        """to_sse_line() status object must include ISO-8601 timestamp."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        before = time.time()
        event = SSEEvent(
            task_id="t-sse-4",
            state="working",
            event="TaskWorking",
            context_id="ctx-sse-3",
        )
        after = time.time()
        line = event.to_sse_line()
        parsed = json.loads(line.split("data: ", 1)[1])

        assert "status" in parsed, f"payload must have 'status' key, got: {parsed}"
        assert "timestamp" in parsed["status"], (
            f"status.timestamp must exist, got: {parsed['status']}"
        )
        ts = parsed["status"]["timestamp"]
        # Verify it's a reasonable ISO-8601 timestamp
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pytest.fail(f"timestamp must be valid ISO-8601, got: {ts!r}")
        ts_epoch = dt.timestamp()
        assert before - 1 <= ts_epoch <= after + 1, (
            f"timestamp {ts} must be close to current time"
        )

    def test_sse_event_to_sse_line_full_payload_structure(self):
        """Full payload must match TaskStatusUpdateEvent spec."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        event = SSEEvent(
            task_id="t-full-1",
            state="completed",
            event="TaskCompleted",
            context_id="ctx-full-1",
        )
        line = event.to_sse_line()
        parsed = json.loads(line.split("data: ", 1)[1])

        # Required top-level keys
        assert parsed.get("taskId") == "t-full-1", f"taskId mismatch: {parsed}"
        assert parsed.get("contextId") == "ctx-full-1", f"contextId mismatch: {parsed}"
        assert "status" in parsed, f"status missing: {parsed}"
        assert "state" not in parsed, f"state must not be top-level: {parsed}"

        status = parsed["status"]
        assert isinstance(status, dict), f"status must be dict: {status}"
        assert status.get("state") == "completed", f"status.state mismatch: {status}"
        assert "timestamp" in status, f"status.timestamp missing: {status}"


class TestTaskStateChangeHookContextId:
    """TaskStateChangeHook must include context_id in emitted SSE events."""

    def test_hook_on_state_change_includes_context_id(self):
        """on_state_change must pass context_id to SSEEvent."""
        from unittest.mock import patch, MagicMock
        from hermes_agent_a2a.hooks import TaskStateChangeHook
        from hermes_agent_a2a.sse_handler import SSEStreamer

        # Capture the SSEEvent that gets pushed
        captured = {}

        class FakeStreamer:
            def get_stream_ids_for_task(self, task_id):
                return ["fake-stream"]

            def push_event(self, stream_id, event):
                captured["event"] = event

        hook = TaskStateChangeHook()
        hook._sse_streamer = FakeStreamer()  # inject

        # Must accept context_id argument
        hook.on_state_change(
            task_id="t-hook-1",
            old_state="submitted",
            new_state="working",
            context_id="ctx-hook-1",
        )

        event = captured.get("event")
        assert event is not None, "SSEEvent was not pushed"
        assert hasattr(event, "context_id"), "SSEEvent must have context_id"
        assert event.context_id == "ctx-hook-1", (
            f"context_id must be 'ctx-hook-1', got: {event.context_id!r}"
        )

    def test_hook_emits_sse_with_nested_status(self):
        """SSEEvent emitted by hook must have nested status structure."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        captured_event = None

        class FakeStreamer:
            def get_stream_ids_for_task(self, task_id):
                return ["fake-stream-2"]

            def push_event(self, stream_id, event):
                nonlocal captured_event
                captured_event = event

        hook = TaskStateChangeHook()
        hook._sse_streamer = FakeStreamer()

        hook.on_state_change(
            task_id="t-hook-status",
            old_state="submitted",
            new_state="working",
            context_id="ctx-status-1",
        )

        assert captured_event is not None
        line = captured_event.to_sse_line()
        parsed = json.loads(line.split("data: ", 1)[1])

        assert "status" in parsed, f"status must be in SSE payload: {parsed}"
        assert isinstance(parsed["status"], dict), "status must be a dict"
        assert "state" in parsed["status"], f"status.state missing: {parsed}"
        assert "contextId" in parsed, f"contextId missing: {parsed}"