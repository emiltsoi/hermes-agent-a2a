"""Google A2A-shaped task payload builders and result parsers.

Google A2A v1.0 error codes:
  -32700  Parse error
  -32600  Invalid Request
  -32603  Internal error
  -38000  Task not found
  -38001  Task not cancelable
  -38002  Push notification not supported
  -38003  Invalid state transition
  -38004  Non-idempotent task
"""

import uuid
from typing import Optional

# A2A-spec-compliant error codes
A2A_ERR_PARSE = -32700
A2A_ERR_INVALID_REQUEST = -32600
A2A_ERR_INTERNAL = -32603
A2A_ERR_TASK_NOT_FOUND = -38000
A2A_ERR_TASK_NOT_CANCELABLE = -38001
A2A_ERR_PUSH_NOT_SUPPORTED = -38002
A2A_ERR_INVALID_STATE_TRANSITION = -38003
A2A_ERR_NON_IDEMPOTENT = -38004

from dataclasses import dataclass


@dataclass
class TaskArtifactUpdateEvent:
    """A task artifact update event.

    Emitted over SSE when a task generates an artifact (partial or final).

    Attributes:
        context_id: REQUIRED — the context ID for this task.
        task_id:    REQUIRED — the task this artifact belongs to.
        artifact:   REQUIRED — the artifact data dict (A2A artifact shape).
        metadata:   optional — additional event metadata.
    """
    context_id: str
    task_id: str
    artifact: dict
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        """Render the event as a plain dict for SSE transport."""
        return {
            "kind": "artifact",
            "contextId": self.context_id,
            "taskId": self.task_id,
            "artifact": self.artifact,
            "metadata": self.metadata or {},
        }


import enum


class TaskState(str, enum.Enum):
    """Canonical task states per Google A2A v1.0 spec."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATES = {"completed", "failed", "canceled"}
ACTIVE_STATES = {"submitted", "working", "input_required", "auth_required"}
AUTH_STATES = {"auth_required", "authenticated", "rejected"}


def is_terminal_state(state: str) -> bool:
    return str(state or "").lower() in TERMINAL_STATES


def build_task_send_payload(
    task_id: str,
    message: str,
    sender_name: str,
    intent: str = "consultation",
    expected_action: str = "reply",
    skill: Optional[str] = None,
    hermes: Optional[dict] = None,
    request_id: Optional[str] = None,
) -> dict:
    metadata = {
        "intent": intent,
        "expected_action": expected_action,
        "context_scope": "full",
        "sender_name": sender_name,
    }
    if skill:
        metadata["skill"] = skill
    if hermes:
        metadata["hermes"] = hermes

    payload = {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": str(uuid.uuid4()),
                "role": 1,
                "parts": [{"text": message}],
                "metadata": metadata,
            },
        },
    }
    return payload


def build_task_get_payload(task_id: str, request_id: Optional[str] = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "tasks/get",
        "params": {"id": task_id},
    }


def build_task_cancel_payload(task_id: str, request_id: Optional[str] = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "tasks/cancel",
        "params": {"id": task_id},
    }


def extract_text_from_parts(parts) -> str:
    chunks = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text", "")
            if text:
                chunks.append(str(text))
        elif isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def parse_json_rpc_error(response: dict) -> str:
    rpc_error = response.get("error") if isinstance(response, dict) else None
    if not rpc_error:
        return ""
    if isinstance(rpc_error, dict):
        return rpc_error.get("message") or str(rpc_error)
    return str(rpc_error)


def build_error_response(code: int, message: str, data=None, id=None) -> dict:
    """Build a spec-compliant JSON-RPC error response: {jsonrpc, code, message, data, id}."""
    err = {"jsonrpc": "2.0", "code": code, "message": message}
    if data is not None:
        err["data"] = data
    if id is not None:
        err["id"] = id
    return err


def parse_task_result(rpc_result: dict, default_task_id: str = "") -> dict:
    rpc_result = rpc_result or {}
    # v1.0 wraps response in a "task" envelope
    inner = rpc_result.get("task", {}) if isinstance(rpc_result, dict) else {}
    if not inner:
        inner = rpc_result  # fall back to old flat format

    status = inner.get("status", {}) if isinstance(inner, dict) else {}
    state = status.get("state", "unknown") if isinstance(status, dict) else "unknown"
    task_id = inner.get("id", default_task_id) if isinstance(inner, dict) else default_task_id
    chunks = []

    for artifact in inner.get("artifacts", []) or []:
        if isinstance(artifact, dict):
            text = extract_text_from_parts(artifact.get("parts", []))
            if text:
                chunks.append(text)

    status_message = status.get("message", {}) if isinstance(status, dict) else {}
    if isinstance(status_message, dict):
        text = extract_text_from_parts(status_message.get("parts", []))
        if text:
            chunks.append(text)

    direct_message = inner.get("message", {}) if isinstance(inner, dict) else {}
    if isinstance(direct_message, dict):
        text = extract_text_from_parts(direct_message.get("parts", []))
        if text:
            chunks.append(text)

    return {
        "task_id": task_id,
        "state": state,
        "response": "\n".join(chunks).strip(),
        "raw_result": rpc_result,
    }
