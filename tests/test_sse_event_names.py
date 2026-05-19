"""TDD tests for SSE event name normalization per Google A2A spec.

Canonical event names per spec:
  - "TaskStarted"     — task moves to "working" state
  - "TaskStatusUpdate" — TaskStatusUpdateEvent (not "message")
  - "TaskArtifactUpdate" — TaskArtifactUpdateEvent
  - "TaskCompleted"   — completed terminal state
  - "TaskFailed"      — failed terminal state
  - "TaskCanceled"    — canceled terminal state
  - "TaskRejected"    — rejected terminal state
  - "TaskAuthRequired" — auth_required state
  - "TaskAuthenticated" — authenticated state
  - "TaskSubmitted"   — submitted state
  - "TaskUpdated"     — fallback for unknown states

These tests define the expected behaviour from the spec.
They are written to FAIL before the fix is implemented.
"""

import json

import pytest


class TestSSEEventNames:
    """SSE event names must match the canonical names from the Google A2A spec."""

    def test_event_name_for_working_uses_task_started(self):
        """State 'working' must emit SSE event name 'TaskStarted'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("working")
        assert name == "TaskStarted", (
            f"state 'working' must emit 'TaskStarted', got: {name!r}"
        )

    def test_event_name_for_completed_uses_task_completed(self):
        """State 'completed' must emit SSE event name 'TaskCompleted'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("completed")
        assert name == "TaskCompleted", (
            f"state 'completed' must emit 'TaskCompleted', got: {name!r}"
        )

    def test_event_name_for_failed_uses_task_failed(self):
        """State 'failed' must emit SSE event name 'TaskFailed'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("failed")
        assert name == "TaskFailed", (
            f"state 'failed' must emit 'TaskFailed', got: {name!r}"
        )

    def test_event_name_for_canceled_uses_task_canceled(self):
        """State 'canceled' must emit SSE event name 'TaskCanceled'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("canceled")
        assert name == "TaskCanceled", (
            f"state 'canceled' must emit 'TaskCanceled', got: {name!r}"
        )

    def test_event_name_for_rejected_uses_task_rejected(self):
        """State 'rejected' must emit SSE event name 'TaskRejected'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("rejected")
        assert name == "TaskRejected", (
            f"state 'rejected' must emit 'TaskRejected', got: {name!r}"
        )

    def test_event_name_for_auth_required_uses_task_auth_required(self):
        """State 'auth_required' must emit SSE event name 'TaskAuthRequired'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("auth_required")
        assert name == "TaskAuthRequired", (
            f"state 'auth_required' must emit 'TaskAuthRequired', got: {name!r}"
        )

    def test_event_name_for_authenticated_uses_task_authenticated(self):
        """State 'authenticated' must emit SSE event name 'TaskAuthenticated'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("authenticated")
        assert name == "TaskAuthenticated", (
            f"state 'authenticated' must emit 'TaskAuthenticated', got: {name!r}"
        )

    def test_event_name_for_submitted_uses_task_submitted(self):
        """State 'submitted' must emit SSE event name 'TaskSubmitted'."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("submitted")
        assert name == "TaskSubmitted", (
            f"state 'submitted' must emit 'TaskSubmitted', got: {name!r}"
        )

    def test_unknown_state_uses_task_updated_fallback(self):
        """Unknown state must emit SSE event name 'TaskUpdated' (fallback)."""
        from hermes_agent_a2a.hooks import _event_name_for_state
        name = _event_name_for_state("some-unknown-state")
        assert name == "TaskUpdated", (
            f"unknown state must emit 'TaskUpdated', got: {name!r}"
        )


