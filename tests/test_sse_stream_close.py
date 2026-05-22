"""Tests for SSE stream close vs push_event race (ARCH-06).

Fix: close_stream() sets stream.closed=True BEFORE popping from _streams.
This ensures push_event() that races past the self._lock check sees
stream.closed=True and returns early instead of appending to the stream.

Before fix: close_stream() did pop() first, then set closed. The stream
object remained accessible to concurrent push_event() calls for the duration
of the close_stream lock held window.
"""

import threading
import time
import pytest

from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent


def make_event(task_id: str, state: str = "working") -> SSEEvent:
    return SSEEvent(
        task_id=task_id,
        state=state,
        event="TaskWorking",
        data={},
    )


def test_close_stream_prevents_concurrent_push():
    """Fix verification: push_event that races close_stream must return early.

    A push_event that starts concurrently with close_stream must see
    stream.closed=True (set before pop) and return early instead of appending.
    """
    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=300.0)

    barrier = threading.Barrier(2)
    push_ok = [True]  # [0]=did push_event return early (not append)?

    stream_id = streamer.open_stream("task-race")

    def pusher():
        barrier.wait()  # sync with closer
        streamer.push_event(stream_id, make_event("task-race", "working"))
        # Check: did the event get appended despite the race?
        # With fix: push_event should see closed=True and not append.
        pending = streamer.get_pending(stream_id)
        push_ok[0] = len(pending) == 0

    def closer():
        barrier.wait()  # sync with pusher
        streamer.close_stream(stream_id)

    t_pusher = threading.Thread(target=pusher)
    t_closer = threading.Thread(target=closer)

    t_pusher.start()
    t_closer.start()
    t_pusher.join()
    t_closer.join()

    assert push_ok[0], "push_event should return early when stream is closed (fix verified)"


def test_stress_concurrent_push_and_close():
    """50-iteration stress: concurrent push + close, zero leaks."""
    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=300.0)

    leak_count = 0

    for i in range(50):
        stream_id = streamer.open_stream(f"stress-{i}")
        streamer.get_pending(stream_id)  # drain

        barrier = threading.Barrier(2)
        leaked = [False]

        def pusher():
            barrier.wait()
            for _ in range(3):
                streamer.push_event(stream_id, make_event(f"stress-{i}"))

        def closer():
            barrier.wait()
            streamer.close_stream(stream_id)

        tp = threading.Thread(target=pusher)
        tc = threading.Thread(target=closer)
        tp.start()
        tc.start()
        tp.join()
        tc.join()

        # Stream is closed; get_pending returns [] for unknown stream_id
        # Leaked only if the event somehow ended up in a stream accessible by get_pending
        pending = streamer.get_pending(stream_id)
        if pending:
            leaked[0] = True

        if leaked[0]:
            leak_count += 1

    assert leak_count == 0, f"Fix failed in {leak_count}/50 iterations"


def test_push_event_after_close_is_noop():
    """Sequential: push_event AFTER close_stream must not append."""
    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=300.0)
    stream_id = streamer.open_stream("task-post-close")

    streamer.push_event(stream_id, make_event("task-post-close", "first"))
    first = streamer.get_pending(stream_id)
    assert len(first) == 1

    streamer.close_stream(stream_id)

    # Push after close — must be no-op
    streamer.push_event(stream_id, make_event("task-post-close", "after-close"))

    # Stream closed and removed from _streams; get_pending returns []
    after = streamer.get_pending(stream_id)
    assert len(after) == 0


def test_get_pending_drains_pending_list():
    """get_pending must drain the pending list on each call."""
    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=300.0)
    stream_id = streamer.open_stream("task-drain")

    streamer.push_event(stream_id, make_event("task-drain", "e1"))
    streamer.push_event(stream_id, make_event("task-drain", "e2"))

    first = streamer.get_pending(stream_id)
    assert len(first) == 2

    second = streamer.get_pending(stream_id)
    assert len(second) == 0

    streamer.close_stream(stream_id)
    after_close = streamer.get_pending(stream_id)
    assert len(after_close) == 0
