"""TDD tests for SSE Last-Event-ID support — per Google A2A SSE spec.

SSE spec (https://html.spec.whatwg.org/multipage/server-sent-events.html)
requires:
  - Each event may carry an ``id`` field
  - The ``Last-Event-ID`` header is used by clients to resume a stream
  - Servers MUST send ``Last-Event-ID`` response header on initial response
  - Event IDs must be monotonically increasing per stream

Tests written to define the expected behaviour:
  1. SSEStreamer assigns event.id when not already set
  2. last_id is tracked per stream
  3. get_pending_after_id returns only events after a given ID
  4. get_last_event_id returns the last ID sent
  5. SSEEvent.to_sse_line() outputs the id: field correctly
"""

import json
import pytest


class TestSSEStreamerLastEventId:
    """SSEStreamer must assign monotonically increasing event IDs on push."""

    def test_push_event_auto_assigns_id_when_not_set(self):
        """push_event() must assign event.id when it is None."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-resume-1")

        event = SSEEvent(task_id="task-resume-1", state="working", event="TaskWorking")
        assert event.id is None, "event.id starts as None"

        streamer.push_event(stream_id, event)

        assert event.id is not None, "push_event must assign event.id"
        assert event.id.startswith("task-resume-1_"), (
            f"event.id must be 'task_id_<N>', got: {event.id!r}"
        )

    def test_push_event_id_is_monotonically_increasing(self):
        """Each push_event call must increment the sequence counter."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-resume-2")

        events = []
        for i in range(5):
            e = SSEEvent(task_id="task-resume-2", state="working", event="TaskWorking")
            streamer.push_event(stream_id, e)
            events.append(e)

        ids = [e.id for e in events]
        # All IDs must be non-None
        assert all(eid is not None for eid in ids), f"Some IDs were None: {ids}"
        # All IDs must be unique
        assert len(set(ids)) == len(ids), f"IDs are not unique: {ids}"
        # IDs must be in increasing order (lexicographic comparison of task_resume_2_<N>)
        for i in range(len(ids) - 1):
            assert ids[i] < ids[i + 1], (
                f"IDs must be monotonically increasing: {ids[i]!r} >= {ids[i+1]!r}"
            )

    def test_push_event_does_not_override_existing_id(self):
        """push_event() must NOT overwrite an already-set event.id."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-resume-3")

        custom_id = "my-custom-id-42"
        event = SSEEvent(
            task_id="task-resume-3",
            state="working",
            event="TaskWorking",
            id=custom_id,
        )
        streamer.push_event(stream_id, event)

        assert event.id == custom_id, (
            f"push_event must NOT overwrite existing id; got: {event.id!r}"
        )

    def test_last_id_tracked_per_stream(self):
        """get_last_event_id must return the ID of the last event sent."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-resume-4")

        e1 = SSEEvent(task_id="task-resume-4", state="working", event="TaskWorking")
        e2 = SSEEvent(task_id="task-resume-4", state="completed", event="TaskCompleted")

        streamer.push_event(stream_id, e1)
        assert streamer.get_last_event_id(stream_id) == e1.id

        streamer.push_event(stream_id, e2)
        assert streamer.get_last_event_id(stream_id) == e2.id

    def test_last_id_is_none_before_any_push(self):
        """get_last_event_id must return None before any event is pushed."""
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-resume-5")

        assert streamer.get_last_event_id(stream_id) is None

    def test_last_id_unknown_stream_returns_none(self):
        """get_last_event_id on unknown stream_id must return None."""
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        assert streamer.get_last_event_id("nonexistent-stream") is None


