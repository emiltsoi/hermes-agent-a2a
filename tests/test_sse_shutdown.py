"""Tests for SSEStreamer shutdown behavior (Wave E, Issue 24)."""

import threading
import time

import pytest

from hermes_agent_a2a.sse_handler import SSEStreamer, SSEEvent


def test_sse_streamer_shutdown_joins_cleanup_thread():
    """SSEStreamer.shutdown() must join the cleanup thread so no daemon threads leak."""
    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=5.0)

    # Open a stream to trigger cleanup thread start
    stream_id = streamer.open_stream("task-1")
    assert streamer._cleanup_thread is not None
    assert streamer._cleanup_thread.is_alive()

    # Shutdown must join the cleanup thread
    streamer.shutdown()

    # After shutdown the cleanup thread must not be alive
    assert not streamer._cleanup_thread.is_alive(), \
        "cleanup thread must not be alive after shutdown()"


def test_sse_streamer_shutdown_idempotent():
    """SSEStreamer.shutdown() must be safe to call multiple times."""
    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=5.0)

    # Open a stream to trigger cleanup thread start
    streamer.open_stream("task-1")

    # Calling shutdown twice must not raise
    streamer.shutdown()
    streamer.shutdown()  # must not raise

    assert not streamer._cleanup_thread.is_alive()


def test_sse_streamer_shutdown_blocks_until_thread_joins():
    """SSEStreamer.shutdown() must block until the cleanup thread has exited."""
    join_times = []

    original_shutdown = threading.Thread.join

    def patched_join(self, timeout=None):
        join_times.append(time.time())
        return original_shutdown(self, timeout)

    streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=0.5)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(threading.Thread, "join", patched_join)
        streamer.open_stream("task-1")
        streamer.shutdown()

    assert len(join_times) == 1, "join must be called exactly once on shutdown"


def test_sse_streamer_no_thread_leak_on_rapid_reload():
    """Rapid open→shutdown cycles must not accumulate threads."""
    threads_before = set(threading.enumerate())

    for _ in range(5):
        streamer = SSEStreamer(idle_timeout=300.0, cleanup_interval=60.0)
        streamer.open_stream("task-1")
        streamer.shutdown()

    threads_after = set(threading.enumerate())
    leaked = threads_after - threads_before

    # Filter out threads that are not SSE cleanup threads (some may be from test harness)
    sse_leaks = [t for t in leaked if t.name == "sse-idle-cleanup"]
    assert len(sse_leaks) == 0, f"SSE cleanup threads leaked: {sse_leaks}"


def test_plugin_on_shutdown_calls_streamer_shutdown():
    """HermesAgentA2APlugin.on_shutdown() must call streamer.shutdown()."""
    from unittest.mock import patch, MagicMock
    from hermes_agent_a2a import plugin as plugin_module
    from hermes_agent_a2a import sse_handler
    import importlib

    # Reload to pick up fresh module state
    importlib.reload(sse_handler)
    importlib.reload(plugin_module)

    # Patch get_sse_streamer so we can track the call
    mock_streamer = MagicMock()
    with patch.object(sse_handler, "get_sse_streamer", return_value=mock_streamer):
        plugin_module.HermesAgentA2APlugin().on_shutdown()

    mock_streamer.shutdown.assert_called_once()
