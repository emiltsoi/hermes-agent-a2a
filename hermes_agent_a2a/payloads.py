"""A2A wire-format payload builders.

Pure helpers that construct proto-compliant Task / TaskStatus / Message /
TaskStatusUpdateEvent dicts. Extracted from ``server.py`` so the HTTP layer
no longer owns A2A serialization. ``server.py`` re-exports these names.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Optional, TYPE_CHECKING

from .security import filter_outbound

if TYPE_CHECKING:
    from .task_queue import _PendingTask


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_status_update_payload(task_id: str, state: str, context_id: Optional[str] = None) -> dict:
    """Build a proto-compliant TaskStatusUpdateEvent payload per a2a.proto:788-800.

    TaskStatusUpdateEvent fields:
      contextId (REQUIRED): context identifier
      taskId (REQUIRED): task identifier
      status (REQUIRED): TaskStatus with state and timestamp
      event (optional): SSE event name
      metadata (optional): additional metadata
    """
    from .hooks import _event_name_for_state as _event_name
    return {
        "taskId": task_id,
        "contextId": context_id or task_id,
        "status": {
            "state": state,
            "timestamp": _utc_now_iso(),
        },
        "event": _event_name(state),
    }


def _build_message_object(text: str, role: int = 2, message_id: Optional[str] = None) -> dict:
    """Build a proto-compliant Message object per a2a.proto:260-277.

    Args:
        text: The message text content.
        role: Role enum value (1=USER, 2=AGENT). Default AGENT since this is server response.
        message_id: Optional message ID. If not provided, a UUID is generated.
    """
    return {
        "message_id": message_id or str(uuid.uuid4()),
        "role": role,
        "parts": [{"text": text}] if text else [],
        "metadata": {},
    }


def _build_task_status(state: str, message_text: Optional[str] = None, timestamp: Optional[str] = None) -> dict:
    """Build a proto-compliant TaskStatus object per a2a.proto:211-219.

    TaskStatus.message is a Message object, not a raw string.
    """
    status = {
        "state": state,
        "timestamp": timestamp or _utc_now_iso(),
    }
    if message_text:
        status["message"] = _build_message_object(message_text, role=2)
    return status


def _build_task_object(task_id: str, state: str, context_id: Optional[str] = None,
                       response: Optional[str] = None, created_at: Optional[float] = None) -> dict:
    """Build a proto-compliant Task object per a2a.proto:163-183.

    Returns a full Task with all fields: id, context_id, status, artifacts.
    """
    ctx_id = context_id or task_id
    filtered = filter_outbound(response) if response else None

    task = {
        "id": task_id,
        "context_id": ctx_id,
        "status": _build_task_status(state, filtered),
    }

    if created_at is not None:
        task["status"]["timestamp"] = datetime.datetime.fromtimestamp(
            created_at, tz=datetime.timezone.utc
        ).isoformat()

    if filtered:
        task["artifacts"] = [
            {
                "artifact_id": str(uuid.uuid4()),
                "parts": [{"text": filtered}],
                "index": 0,
            }
        ]

    return task


def _build_task_list_item(task: "_PendingTask", state: str) -> dict:
    """Build a single task item dict for ListTasks responses.

    Returns a proto-compliant Task object per a2a.proto:163-183.
    """
    filtered = filter_outbound(task.response) if task.response else None
    return _build_task_object(
        task_id=task.task_id,
        state=state,
        context_id=task.context_id,
        response=filtered,
        created_at=task.created_at,
    )
