"""Thread-safe singleton for A2A runtime state.

Replaces the builtins hack with a proper singleton pattern that survives
plugin reloads and provides thread-safe access to shared state.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from threading import Lock
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .server import A2AServer
    from .task_queue import TaskQueue

_logger = logging.getLogger(__name__)


class A2AMetrics:
    """Thread-safe metrics collector for A2A operations."""
    
    def __init__(self):
        self._lock = Lock()
        self._webhook_attempts = 0
        self._webhook_successes = 0
        self._webhook_failures = 0
        self._tasks_received = 0
        self._tasks_completed = 0
        self._tasks_canceled = 0
        self._tasks_failed = 0
        self._start_time = time.time()
    
    def record_webhook_attempt(self) -> None:
        with self._lock:
            self._webhook_attempts += 1
    
    def record_webhook_success(self) -> None:
        with self._lock:
            self._webhook_successes += 1
    
    def record_webhook_failure(self) -> None:
        with self._lock:
            self._webhook_failures += 1

    def record_webhook_result(self, success: bool) -> None:
        """Atomically record a webhook result (attempt + success or failure)."""
        with self._lock:
            self._webhook_attempts += 1
            if success:
                self._webhook_successes += 1
            else:
                self._webhook_failures += 1

    def record_task_received(self) -> None:
        with self._lock:
            self._tasks_received += 1
    
    def record_task_completed(self) -> None:
        with self._lock:
            self._tasks_completed += 1
    
    def record_task_canceled(self) -> None:
        with self._lock:
            self._tasks_canceled += 1
    
    def get_metrics(self) -> dict:
        with self._lock:
            uptime = time.time() - self._start_time
            webhook_success_rate = (
                self._webhook_successes / self._webhook_attempts * 100
                if self._webhook_attempts > 0 else 0
            )
            queue_depth = self._get_queue_depth()
            return {
                "uptime_seconds": round(uptime, 2),
                "webhook": {
                    "attempts": self._webhook_attempts,
                    "successes": self._webhook_successes,
                    "failures": self._webhook_failures,
                    "success_rate_percent": round(webhook_success_rate, 2),
                },
                "tasks": {
                    "received": self._tasks_received,
                    "completed": self._tasks_completed,
                    "canceled": self._tasks_canceled,
                    "failed": self._tasks_failed,
                },
                "queue": {
                    "pending_count": queue_depth,
                },
            }
    
    def _get_queue_depth(self) -> int:
        try:
            queue = get_runtime_state().get_task_queue()
            if queue is None:
                _logger.warning("[A2A Metrics] Task queue is None during queue depth lookup — state may have been cleared")
                return 0
            return queue.pending_count()
        except Exception as exc:
            _logger.warning("[A2A Metrics] Could not determine queue depth: %s", exc)
            return 0


class A2ARuntimeState:
    """Thread-safe singleton for A2A runtime state.
    
    This class manages the shared state between the A2A server thread
    and the gateway hooks. It uses a module-level singleton pattern with
    thread-safe access to prevent race conditions.
    
    The singleton survives plugin reloads because it's stored at module level.
    """
    
    _instance: Optional[A2ARuntimeState] = None
    _lock: Lock = Lock()
    
    def __new__(cls) -> A2ARuntimeState:
        """Create or return the singleton instance."""
        # Python's GIL makes double-checked locking safe here.
        # Do not add checks between lines 123 and 125 without holding cls._lock.
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the runtime state (only once)."""
        if self._initialized:
            return
        
        self._state_lock = Lock()
        self._task_queue: Optional["TaskQueue"] = None
        self._server: Optional["A2AServer"] = None
        self._thread: Optional[object] = None  # Thread object
        self._owner_module: str = __name__
        self._metrics = A2AMetrics()
        self._initialized = True
    
    def get_task_queue(self) -> "TaskQueue":
        """Get or create the task queue."""
        with self._state_lock:
            if self._task_queue is None:
                from .task_queue import TaskQueue
                self._task_queue = TaskQueue()
            return self._task_queue
    
    def get_server(self) -> Optional["A2AServer"]:
        """Get the A2A server instance."""
        with self._state_lock:
            return self._server
    
    def set_server(self, server: Optional["A2AServer"]) -> None:
        """Set the A2A server instance."""
        with self._state_lock:
            self._server = server
    
    def set_thread(self, thread: Optional[object]) -> None:
        """Set the server thread instance."""
        with self._state_lock:
            self._thread = thread
    
    def set_owner_module(self, module: str) -> None:
        """Set the owner module name."""
        with self._state_lock:
            self._owner_module = module
    
    def clear(self) -> None:
        """Clear all state (for shutdown/reload)."""
        # Stop the metrics logger before resetting state so the background
        # thread doesn't hold stale references.
        _stop_metrics_logger()
        with self._state_lock:
            self._task_queue = None
            self._server = None
            self._thread = None
            self._owner_module = __name__
            self._metrics = A2AMetrics()
    
    def to_dict(self) -> dict:
        """Export state as dict (for backward compatibility).
        
        Returns a snapshot to prevent mutation of live objects without locks.
        """
        with self._state_lock:
            return {
                "task_queue": None,  # Live TaskQueue not exposed to prevent lock bypass
                "server": self._server,
                "thread": self._thread,
                "owner_module": self._owner_module,
            }
    
    def get_metrics(self) -> A2AMetrics:
        """Get the metrics instance."""
        with self._state_lock:
            return self._metrics


# Convenience functions for backward compatibility
def get_runtime_state() -> A2ARuntimeState:
    """Get the singleton runtime state instance."""
    return A2ARuntimeState()


def clear_runtime_state() -> None:
    """Clear the runtime state."""
    _stop_metrics_logger()
    state = get_runtime_state()
    state.clear()


_metrics_logger_event: Optional[threading.Event] = None
_metrics_logger_thread: Optional[threading.Thread] = None


def _start_metrics_logger() -> None:
    """Start background thread to log metrics periodically."""
    global _metrics_logger_event, _metrics_logger_thread

    if os.getenv("A2A_METRICS_LOG_ENABLED", "false").lower() != "true":
        return

    # Stop any existing logger
    _stop_metrics_logger()

    log_interval = int(os.getenv("A2A_METRICS_LOG_INTERVAL", "300"))  # 5 minutes default
    _metrics_logger_event = threading.Event()

    def log_metrics():
        while not _metrics_logger_event.wait(log_interval):
            try:
                state = get_runtime_state()
                metrics = state.get_metrics().get_metrics()
                _logger.info("[A2A Metrics] %s", json.dumps(metrics))
            except Exception as exc:
                _logger.error("[A2A Metrics] Logger error: %s", exc)

    # NOTE: Daemon thread — final metrics may be lost on fast shutdown.
    # The logger is killed immediately on interpreter exit without completing
    # the current iteration. For graceful shutdown, use _stop_metrics_logger().
    thread = threading.Thread(target=log_metrics, daemon=True)
    thread.start()
    _metrics_logger_thread = thread
    _logger.info("[A2A Metrics] Periodic logging started (interval: %ds)", log_interval)


def _stop_metrics_logger() -> None:
    """Stop the background metrics logger if running."""
    global _metrics_logger_event, _metrics_logger_thread
    if _metrics_logger_event is not None:
        _metrics_logger_event.set()
        _metrics_logger_event = None
    if _metrics_logger_thread is not None:
        # Wait for thread to exit with a timeout to avoid blocking shutdown
        _metrics_logger_thread.join(timeout=2.0)
        _metrics_logger_thread = None
