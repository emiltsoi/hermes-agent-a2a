"""Server-Sent Events (SSE) stream management.

SSEStreamer manages the lifecycle of SSE streams per task_id.
Each POST /tasks/sendSubscribe opens a new stream_id; events are pushed
as task state changes occur.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional


@dataclass
class SSEEvent:
    """A Server-Sent Event emitted on a task state change.

    Attributes:
        task_id:   The task this event pertains to.
        state:     The new task state (e.g. "working", "completed").
        event:     The SSE event name (e.g. "TaskWorking", "TaskCompleted").
        data:      Additional event payload (default: {}).
        id:        Optional event ID for SSE Last-Event-ID.
    """
    task_id: str
    state: str
    event: str
    data: dict = field(default_factory=dict)
    id: Optional[str] = None

    def to_sse_line(self) -> str:
        """Render the event as an SSE-formatted string: data: {...}\n\n"""
        parts = []
        if self.id:
            parts.append(f"id: {self.id}")
        parts.append(f"event: {self.event}")
        payload = {
            "taskId": self.task_id,
            "state": self.state,
            **self.data,
        }
        parts.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
        return "\n".join(parts) + "\n\n"


@dataclass
class _Stream:
    """Internal representation of an active SSE stream."""
    stream_id: str
    task_id: str
    created_at: float = field(default_factory=time.time)
    closed: bool = False
    # Pending event lines to flush when client reads
    pending: list[str] = field(default_factory=list)
    pending_lock: Lock = field(default_factory=Lock)


class SSEStreamer:
    """Manages SSE stream lifecycle per task.

    Contract:
        open_stream(task_id: str) → str          # returns stream_id
        push_event(stream_id: str, event: SSEEvent) → None
        close_stream(stream_id: str) → None

    Streams are identified by a unique stream_id (UUID).  The caller
    (typically TaskStateChangeHook) calls push_event to queue an SSE line;
    the HTTP handler reads it and streams it to the client.
    """

    def __init__(self):
        # stream_id → _Stream
        self._streams: dict[str, _Stream] = {}
        # task_id → list of stream_ids subscribed to that task
        self._by_task: dict[str, list[str]] = {}
        self._lock = Lock()

    def open_stream(self, task_id: str) -> str:
        """Open a new SSE stream for task_id.

        Returns a unique stream_id.
        """
        with self._lock:
            stream_id = str(uuid.uuid4())
            stream = _Stream(stream_id=stream_id, task_id=task_id)
            self._streams[stream_id] = stream
            self._by_task.setdefault(task_id, []).append(stream_id)
            return stream_id

    def push_event(self, stream_id: str, event: SSEEvent) -> None:
        """Push an SSE event to a stream (appends to pending buffer).

        Safe to call on a closed stream (no-op).
        """
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return
            line = event.to_sse_line()
            with stream.pending_lock:
                stream.pending.append(line)

    def close_stream(self, stream_id: str) -> None:
        """Close a stream — flushes remaining events and removes it."""
        with self._lock:
            stream = self._streams.pop(stream_id, None)
            if stream is None:
                return
            stream.closed = True
            # Remove from task index
            task_streams = self._by_task.get(stream.task_id, [])
            if stream_id in task_streams:
                task_streams.remove(stream_id)

    def get_pending(self, stream_id: str) -> list[str]:
        """Return and clear pending SSE lines for a stream (called by HTTP handler)."""
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return []
            with stream.pending_lock:
                lines = list(stream.pending)
                stream.pending.clear()
                return lines

    def is_closed(self, stream_id: str) -> bool:
        """Return True if the stream has been closed."""
        with self._lock:
            stream = self._streams.get(stream_id)
            return stream is None or stream.closed

    def get_stream_task_id(self, stream_id: str) -> Optional[str]:
        """Return the task_id for a stream, or None if not found."""
        with self._lock:
            stream = self._streams.get(stream_id)
            return stream.task_id if stream else None

    def get_stream_ids_for_task(self, task_id: str) -> list[str]:
        """Return all open stream_ids for a task."""
        with self._lock:
            return list(self._by_task.get(task_id, []))


# Module-level singleton
_streamer: Optional[SSEStreamer] = None
_streamer_lock = Lock()


def get_sse_streamer() -> SSEStreamer:
    """Return the module-level SSEStreamer singleton."""
    global _streamer
    with _streamer_lock:
        if _streamer is None:
            _streamer = SSEStreamer()
        return _streamer