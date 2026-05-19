"""Tests for SSE stream cleanup when clients disconnect uncleanly (Wave D, Issue 18).

Covers:
1. SSE stream is closed and cleaned up when client drops connection
2. SSEStreamer.cleanup_thread removes stale streams on next invocation
3. Orphaned streams time out after idle_timeout and are removed
"""

import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent


class TestSSEUncleanDisconnect:
    """SSE stream cleanup when client disconnects without calling close_stream."""

    def test_close_stream_removes_stream_immediately(self):
        """close_stream must immediately remove the stream from _streams dict."""
        streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-disconnect-1")

        # Stream must exist before close
        assert not streamer.is_closed(stream_id)

        streamer.close_stream(stream_id)

        # After close, stream must be gone
        assert streamer.is_closed(stream_id)
        assert stream_id not in streamer._streams

    def test_push_event_on_closed_stream_is_noop(self):
        """push_event on a closed stream must not raise."""
        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-push-closed")
        streamer.close_stream(stream_id)

        event = SSEEvent(task_id="task-push-closed", state="completed", event="TaskCompleted")
        # Must not raise — closed streams are gracefully ignored
        streamer.push_event(stream_id, event)

    def test_get_pending_on_closed_stream_returns_empty(self):
        """get_pending on a closed stream must return empty list."""
        streamer = SSEStreamer()
        stream_id = streamer.open_stream("task-pending-closed")

        event = SSEEvent(task_id="task-pending-closed", state="working", event="TaskWorking")
        streamer.push_event(stream_id, event)

        streamer.close_stream(stream_id)

        # Must not raise, must return empty
        assert streamer.get_pending(stream_id) == []

    def test_orphan_stream_not_closed_until_idle_timeout(self):
        """An orphaned stream (no close_stream called) must survive until idle_timeout.

        The cleanup thread only removes streams that have been idle for > idle_timeout.
        So a fresh orphaned stream should still be present immediately after disconnect.
        """
        streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-orphan-1")

        # Stream is still open — is_closed returns False
        assert not streamer.is_closed(stream_id)

        # Even without calling close_stream, the stream still exists
        # (it will be cleaned up by the background thread after idle_timeout)


class TestCleanupThreadRemovesStaleStreams:
    """SSEStreamer.cleanup_thread removes stale streams on next invocation."""

    def test__close_idle_streams_removes_idle_stream(self):
        """_close_idle_streams must remove a stream that has been idle > idle_timeout."""
        import logging
        logger = logging.getLogger("test")

        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-idle-1")

        # Wait for the stream to become idle
        time.sleep(0.15)

        # Manually trigger cleanup — stream should be removed
        streamer._close_idle_streams(logger)

        # The stream must have been removed
        assert streamer.is_closed(stream_id)

    def test_active_stream_not_removed_by_cleanup(self):
        """A stream that received push_event recently must NOT be removed by cleanup."""
        import logging
        logger = logging.getLogger("test")

        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-active-1")

        # Push an event to reset idle_since
        event = SSEEvent(task_id="task-active-1", state="working", event="TaskWorking")
        streamer.push_event(stream_id, event)

        # Wait but then push again before cleanup runs
        time.sleep(0.05)
        streamer.push_event(stream_id, event)

        # Now run cleanup
        streamer._close_idle_streams(logger)

        # Stream must still be present — it was active
        assert not streamer.is_closed(stream_id)

    def test_cleanup_thread_runs_periodically(self):
        """The cleanup thread must run _close_idle_streams every cleanup_interval."""
        cleanup_calls = []

        original_close_idle = SSEStreamer._close_idle_streams

        def tracking_close_idle(self, logger):
            cleanup_calls.append(time.time())
            return original_close_idle(self, logger)

        streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=0.1)

        with patch.object(SSEStreamer, "_close_idle_streams", tracking_close_idle):
            streamer.open_stream("task-periodic-1")
            time.sleep(0.35)  # Wait for ~3 cleanup cycles

        # Should have at least 2 cleanup calls within 0.35s with 0.1s interval
        assert len(cleanup_calls) >= 2, \
            f"Expected at least 2 cleanup calls, got {len(cleanup_calls)}"

    def test_multiple_idle_streams_all_removed(self):
        """When multiple streams are idle, all must be removed by cleanup."""
        import logging
        logger = logging.getLogger("test")

        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)

        # Open multiple streams
        sid1 = streamer.open_stream("task-multi-1")
        sid2 = streamer.open_stream("task-multi-2")
        sid3 = streamer.open_stream("task-multi-3")

        # Wait for them to all become idle
        time.sleep(0.2)

        # Run cleanup
        streamer._close_idle_streams(logger)

        # All streams must be closed
        assert streamer.is_closed(sid1)
        assert streamer.is_closed(sid2)
        assert streamer.is_closed(sid3)


