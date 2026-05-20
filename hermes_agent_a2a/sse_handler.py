"""Server-Sent Events (SSE) stream management.

SSEStreamer manages the lifecycle of SSE streams per task_id.
Each POST /tasks/sendSubscribe opens a new stream_id; events are pushed
as task state changes occur.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
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
        context_id: The context ID (required per Google A2A spec TaskStatusUpdateEvent).
        kind:      Event kind — "status" (default, TaskStatusUpdateEvent) or
                   "artifact" (TaskArtifactUpdateEvent).
        artifact:  Artifact data dict (only used when kind="artifact").
        metadata:  Optional metadata dict (only used when kind="artifact").
    """
    task_id: str
    state: str
    event: str
    data: dict = field(default_factory=dict)
    id: Optional[str] = None
    context_id: Optional[str] = None
    kind: str = "status"  # "status" or "artifact"
    artifact: Optional[dict] = None
    metadata: Optional[dict] = None

    def to_sse_line(self) -> str:
        """Render the event as an SSE-formatted string: data: {...}\n\n

        For kind="status" (default): emits the Google A2A TaskStatusUpdateEvent spec structure:
        {
          "taskId": "...",
          "contextId": "...",       // REQUIRED per spec
          "status": {               // state nested inside status
            "state": "...",
            "message": {...},        // from data.get("message", {})
            "timestamp": "ISO-8601"
          },
          "metadata": {...}
        }

        For kind="artifact": emits the TaskArtifactUpdateEvent structure:
        {
          "kind": "artifact",
          "contextId": "...",
          "taskId": "...",
          "artifact": {...},
          "metadata": {...}
        }
        """
        parts = []
        if self.id:
            parts.append(f"id: {self.id}")
        parts.append(f"event: {self.event}")

        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        if self.kind == "artifact":
            payload = {
                "contextId": self.context_id or self.task_id,
                "taskId": self.task_id,
                "artifact": self.artifact or {},
                "metadata": self.metadata or {},
            }
        else:
            # TaskStatusUpdateEvent format (existing)
            message = self.data.get("message", {"role": "agent", "parts": []})
            payload = {
                "taskId": self.task_id,
                "contextId": self.context_id or self.task_id,
                "status": {
                    "state": self.state,
                    "message": message,
                    "timestamp": timestamp,
                },
                "metadata": self.data.get("metadata", {}),
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
    # Tracks when the stream was last active (last event push or read)
    idle_since: float = field(default_factory=time.time)
    # Separate sentinel for idle-timeout decisions — updated on both push and read,
    # so _close_idle_streams sees a stable snapshot and is not fooled by a
    # get_pending call that happens right before cleanup runs.
    _last_activity: float = field(default_factory=time.time)
    # Pending event lines to flush when client reads
    pending: list[str] = field(default_factory=list)
    pending_lock: Lock = field(default_factory=Lock)
    # Monotonically increasing sequence counter for Last-Event-ID
    sequence_counter: int = 0
    # The last SSE event ID sent on this stream (per SSE spec)
    last_id: Optional[str] = None


class SSEStreamer:
    """Manages SSE stream lifecycle per task.

    Contract:
        open_stream(task_id: str) → str          # returns stream_id
        push_event(stream_id: str, event: SSEEvent) → None
        close_stream(stream_id: str) → None

    Streams are identified by a unique stream_id (UUID).  The caller
    (typically TaskStateChangeHook) calls push_event to queue an SSE line;
    the HTTP handler reads it and streams it to the client.

    A background cleanup thread runs every 60 seconds and closes any streams
    that have been idle longer than idle_timeout (default: 300 s).  The thread
    is lazy-initialised on first streamer use, is a daemon thread, and handles
    exceptions gracefully.
    """

    def __init__(self, idle_timeout: float = 300.0, cleanup_interval: float = 60.0):
        # stream_id → _Stream
        self._streams: dict[str, _Stream] = {}
        # task_id → list of stream_ids subscribed to that task
        self._by_task: dict[str, list[str]] = {}
        self._lock = Lock()
        self._idle_timeout = idle_timeout
        self._cleanup_interval = cleanup_interval
        self._cleanup_thread: Optional[Thread] = None
        self._started = False
        self._shutdown_event = Event()

    def open_stream(self, task_id: str) -> str:
        """Open a new SSE stream for task_id.

        Returns a unique stream_id.  Lazily starts the background cleanup
        thread on first use.
        """
        self._ensure_cleanup_thread()
        with self._lock:
            stream_id = str(uuid.uuid4())
            stream = _Stream(stream_id=stream_id, task_id=task_id)
            self._streams[stream_id] = stream
            self._by_task.setdefault(task_id, []).append(stream_id)
            return stream_id

    def push_event(self, stream_id: str, event: SSEEvent) -> None:
        """Push an SSE event to a stream (appends to pending buffer).

        Safe to call on a closed stream (no-op).  Resets idle_since so an
        active stream is not closed by the cleanup thread.

        If event.id is not already set, assigns a monotonically increasing
        SSE event ID of the form ``task_id + "_" + str(sequence_number)``.
        Tracks last_id per stream for Last-Event-ID resumption support.
        """
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return
            stream._last_activity = time.time()
            # Assign SSE event ID if not already set (SSE spec: id must be monotonic)
            if event.id is None:
                stream.sequence_counter += 1
                event.id = f"{stream.task_id}_{stream.sequence_counter}"
            stream.last_id = event.id
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
        """Return and clear pending SSE lines for a stream (called by HTTP handler).

        Does NOT update _last_activity — only push_event (server-side activity) does.
        This ensures a stream with no pending events times out even if a client
        is actively polling it.
        """
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return []
            with stream.pending_lock:
                lines = list(stream.pending)
                stream.pending.clear()
                return lines

    def get_last_event_id(self, stream_id: str) -> Optional[str]:
        """Return the last SSE event ID sent on a stream, or None if no events sent."""
        with self._lock:
            stream = self._streams.get(stream_id)
            return stream.last_id if stream else None

    def get_pending_after_id(self, stream_id: str, after_id: str) -> list[str]:
        """Return pending SSE lines for a stream with id strictly greater than after_id.

        Used for SSE stream resumption: when a client reconnects with a Last-Event-ID
        header, call this to fetch only events emitted after that point.

        Does NOT clear the returned lines — callers must also call get_pending()
        to consume them after replay.

        Returns events whose SSE id field (e.g. ``task_id_N``) is lexicographically
        greater than after_id.
        """
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return []
            with stream.pending_lock:
                # Filter to lines with an id: field lexicographically after after_id.
                # The SSE line format is: "id: <event_id>\n..."  — we scan all pending
                # lines and keep those whose id value > after_id.
                result = []
                for line in stream.pending:
                    if line.startswith("id: "):
                        # Extract the id value (up to the newline)
                        line_id = line.split("id: ", 1)[1].split("\n")[0].strip()
                        if line_id > after_id:
                            result.append(line)
                    else:
                        # Lines without an id field are always replayed
                        result.append(line)
                return result

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

    # ------------------------------------------------------------------
    # Background idle-timeout cleanup
    # ------------------------------------------------------------------

    def _ensure_cleanup_thread(self) -> None:
        """Lazily start the background cleanup thread on first streamer use."""
        if self._started:
            return
        self._started = True
        self._cleanup_thread = Thread(target=self._cleanup_loop, name="sse-idle-cleanup", daemon=True)
        self._cleanup_thread.start()

    def shutdown(self) -> None:
        """Signal the cleanup thread to exit and block until it has joined.

        Safe to call multiple times. After shutdown() the streamer cannot be
        reused — open_stream() will start a new cleanup thread.
        """
        if not self._started:
            return
        self._shutdown_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5.0)

    def _cleanup_loop(self) -> None:
        """Run every cleanup_interval seconds, closing streams idle > idle_timeout.

        Exceptions are caught and logged so the thread runs indefinitely as a
        daemon until the process exits or shutdown() is called.
        """
        logger = logging.getLogger(__name__)
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=self._cleanup_interval)
            if self._shutdown_event.is_set():
                break
            try:
                self._close_idle_streams(logger)
            except Exception:
                logger.exception("sse_handler: idle cleanup iteration failed")

    def _close_idle_streams(self, logger: logging.Logger) -> None:
        """Find and close all streams idle longer than idle_timeout."""
        now = time.time()
        with self._lock:
            for stream_id in list(self._streams.keys()):
                stream = self._streams[stream_id]
                if stream.closed:
                    continue
                idle = now - stream._last_activity
                if idle > self._idle_timeout:
                    self._streams.pop(stream_id)
                    # Remove from task index
                    task_streams = self._by_task.get(stream.task_id, [])
                    if stream_id in task_streams:
                        task_streams.remove(stream_id)
                    logger.warning(
                        "sse_handler: closing idle stream_id=%s task_id=%s "
                        "(idle %.1f s > %.0f s)",
                        stream_id, stream.task_id,
                        idle, self._idle_timeout,
                    )


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


def emit_artifact_event(
    task_id: str,
    context_id: str,
    artifact: dict,
    metadata: Optional[dict] = None,
) -> SSEEvent:
    """Construct and return an SSEEvent for a TaskArtifactUpdateEvent.

    The returned event uses kind="artifact" and renders the correct SSE payload:
        {"kind": "artifact", "contextId": "...", "taskId": "...", "artifact": {...}, "metadata": {}}

    Callers should push the event to SSE streams via SSEStreamer.push_event().

    Args:
        task_id:    The task that generated the artifact.
        context_id: The context ID for this task.
        artifact:   The A2A artifact dict (e.g. {"parts": [...], "index": 0}).
        metadata:   Optional event metadata.

    Returns:
        An SSEEvent with kind="artifact".
    """
    return SSEEvent(
        task_id=task_id,
        state="artifact",
        event="TaskArtifactUpdate",
        context_id=context_id,
        kind="artifact",
        artifact=artifact,
        metadata=metadata,
    )