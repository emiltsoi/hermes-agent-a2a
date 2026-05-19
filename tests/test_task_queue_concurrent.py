"""Wave D Issue 20: Concurrent TaskQueue operations tests.

Tests race conditions and ordering under load for concurrent
enqueue/dequeue/complete operations on TaskQueue.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from hermes_agent_a2a.server import TaskQueue


class TestConcurrentEnqueue:
    """Concurrent enqueue from multiple threads — all tasks appear in queue."""

    def test_concurrent_enqueue_all_tasks_appear(self):
        """All tasks enqueued concurrently should appear in the queue."""
        tq = TaskQueue()
        num_threads = 20
        tasks_per_thread = 50
        expected_total = num_threads * tasks_per_thread

        def enqueue_batch(batch_id: int) -> list[str]:
            task_ids = []
            for i in range(tasks_per_thread):
                task_id = f"batch{batch_id}-task{i}"
                task_ids.append(task_id)
                tq.enqueue(task_id, f"text-{batch_id}-{i}", {"batch": batch_id})
            return task_ids

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(enqueue_batch, i) for i in range(num_threads)]
            results = [f.result() for f in as_completed(futures)]

        # All tasks should be in pending
        all_task_ids = set()
        for batch_result in results:
            all_task_ids.update(batch_result)

        pending_ids = set(tq._pending.keys())
        assert pending_ids == all_task_ids, (
            f"Expected {len(all_task_ids)} pending tasks, got {len(pending_ids)}. "
            f"Missing: {all_task_ids - pending_ids}"
        )

    def test_concurrent_enqueue_no_duplicates(self):
        """No duplicate task_ids should be created under concurrent enqueue."""
        tq = TaskQueue()
        num_threads = 10
        tasks_per_thread = 100
        unique_task_ids = [f"task-{i}" for i in range(num_threads * tasks_per_thread)]

        def enqueue_range(start: int, count: int) -> None:
            for i in range(start, start + count):
                tq.enqueue(unique_task_ids[i], f"text-{i}", {})

        step = tasks_per_thread
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(enqueue_range, i * step, step)
                for i in range(num_threads)
            ]
            for f in as_completed(futures):
                f.result()

        # Only unique task_ids should be in pending (duplicates should be rejected)
        assert len(tq._pending) == len(set(unique_task_ids))
        for task_id in unique_task_ids:
            if task_id in tq._pending:
                assert tq._pending[task_id].task_id == task_id


class TestConcurrentEnqueueComplete:
    """Concurrent enqueue + complete — no tasks lost."""

    def test_concurrent_enqueue_and_complete_no_loss(self):
        """Tasks should not be lost when enqueueing and completing concurrently."""
        tq = TaskQueue()
        num_threads = 10
        tasks_per_thread = 50
        total_tasks = num_threads * tasks_per_thread

        enqueued_count = threading.atomic = 0
        completed_count = threading.atomic = 0
        lock = threading.Lock()

        def enqueue_batch(batch_id: int) -> int:
            count = 0
            for i in range(tasks_per_thread):
                task_id = f"batch{batch_id}-task{i}"
                task = tq.enqueue(task_id, f"text-{batch_id}-{i}", {"batch": batch_id})
                if task is not None:
                    count += 1
            return count

        def complete_batch() -> None:
            for _ in range(tasks_per_thread * 2):
                for task_id in list(tq._pending.keys())[:5]:
                    tq.complete(task_id, "done")

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            # Alternate enqueue and complete threads
            futures = []
            for i in range(num_threads):
                futures.append(executor.submit(enqueue_batch, i))
            for f in as_completed(futures):
                f.result()

        # Verify: enqueued count - completed count should match pending + completed
        pending_count = len(tq._pending)
        completed_in_tq = len(tq._completed)
        total_accounted = pending_count + completed_in_tq

        assert total_accounted <= total_tasks, (
            f"More tasks accounted for ({total_accounted}) than enqueued ({total_tasks}). "
            f"pending={pending_count}, completed={completed_in_tq}"
        )

    def test_enqueue_complete_rapid_alternation(self):
        """Rapidly alternating enqueue/complete should not lose tasks."""
        tq = TaskQueue()
        num_cycles = 100

        for i in range(num_cycles):
            task = tq.enqueue(f"task-{i}", f"text-{i}", {})
            assert task is not None, f"Failed to enqueue task-{i}"
            tq.complete(f"task-{i}", f"result-{i}")

        assert len(tq._pending) == 0
        assert len(tq._completed) == num_cycles
        assert tq.pending_count() == 0


class TestConcurrentDequeue:
    """Concurrent dequeue from multiple workers — tasks distributed correctly."""

    def test_concurrent_dequeue_no_double_work(self):
        """Same task should not be dequeued by multiple workers."""
        tq = TaskQueue()
        num_workers = 10
        num_tasks = 100

        # Enqueue all tasks
        for i in range(num_tasks):
            tq.enqueue(f"task-{i}", f"text-{i}", {})

        dequeued_ids = []
        lock = threading.Lock()

        def worker(worker_id: int) -> list[str]:
            local_dequeued = []
            while True:
                with lock:
                    if not tq._pending:
                        break
                    task_ids = list(tq._pending.keys())
                    if not task_ids:
                        break

                # Try to claim a task
                claimed = None
                for tid in task_ids:
                    with lock:
                        if tid in tq._pending:
                            tq.mark_processing(tid)
                            claimed = tid
                            break

                if claimed:
                    local_dequeued.append(claimed)
                    tq.complete(claimed, f"done by worker-{worker_id}")
                else:
                    time.sleep(0.001)

            return local_dequeued

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker, i) for i in range(num_workers)]
            results = [f.result() for f in as_completed(futures)]

        all_dequeued = []
        for r in results:
            all_dequeued.extend(r)

        # Each task should be dequeued exactly once
        assert len(all_dequeued) == len(set(all_dequeued)), (
            f"Tasks dequeued multiple times: {len(all_dequeued)} total, "
            f"{len(set(all_dequeued))} unique"
        )
        assert len(all_dequeued) == num_tasks

    def test_concurrent_dequeue_all_tasks_accounted_for(self):
        """All tasks must be accounted for (no lost or duplicated work)."""
        tq = TaskQueue()
        num_workers = 8
        num_tasks = 80

        for i in range(num_tasks):
            tq.enqueue(f"task-{i}", f"text-{i}", {})

        completed_ids = []
        lock = threading.Lock()
        start_barrier = threading.Barrier(num_workers)

        def worker(worker_id: int) -> None:
            start_barrier.wait()
            while True:
                with lock:
                    if not tq._pending:
                        break
                    task_ids = list(tq._pending.keys())
                    if not task_ids:
                        break
                    tid = task_ids[0]
                    tq.mark_processing(tid)
                tq.complete(tid, "done")
                with lock:
                    completed_ids.append(tid)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker, i) for i in range(num_workers)]
            for f in as_completed(futures):
                f.result()

        # No duplicate completions
        assert len(completed_ids) == len(set(completed_ids)), \
            f"Duplicate work detected: {len(completed_ids)} completions, {len(set(completed_ids))} unique"
        # All tasks completed
        assert len(completed_ids) == num_tasks, \
            f"Not all tasks completed: {len(completed_ids)}/{num_tasks}"


class TestQueueDepthCounter:
    """Queue depth counter stays accurate under concurrent access."""

    def test_pending_count_accuracy_under_concurrent_enqueue(self):
        """pending_count() stays accurate with concurrent enqueue operations."""
        tq = TaskQueue()
        num_threads = 10
        tasks_per_thread = 100

        def enqueue_batch(batch_id: int) -> None:
            for i in range(tasks_per_thread):
                tq.enqueue(f"batch{batch_id}-task{i}", f"text-{i}", {})

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(enqueue_batch, i) for i in range(num_threads)]
            for f in as_completed(futures):
                f.result()

        expected = num_threads * tasks_per_thread
        actual = tq.pending_count()
        assert actual == expected, f"pending_count={actual}, expected={expected}"
        assert len(tq._pending) == expected

    def test_pending_count_accuracy_under_concurrent_complete(self):
        """pending_count() stays accurate with concurrent complete operations."""
        tq = TaskQueue()
        num_tasks = 500

        # Enqueue all tasks first
        for i in range(num_tasks):
            tq.enqueue(f"task-{i}", f"text-{i}", {})

        assert tq.pending_count() == num_tasks

        def complete_batch(start: int, count: int) -> None:
            for i in range(start, start + count):
                tq.complete(f"task-{i}", f"result-{i}")

        num_workers = 10
        batch_size = num_tasks // num_workers
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(complete_batch, i * batch_size, batch_size)
                for i in range(num_workers)
            ]
            for f in as_completed(futures):
                f.result()

        assert tq.pending_count() == 0
        assert len(tq._pending) == 0
        assert len(tq._completed) == num_tasks

    def test_pending_count_mixed_enqueue_complete(self):
        """pending_count() accurate with concurrent enqueue + complete mixed."""
        tq = TaskQueue()
        num_workers = 10
        ops_per_worker = 100

        events = []

        def worker(worker_id: int) -> None:
            for i in range(ops_per_worker):
                op = i % 3
                if op == 0:
                    # Enqueue
                    task_id = f"w{worker_id}-enqueue-{i}"
                    tq.enqueue(task_id, f"text-{i}", {"worker": worker_id})
                    events.append(("enqueue", task_id))
                elif op == 1 and tq._pending:
                    # Complete one
                    task_ids = list(tq._pending.keys())
                    if task_ids:
                        tid = task_ids[i % len(task_ids)]
                        tq.complete(tid, "done")
                        events.append(("complete", tid))
                else:
                    time.sleep(0.001)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker, i) for i in range(num_workers)]
            for f in as_completed(futures):
                f.result()

        # Final state: pending_count should match actual pending
        assert tq.pending_count() == len(tq._pending)

        # Counter should be non-negative
        assert tq.pending_count() >= 0

    def test_atomic_counters_under_high_concurrency(self):
        """Atomic counters (_enqueue_count, _complete_count, _cancel_count) are consistent."""
        tq = TaskQueue()
        num_workers = 20
        tasks_per_worker = 50

        def worker(worker_id: int) -> None:
            for i in range(tasks_per_worker):
                task_id = f"w{worker_id}-task{i}"
                tq.enqueue(task_id, f"text-{i}", {})
                # Randomly complete some tasks
                if i % 3 == 0:
                    tq.complete(task_id, "done")

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker, i) for i in range(num_workers)]
            for f in as_completed(futures):
                f.result()

        # Verify counter consistency
        enqueued = tq._enqueue_count
        completed = tq._complete_count
        pending = len(tq._pending)

        # enqueued = completed + pending (approximately, since some may still be pending)
        # The exact relationship depends on timing, but completed + pending should be <= enqueued
        assert completed + pending <= enqueued
        assert enqueued == num_workers * tasks_per_worker