class TestSSEEventToSSELine:
    """SSEEvent.to_sse_line() must emit the correct event name in the SSE line."""

    def _parse_sse_event_name(self, line: str) -> str:
        """Extract the event name from an SSE 'event: ...' line."""
        for lpart in line.split("\n"):
            if lpart.startswith("event:"):
                return lpart[6:].strip()
        return ""

    def test_artifact_event_uses_task_artifact_update(self):
        """emit_artifact_event() must use event name 'TaskArtifactUpdate'."""
        from hermes_agent_a2a.sse_handler import emit_artifact_event
        evt = emit_artifact_event(
            task_id="t1",
            context_id="ctx1",
            artifact={"parts": []},
        )
        # The event name is in the SSE line prefixed "event: ..."
        sse_line = evt.to_sse_line()
        event_name = self._parse_sse_event_name(sse_line)
        assert event_name == "TaskArtifactUpdate", (
            f"artifact event must emit 'TaskArtifactUpdate', got: {event_name!r}"
        )

    def test_status_event_uses_task_started_for_working(self):
        """SSEEvent with state='working' must emit 'TaskStarted' in SSE line."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        evt = SSEEvent(
            task_id="t1",
            state="working",
            event="TaskStarted",
            context_id="ctx1",
        )
        sse_line = evt.to_sse_line()
        event_name = self._parse_sse_event_name(sse_line)
        assert event_name == "TaskStarted", (
            f"SSE line must emit 'TaskStarted', got: {event_name!r}"
        )

    def test_status_event_uses_task_completed_for_completed(self):
        """SSEEvent with state='completed' must emit 'TaskCompleted' in SSE line."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        evt = SSEEvent(
            task_id="t1",
            state="completed",
            event="TaskCompleted",
            context_id="ctx1",
        )
        sse_line = evt.to_sse_line()
        event_name = self._parse_sse_event_name(sse_line)
        assert event_name == "TaskCompleted", (
            f"SSE line must emit 'TaskCompleted', got: {event_name!r}"
        )


class TestTaskStateChangeHookEventNames:
    """TaskStateChangeHook must emit canonical event names."""

    def test_hook_emits_task_started_for_working(self):
        """on_state_change(..., 'working') must push SSE event with name 'TaskStarted'."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        captured_event = None

        class FakeStreamer:
            def get_stream_ids_for_task(self, task_id):
                return ["fake-stream"]

            def push_event(self, stream_id, event):
                nonlocal captured_event
                captured_event = event

        hook = TaskStateChangeHook()
        hook._sse_streamer = FakeStreamer()
        hook.on_state_change("tid", "submitted", "working")

        assert captured_event is not None
        assert captured_event.event == "TaskStarted", (
            f"hook must emit 'TaskStarted', got: {captured_event.event!r}"
        )

    def test_hook_emits_task_completed_for_completed(self):
        """on_state_change(..., 'completed') must push SSE event with name 'TaskCompleted'."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        captured_event = None

        class FakeStreamer:
            def get_stream_ids_for_task(self, task_id):
                return ["fake-stream"]

            def push_event(self, stream_id, event):
                nonlocal captured_event
                captured_event = event

        hook = TaskStateChangeHook()
        hook._sse_streamer = FakeStreamer()
        hook.on_state_change("tid", "working", "completed")

        assert captured_event is not None
        assert captured_event.event == "TaskCompleted", (
            f"hook must emit 'TaskCompleted', got: {captured_event.event!r}"
        )

    def test_hook_emits_task_failed_for_failed(self):
        """on_state_change(..., 'failed') must push SSE event with name 'TaskFailed'."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        captured_event = None

        class FakeStreamer:
            def get_stream_ids_for_task(self, task_id):
                return ["fake-stream"]

            def push_event(self, stream_id, event):
                nonlocal captured_event
                captured_event = event

        hook = TaskStateChangeHook()
        hook._sse_streamer = FakeStreamer()
        hook.on_state_change("tid", "working", "failed")

        assert captured_event is not None
        assert captured_event.event == "TaskFailed", (
            f"hook must emit 'TaskFailed', got: {captured_event.event!r}"
        )

    def test_hook_emits_task_canceled_for_canceled(self):
        """on_state_change(..., 'canceled') must push SSE event with name 'TaskCanceled'."""
        from hermes_agent_a2a.hooks import TaskStateChangeHook

        captured_event = None

        class FakeStreamer:
            def get_stream_ids_for_task(self, task_id):
                return ["fake-stream"]

            def push_event(self, stream_id, event):
                nonlocal captured_event
                captured_event = event

        hook = TaskStateChangeHook()
        hook._sse_streamer = FakeStreamer()
        hook.on_state_change("tid", "working", "canceled")

        assert captured_event is not None
        assert captured_event.event == "TaskCanceled", (
            f"hook must emit 'TaskCanceled', got: {captured_event.event!r}"
        )