class TestGetPendingAfterId:
    """get_pending_after_id must return only events with id > after_id."""

    def _push_events(self, streamer, stream_id, task_id, states):
        """Push a sequence of events and return their ids."""
        from hermes_agent_a2a.sse_handler import SSEEvent
        ids = []
        for state in states:
            e = SSEEvent(task_id=task_id, state=state, event=f"Task{state.title()}")
            streamer.push_event(stream_id, e)
            ids.append(e.id)
        return ids

    def test_get_pending_after_id_filters_correctly(self):
        """get_pending_after_id must return only events after the given ID."""
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-after-1")
        ids = self._push_events(streamer, stream_id, "task-after-1", ["working", "completed"])

        after_first = streamer.get_pending_after_id(stream_id, ids[0])
        assert len(after_first) == 1, f"Expected 1 event after {ids[0]}, got: {after_first}"
        assert f"id: {ids[1]}" in after_first[0], (
            f"Expected event with id {ids[1]}, got: {after_first[0]!r}"
        )

    def test_get_pending_after_id_empty_when_no_events_after(self):
        """get_pending_after_id must return [] when after_id >= all event ids."""
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-after-2")
        ids = self._push_events(streamer, stream_id, "task-after-2", ["working", "completed"])

        after_last = streamer.get_pending_after_id(stream_id, ids[1])
        assert after_last == [], f"Expected empty list after last id {ids[1]}, got: {after_last}"

    def test_get_pending_after_id_unknown_stream_returns_empty(self):
        """get_pending_after_id on unknown stream must return []."""
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        result = streamer.get_pending_after_id("nonexistent-stream", "any-id")
        assert result == []

    def test_get_pending_after_id_includes_events_without_id_field(self):
        """Lines without an id: field must always be included (SSE spec)."""
        from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-no-id")

        # Push an event with an ID
        e1 = SSEEvent(task_id="task-no-id", state="working", event="TaskWorking")
        streamer.push_event(stream_id, e1)

        # Add a raw line directly (simulating a line without id field)
        with streamer._lock:
            stream = streamer._streams[stream_id]
            with stream.pending_lock:
                stream.pending.append("data: raw event without id\n\n")

        after_id = streamer.get_pending_after_id(stream_id, e1.id)
        # Should include the raw line
        assert len(after_id) == 1
        assert "raw event without id" in after_id[0]

    def test_get_pending_after_id_empty_string_returns_all_pending(self):
        """after_id='' (or '0') should return all pending events."""
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-empty-after")
        ids = self._push_events(streamer, stream_id, "task-empty-after", ["working", "completed"])

        after_empty = streamer.get_pending_after_id(stream_id, "")
        assert len(after_empty) == 2, f"Expected 2 events after '', got: {len(after_empty)}"


class TestSSEEventIdLine:
    """SSEEvent.to_sse_line() must correctly emit the id: field."""

    def test_sse_event_to_sse_line_with_id(self):
        """to_sse_line() must output 'id: <event_id>' as the first line."""
        from hermes_agent_a2a.sse_handler import SSEEvent

        event = SSEEvent(
            task_id="t-line-1",
            state="working",
            event="TaskWorking",
            id="t-line-1_3",
        )
        line = event.to_sse_line()
        first_line = line.split("\n")[0]
        assert first_line == "id: t-line-1_3", (
            f"First SSE line must be 'id: <event_id>', got: {first_line!r}"
        )

    def test_sse_event_to_sse_line_without_id_omits_id_field(self):
        """to_sse_line() must NOT output 'id:' when event.id is None."""
        from hermes_agent_a2a.sse_handler import SSEEvent

        event = SSEEvent(
            task_id="t-no-id-line",
            state="working",
            event="TaskWorking",
            id=None,
        )
        line = event.to_sse_line()
        assert not line.startswith("id:"), (
            f"SSE line must not start with 'id:' when event.id is None, got: {line!r}"
        )

    def test_sse_event_to_sse_line_id_comes_before_event(self):
        """The 'id:' field must appear before 'event:' per SSE spec ordering."""
        from hermes_agent_a2a.sse_handler import SSEEvent

        event = SSEEvent(
            task_id="t-order-1",
            state="completed",
            event="TaskCompleted",
            id="t-order-1_1",
        )
        line = event.to_sse_line()
        id_pos = line.find("id: ")
        event_pos = line.find("event: ")
        assert id_pos != -1, f"'id:' not found in: {line!r}"
        assert event_pos != -1, f"'event:' not found in: {line!r}"
        assert id_pos < event_pos, (
            f"'id:' (pos {id_pos}) must come before 'event:' (pos {event_pos})"
        )


class TestTaskStateChangeHookLastEventId:
    """TaskStateChangeHook must emit events with auto-assigned IDs."""

    def test_hook_assigns_last_event_id_on_push(self):
        """The hook's SSE push must result in events with monotonically increasing IDs."""
        from unittest.mock import MagicMock
        from hermes_agent_a2a.hooks import TaskStateChangeHook
        from hermes_agent_a2a.sse_handler import SSEStreamer

        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-hook-1")

        # Inject the real streamer
        hook = TaskStateChangeHook()
        hook._sse_streamer = streamer

        hook.on_state_change(
            task_id="task-hook-1",
            old_state="submitted",
            new_state="working",
            context_id="ctx-hook-1",
        )
        hook.on_state_change(
            task_id="task-hook-1",
            old_state="working",
            new_state="completed",
            context_id="ctx-hook-1",
        )

        pending = streamer.get_pending(stream_id)
        assert len(pending) == 2, f"Expected 2 events, got: {len(pending)}"

        # Verify both events have id: fields with increasing IDs
        ids = []
        for line in pending:
            for l in line.strip().split("\n"):
                if l.startswith("id:"):
                    ids.append(l)
                    break

        assert len(ids) == 2, f"Expected 2 id: lines, got: {ids}"
        assert ids[0] < ids[1], f"IDs must be increasing: {ids}"