class TestOrphanedStreamsTimeout:
    """Orphaned streams (client disconnected without close_stream) must time out."""

    def test_orphaned_stream_removed_after_idle_timeout(self):
        """An orphaned stream must be removed after being idle for > idle_timeout."""
        streamer = SSEStreamer(idle_timeout=0.2, cleanup_interval=1.0)
        stream_id = streamer.open_stream("task-orphaned-timeout")

        # Orphan the stream (don't call close_stream)
        assert not streamer.is_closed(stream_id)

        # Wait longer than idle_timeout
        time.sleep(0.3)

        # Run cleanup explicitly (normally this runs in background thread)
        import logging
        logger = logging.getLogger("test")
        streamer._close_idle_streams(logger)

        # Orphaned stream must now be removed
        assert streamer.is_closed(stream_id)

    def test_get_pending_does_not_reset_idle(self):
        """get_pending does NOT extend stream lifetime — only push_event does.

        A stream with no pending events times out even when client is polling it.
        This prevents zombie connections from consuming resources indefinitely.
        """
        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-polling-1")

        # Push an event first so stream has content
        event = SSEEvent(task_id="task-polling-1", state="working", event="TaskWorking")
        streamer.push_event(stream_id, event)

        time.sleep(0.05)

        # get_pending reads the events but does NOT reset _last_activity
        streamer.get_pending(stream_id)

        # Total time > 0.1s timeout; stream should be closed
        time.sleep(0.08)

        import logging
        logger = logging.getLogger("test")
        streamer._close_idle_streams(logger)

        # Stream must be closed since only push_event (not get_pending) extends lifetime
        assert streamer.is_closed(stream_id)

    def test_task_index_cleaned_up_when_stream_removed(self):
        """When a stream is removed, it must also be removed from _by_task index."""
        import logging
        logger = logging.getLogger("test")

        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-index-cleanup")

        # Verify stream is in the task index
        assert stream_id in streamer.get_stream_ids_for_task("task-index-cleanup")

        # Wait for idle timeout and run cleanup
        time.sleep(0.15)
        streamer._close_idle_streams(logger)

        # Stream must be gone from both _streams and _by_task
        assert streamer.is_closed(stream_id)
        assert stream_id not in streamer.get_stream_ids_for_task("task-index-cleanup")

    def test_closed_stream_already_in_task_index_on_second_cleanup(self):
        """If a stream was already removed from _streams but still in _by_task,
        second cleanup pass must not fail and must clean up _by_task."""
        import logging
        logger = logging.getLogger("test")

        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-double-cleanup")

        # Wait for idle timeout
        time.sleep(0.15)

        # First cleanup removes from _streams
        streamer._close_idle_streams(logger)
        assert streamer.is_closed(stream_id)

        # Second cleanup pass must not raise and must clean up _by_task
        streamer._close_idle_streams(logger)

        # _by_task must be clean too
        assert stream_id not in streamer.get_stream_ids_for_task("task-double-cleanup")


class TestSimulatedUncleanDisconnect:
    """Simulate the scenario where a client drops connection without calling close_stream."""

    def test_simulated_unclean_disconnect_stream_gets_cleaned_up(self):
        """Simulate client crash/drop: stream left open, then background cleanup removes it."""
        streamer = SSEStreamer(idle_timeout=0.2, cleanup_interval=0.5)

        # Client opens stream
        stream_id = streamer.open_stream("task-client-crash")

        # Client crashes/disconnects — close_stream is NEVER called

        # Verify stream is still open
        assert not streamer.is_closed(stream_id)

        # Wait for idle_timeout + some margin for cleanup_interval
        time.sleep(0.6)  # 0.5 interval + 0.1 margin

        # The cleanup thread should have removed the orphaned stream
        # (We can verify by checking the cleanup ran and removed the stream)
        import logging
        logger = logging.getLogger("test")

        # Force a cleanup cycle to verify
        streamer._close_idle_streams(logger)

        assert streamer.is_closed(stream_id)

    def test_unclean_disconnect_with_pending_events(self):
        """If client disconnects with pending events, those events are discarded on cleanup."""
        streamer = SSEStreamer(idle_timeout=0.1, cleanup_interval=60.0)
        stream_id = streamer.open_stream("task-pending-discard")

        # Push some events before disconnect
        for state in ["working", "completed"]:
            event = SSEEvent(
                task_id="task-pending-discard",
                state=state,
                event=f"Task{state.capitalize()}"
            )
            streamer.push_event(stream_id, event)

        # Get pending events (simulates client reading)
        pending1 = streamer.get_pending(stream_id)
        assert len(pending1) == 2

        # Client crashes without reading more events
        # (no more get_pending calls)

        # Wait for idle timeout
        time.sleep(0.15)

        # Run cleanup
        import logging
        logger = logging.getLogger("test")
        streamer._close_idle_streams(logger)

        # Stream is gone and pending events are discarded
        assert streamer.is_closed(stream_id)

    def test_multiple_clients_one_crashes_others_survive(self):
        """If multiple clients subscribe and one disconnects uncleanly,
        the other streams must not be affected."""
        streamer = SSEStreamer(idle_timeout=0.2, cleanup_interval=60.0)

        # Client A and B both subscribe
        stream_a = streamer.open_stream("task-shared")
        stream_b = streamer.open_stream("task-shared")

        # Push events to both (push_event resets _last_activity)
        event_a = SSEEvent(task_id="task-shared", state="working", event="TaskWorking")
        event_b = SSEEvent(task_id="task-shared", state="working", event="TaskWorking")
        streamer.push_event(stream_a, event_a)
        streamer.push_event(stream_b, event_b)

        # Client A disconnects cleanly
        streamer.close_stream(stream_a)

        # Client B keeps polling (read events) AND server pushes new events periodically
        # Note: only push_event extends _last_activity, not get_pending
        time.sleep(0.05)
        streamer.get_pending(stream_b)  # Read events, no lifetime extension
        streamer.push_event(stream_b, SSEEvent(task_id="task-shared", state="working", event="TaskWorking"))
        time.sleep(0.05)
        streamer.get_pending(stream_b)  # Read again
        streamer.push_event(stream_b, SSEEvent(task_id="task-shared", state="working", event="TaskWorking"))

        # Wait for idle timeout
        time.sleep(0.15)

        # Run cleanup
        import logging
        logger = logging.getLogger("test")
        streamer._close_idle_streams(logger)

        # Client A's stream was already removed
        assert streamer.is_closed(stream_a)

        # Client B's stream survived because it received push_event within timeout
        assert not streamer.is_closed(stream_b)
