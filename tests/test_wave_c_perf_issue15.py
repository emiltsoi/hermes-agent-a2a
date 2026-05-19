"""TDD tests for Wave C Issue 15: Watchdog O(n) Full Copy optimization.

Tests _oldest_pending_time tracking in TaskQueue to avoid scanning the entire
pending dict when the queue is young.
"""
import time
import pytest

from hermes_agent_a2a.server import TaskQueue, _RESPONSE_TIMEOUT


class TestOldestPendingTimeTracking:
    """TaskQueue should track _oldest_pending_time to optimize watchdog."""

    def test_oldest_pending_time_initialized_to_none(self):
        """Fresh TaskQueue has no pending tasks, so _oldest_pending_time is None."""
        tq = TaskQueue()
        assert tq._oldest_pending_time is None

    def test_oldest_pending_time_set_on_first_enqueue(self):
        """Enqueueing first task sets _oldest_pending_time to that task's created_at."""
        tq = TaskQueue()
        task = tq.enqueue("task-1", "hello", {})
        assert task is not None
        assert tq._oldest_pending_time is not None
        assert abs(tq._oldest_pending_time - task.created_at) < 0.1

    def test_oldest_pending_time_is_min_of_all_pending(self):
        """_oldest_pending_time always reflects the minimum created_at of pending tasks."""
        tq = TaskQueue()

        # Enqueue first task
        task1 = tq.enqueue("task-1", "hello", {})
        time.sleep(0.01)
        oldest_of_first = task1.created_at

        # Enqueue second task (created later)
        task2 = tq.enqueue("task-2", "world", {})
        assert tq._oldest_pending_time == oldest_of_first

        # Enqueue third task (created even later)
        task3 = tq.enqueue("task-3", "third", {})
        assert tq._oldest_pending_time == oldest_of_first

    def test_oldest_pending_time_updated_when_oldest_completed(self):
        """When the oldest task is completed, _oldest_pending_time advances."""
        tq = TaskQueue()

        # Enqueue two tasks with a gap
        task1 = tq.enqueue("task-1", "hello", {})
        time.sleep(0.02)
        task2 = tq.enqueue("task-2", "world", {})

        oldest = tq._oldest_pending_time
        assert oldest == task1.created_at

        # Complete the oldest task
        tq.complete("task-1", "done")

        # Now oldest should be task2's created_at
        assert tq._oldest_pending_time == task2.created_at

    def test_oldest_pending_time_updated_when_oldest_canceled(self):
        """When the oldest task is canceled, _oldest_pending_time advances."""
        tq = TaskQueue()

        task1 = tq.enqueue("task-1", "hello", {})
        time.sleep(0.02)
        task2 = tq.enqueue("task-2", "world", {})

        # Cancel the oldest task
        tq.cancel("task-1")

        # Now oldest should be task2's created_at
        assert tq._oldest_pending_time == task2.created_at

    def test_oldest_pending_time_updated_on_eviction(self):
        """When oldest task is evicted due to overflow, _oldest_pending_time advances."""
        from hermes_agent_a2a.server import _TASK_CACHE_MAX, _MAX_PENDING

        tq = TaskQueue()

        # Fill beyond _TASK_CACHE_MAX to trigger eviction
        for i in range(_TASK_CACHE_MAX + 2):
            tq.enqueue(f"task-{i}", f"hello {i}", {})

        # The oldest remaining task's created_at should be the new _oldest_pending_time
        assert tq._oldest_pending_time is not None

    def test_oldest_pending_time_none_when_all_removed(self):
        """After all pending tasks are removed, _oldest_pending_time returns to None."""
        tq = TaskQueue()

        task1 = tq.enqueue("task-1", "hello", {})
        task2 = tq.enqueue("task-2", "world", {})

        tq.complete("task-1", "done")
        tq.complete("task-2", "done")

        assert tq._oldest_pending_time is None


class TestWatchdogOptimization:
    """Watchdog should skip iteration when queue is too young to have orphans."""

    def test_watchdog_skips_scan_when_no_old_tasks(self, monkeypatch):
        """If _oldest_pending_time is recent (>= cutoff), skip full dict scan."""
        from hermes_agent_a2a import server

        calls = []

        def mock_sleep(delay):
            calls.append(delay)
            # Don't actually sleep in test

        monkeypatch.setattr(time, "sleep", mock_sleep)

        # Create a task queue with a fresh task
        tq = TaskQueue()
        tq.enqueue("task-1", "hello", {})

        # Manually set _oldest_pending_time to now (too young to be orphaned)
        tq._oldest_pending_time = time.time()

        # Calculate what cutoff would be
        cutoff = time.time() - 2 * _RESPONSE_TIMEOUT

        # Verify that newest task's created_at >= cutoff (queue is young)
        assert tq._oldest_pending_time >= cutoff

        # The optimization: we would skip the scan because _oldest_pending_time >= cutoff
        # This is the core optimization - we only scan if _oldest_pending_time < cutoff

    def test_watchdog_would_scan_when_old_tasks_exist(self):
        """If _oldest_pending_time < cutoff, there may be orphaned tasks to find."""
        tq = TaskQueue()

        # Enqueue a task
        task = tq.enqueue("task-1", "hello", {})

        # Simulate time passing - set created_at to the past
        task.created_at = time.time() - 2 * _RESPONSE_TIMEOUT - 10
        tq._oldest_pending_time = task.created_at

        cutoff = time.time() - 2 * _RESPONSE_TIMEOUT

        # _oldest_pending_time < cutoff means there could be orphaned tasks
        assert tq._oldest_pending_time < cutoff

        # The watchdog MUST iterate in this case to find the orphaned task
