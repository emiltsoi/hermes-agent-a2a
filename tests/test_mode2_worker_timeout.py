"""Tests for Mode 2 worker timeout enforcement — gates CRIT-01 (a2a-review-20260602).

The parent process passes a `timeout` field via stdin JSON. Before the fix,
this field was accepted and silently discarded — the worker's LLM loop was
only bounded by `AIAgent.max_iterations`, not the caller's intended
wall-clock budget. This test verifies that `_enforce_timeout` actually
enforces the budget.
"""
import signal
import time

import pytest


def test_enforce_timeout_raises_after_seconds():
    """_enforce_timeout(N) must raise TimeoutError after N seconds elapse."""
    from hermes_agent_a2a._mode2_worker import _enforce_timeout

    # Use 1 second — small enough to not slow the suite, large enough to be reliable.
    _enforce_timeout(1)

    start = time.time()
    with pytest.raises(TimeoutError, match="wall-clock budget"):
        # Sleep long enough to let the alarm fire.
        time.sleep(3)
    elapsed = time.time() - start

    # Should fire between 1s and 2.5s (allow scheduler slack)
    assert 0.9 <= elapsed <= 2.5, f"Timeout fired at unexpected time: {elapsed:.2f}s"


def test_enforce_timeout_does_not_fire_before_budget():
    """If the budget is large, work within it should not be interrupted."""
    from hermes_agent_a2a._mode2_worker import _enforce_timeout

    # Set a 5s budget, do 0.5s of work, then disable the alarm. Should not raise.
    _enforce_timeout(5)

    start = time.time()
    time.sleep(0.5)
    # Disable the alarm — work is done.
    signal.alarm(0)

    elapsed = time.time() - start
    # Should complete in ~0.5s without raising
    assert elapsed < 2.0, f"Work interrupted unexpectedly: {elapsed:.2f}s"


def test_enforce_timeout_replaces_previous_alarm():
    """Calling _enforce_timeout twice must use the latest budget, not accumulate."""
    from hermes_agent_a2a._mode2_worker import _enforce_timeout

    # First budget: 100s (effectively infinite for this test)
    _enforce_timeout(100)
    # Second budget: 1s (overrides the first)
    _enforce_timeout(1)

    start = time.time()
    with pytest.raises(TimeoutError, match="wall-clock budget"):
        time.sleep(3)
    elapsed = time.time() - start

    # Should fire around 1s, not 100s
    assert 0.9 <= elapsed <= 2.5, (
        f"Timeout did not override previous alarm: fired at {elapsed:.2f}s"
    )
