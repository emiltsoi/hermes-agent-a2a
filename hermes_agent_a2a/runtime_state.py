"""Thread-safe singleton for A2A runtime state.

Replaces the builtins hack with a proper singleton pattern that survives
plugin reloads and provides thread-safe access to shared state.
"""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .server import A2AServer, TaskQueue


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
        self._initialized = True
    
    def get_task_queue(self) -> "TaskQueue":
        """Get or create the task queue."""
        with self._state_lock:
            if self._task_queue is None:
                from .server import TaskQueue
                self._task_queue = TaskQueue()
            return self._task_queue
    
    def set_task_queue(self, queue: "TaskQueue") -> None:
        """Set the task queue."""
        with self._state_lock:
            self._task_queue = queue
    
    def get_server(self) -> Optional["A2AServer"]:
        """Get the A2A server instance."""
        with self._state_lock:
            return self._server
    
    def set_server(self, server: Optional["A2AServer"]) -> None:
        """Set the A2A server instance."""
        with self._state_lock:
            self._server = server
    
    def get_thread(self) -> Optional[object]:
        """Get the server thread instance."""
        with self._state_lock:
            return self._thread
    
    def set_thread(self, thread: Optional[object]) -> None:
        """Set the server thread instance."""
        with self._state_lock:
            self._thread = thread
    
    def get_owner_module(self) -> str:
        """Get the owner module name."""
        with self._state_lock:
            return self._owner_module
    
    def set_owner_module(self, module: str) -> None:
        """Set the owner module name."""
        with self._state_lock:
            self._owner_module = module
    
    def clear(self) -> None:
        """Clear all state (for shutdown/reload)."""
        with self._state_lock:
            self._task_queue = None
            self._server = None
            self._thread = None
            self._owner_module = __name__
    
    def to_dict(self) -> dict:
        """Export state as dict (for backward compatibility)."""
        with self._state_lock:
            return {
                "task_queue": self._task_queue,
                "server": self._server,
                "thread": self._thread,
                "owner_module": self._owner_module,
            }


# Convenience functions for backward compatibility
def get_runtime_state() -> A2ARuntimeState:
    """Get the singleton runtime state instance."""
    return A2ARuntimeState()


def clear_runtime_state() -> None:
    """Clear the runtime state."""
    state = get_runtime_state()
    state.clear()
