"""Thread-safe task queue and pending-task model for the A2A server.

Extracted from ``server.py`` to decouple the inbound-task state machine from
the HTTP transport. ``server.py`` re-exports these names for backward
compatibility.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from threading import Event, Lock
from typing import Optional

from .security import filter_outbound

logger = logging.getLogger(__name__)

_TASK_CACHE_MAX = 100000
_MAX_PENDING = 100000


class _PendingTask:
    __slots__ = ("task_id", "text", "metadata", "response", "ready", "created_at", "context_id", "_returned")

    def __init__(self, task_id: str, text: str, metadata: dict, context_id: Optional[str] = None):
        self.task_id = task_id
        self.text = text
        self.metadata = metadata
        self.response: Optional[str] = None
        self.ready = Event()
        self.created_at = time.time()
        self.context_id = context_id
        self._returned = False  # True once A2A server has returned to caller


class TaskQueue:
    """Thread-safe queue for pending A2A tasks with full state machine.

    States:
      submitted     — task received, not yet processing
      working       — actively being processed
      auth_required — waiting for authentication
      authenticated — auth confirmed, waiting to become working
      completed     — task done, response available
      failed        — task failed
      canceled      — task canceled
      rejected      — task rejected (auth or policy)

    Valid transitions:
      submitted       → working
      auth_required   → authenticated, rejected
      authenticated    → working
      working          → completed, failed, canceled
    """

    # State machine definition: from_state → set of allowed to_states
    _TRANSITIONS: dict[str, set[str]] = {
        "submitted":     {"working", "completed", "failed", "canceled"},
        "working":       {"completed", "failed", "canceled"},
        "auth_required": {"authenticated", "rejected"},
        "authenticated": {"working", "failed"},
        "completed":     set(),
        "failed":        set(),
        "canceled":      set(),
        "rejected":      set(),
    }

    def __init__(self):
        self._pending: OrderedDict[str, _PendingTask] = OrderedDict()
        self._completed: OrderedDict[str, _PendingTask] = OrderedDict()
        self._processing: set[str] = set()
        self._lock = Lock()
        # Atomic counters — eliminate re-entrancy risk in _get_queue_depth
        self._enqueue_count = 0
        self._complete_count = 0
        self._cancel_count = 0
        # State machine: task_id → current state
        self._states: dict[str, str] = {}
        # Oldest pending task created_at — used to skip watchdog scan when queue is young
        self._oldest_pending_time: Optional[float] = None

    def set_auth_required(self, task_id: str, metadata: dict) -> None:
        """Place a task in auth_required state without queuing it."""
        with self._lock:
            self._states[task_id] = "auth_required"

    def set_authenticated(self, task_id: str, metadata: dict) -> None:
        """Mark a task as authenticated (from auth_required)."""
        with self._lock:
            self._states[task_id] = "authenticated"

    def transition(self, task_id: str, to_state: str, return_error: bool = False) -> bool | tuple[bool, int | None]:
        """Attempt a state transition.

        Args:
            task_id: The task to transition.
            to_state: The target state.
            return_error: If True, return (success, error_code) on failure.

        Returns:
            True if transition succeeded.
            If return_error=True, returns (False, error_code) on failure.
            If return_error=False, returns False on failure.
        """
        with self._lock:
            from_state = self._states.get(task_id, "submitted")
            allowed = self._TRANSITIONS.get(from_state, set())
            if to_state in allowed:
                self._states[task_id] = to_state
                if return_error:
                    return True, None
                return True
            if return_error:
                return False, -38003  # Invalid state transition
            return False

    def pending_count(self) -> int:
        # Counter-based — no re-entrancy risk, no traversal of singleton
        with self._lock:
            return max(0, self._enqueue_count - self._complete_count - self._cancel_count)

    def enqueue(self, task_id: str, text: str, metadata: dict, context_id: Optional[str] = None) -> _PendingTask | None:
        task = _PendingTask(task_id, text, metadata, context_id=context_id)
        with self._lock:
            if len(self._pending) >= _MAX_PENDING:
                return None
            if task_id in self._pending:
                return None
            self._pending[task_id] = task
            self._states[task_id] = "submitted"
            # Track oldest pending time for watchdog optimization
            if self._oldest_pending_time is None:
                self._oldest_pending_time = task.created_at
            else:
                self._oldest_pending_time = min(self._oldest_pending_time, task.created_at)
            # Only evict tasks that are not currently being processed to avoid race
            evicted = 0
            while len(self._pending) > _TASK_CACHE_MAX:
                for tid, old_task in list(self._pending.items()):
                    if tid not in self._processing:
                        self._pending.pop(tid)
                        old_task.response = "(dropped — queue overflow)"
                        old_task.ready.set()
                        evicted += 1
                        # If evicted task was the oldest, recalculate
                        if old_task.created_at == self._oldest_pending_time and self._oldest_pending_time is not None:
                            self._oldest_pending_time = min((t.created_at for t in self._pending.values()), default=None)
                        break
                else:
                    # All pending tasks are being processed, stop evicting
                    break
            # Increment counter for successfully enqueued (non-evicted) tasks only
            self._enqueue_count += 1 - evicted
        # Record task received metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_received()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_received): %s", exc)
        return task

    def drain_pending(self, exclude: set[str] | None = None) -> list[_PendingTask]:
        with self._lock:
            skip = set(exclude or ()) | self._processing
            return [t for t in self._pending.values() if t.task_id not in skip]

    def requeue_tasks(self, tasks: list[_PendingTask]) -> None:
        """Re-queue tasks that were drained but not processed."""
        with self._lock:
            for task in tasks:
                if task.task_id not in self._pending and task.task_id not in self._processing:
                    self._pending[task.task_id] = task
                    self._enqueue_count += 1

    def mark_processing(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._pending:
                self._processing.add(task_id)

    def complete(self, task_id: str, response: str) -> None:
        with self._lock:
            self._processing.discard(task_id)
            task = self._pending.pop(task_id, None)
            if task:
                # Only set response if we haven't already returned to the caller.
                # Late completions after timeout are logged but don't overwrite
                # the response already delivered to the client.
                if not task._returned:
                    task.response = response
                    task.ready.set()
                self._completed[task_id] = task
                self._complete_count += 1
                self._states[task_id] = "completed"
                # Update _oldest_pending_time if we removed the oldest task
                if task.created_at == self._oldest_pending_time:
                    self._oldest_pending_time = min((t.created_at for t in self._pending.values()), default=None)
                while len(self._completed) > _TASK_CACHE_MAX:
                    self._completed.popitem(last=False)
        # Record task completed metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_completed()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_completed): %s", exc)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            self._processing.discard(task_id)
            task = self._pending.pop(task_id, None)
            if task:
                task.response = "(canceled)"
                task.ready.set()
                self._completed[task_id] = task
                self._cancel_count += 1
                self._states[task_id] = "canceled"
                # Update _oldest_pending_time if we removed the oldest task
                if task.created_at == self._oldest_pending_time:
                    self._oldest_pending_time = min((t.created_at for t in self._pending.values()), default=None)
        # Record task canceled metric
        try:
            from .runtime_state import get_runtime_state as get_state
            get_state().get_metrics().record_task_canceled()
        except Exception as exc:
            logger.debug("TaskQueue: metrics unavailable (record_task_canceled): %s", exc)

    def get_status(self, task_id: str) -> dict:
        with self._lock:
            if task_id in self._pending:
                return {"state": self._states.get(task_id, "working")}
            task = self._completed.get(task_id)
            if task:
                if task.response == "(canceled)":
                    return {"state": "canceled"}
                if task_id in self._states:
                    return {"state": self._states[task_id]}
                return {"state": "completed", "response": filter_outbound(task.response)}
            # Check pre-queue auth/rejected states
            if task_id in self._states:
                return {"state": self._states[task_id]}
        return {"state": "unknown"}

    def get_task_metadata(self, task_id: str) -> dict:
        """Get metadata for a task by ID (public API for hooks)."""
        with self._lock:
            task = self._pending.get(task_id) or self._completed.get(task_id)
            return getattr(task, "metadata", {}) if task else {}

    def get_all_task_metadata(self) -> dict[str, dict]:
        """Get metadata for all pending and completed tasks (public API for hooks)."""
        with self._lock:
            result = {}
            for task in list(self._pending.values()) + list(self._completed.values()):
                result[task.task_id] = getattr(task, "metadata", {})
            return result

    def get_processing_tasks(self) -> list[str]:
        """Get list of currently processing task IDs (public API for hooks)."""
        with self._lock:
            return list(self._processing)

    def find_task_by_id(self, task_id: str) -> Optional[_PendingTask]:
        """Find a task by ID across pending and completed (public API for hooks)."""
        with self._lock:
            task = self._pending.get(task_id)
            if task is not None:
                return task
            return self._completed.get(task_id)

    def snapshot(self) -> list[tuple[float, _PendingTask, str]]:
        """Return a consistent snapshot of all tasks as (created_at, task, state).

        Taken under the queue lock so callers (e.g. paginated ListTasks) don't
        reach into the queue internals directly.
        """
        with self._lock:
            return [
                (task.created_at, task, self._states.get(task.task_id, "unknown"))
                for task in list(self._pending.values()) + list(self._completed.values())
            ]
