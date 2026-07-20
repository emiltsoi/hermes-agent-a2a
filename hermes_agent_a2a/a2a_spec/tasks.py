"""Google A2A task payload builders and result parsers.

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

import enum
import uuid
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone

# A2A-spec-compliant error codes
A2A_ERR_PARSE = -32700
A2A_ERR_INVALID_REQUEST = -32600
A2A_ERR_INTERNAL = -32603
A2A_ERR_TASK_NOT_FOUND = -38000
A2A_ERR_TASK_NOT_CANCELABLE = -38001
A2A_ERR_PUSH_NOT_SUPPORTED = -38002
A2A_ERR_INVALID_STATE_TRANSITION = -38003
A2A_ERR_NON_IDEMPOTENT = -38004


# ---------------------------------------------------------------------------
# Event models
# ---------------------------------------------------------------------------


@dataclass
class TaskArtifactUpdateEvent:
    """TaskArtifactUpdateEvent per a2a.proto:775-787.

    Used in StreamResponse oneof payload with artifact_update discriminator.

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
        """Render as spec-compliant StreamResponse artifact_update discriminator."""
        return {
            "artifact_update": {
                "task_id": self.task_id,
                "context_id": self.context_id,
                "artifact": self.artifact,
                "append": False,
                "last_chunk": True,
                "metadata": self.metadata or {},
            }
        }


@dataclass
class SendMessageConfiguration:
    """SendMessageConfiguration per a2a.proto:143-161.

    Allows the caller to configure output modes, push notifications,
    history length, and blocking behavior for a SendMessage call.

    Attributes:
        accepted_output_modes: List of accepted output modes (e.g., ["text", "data"]).
        task_push_notification_config: Optional push notification config for this task.
        history_length: Optional number of previous messages to include.
        return_immediately: If True, return immediately without waiting for completion.
    """
    accepted_output_modes: Optional[list[str]] = None
    task_push_notification_config: Optional[dict] = None
    history_length: Optional[int] = None
    return_immediately: Optional[bool] = None


# ---------------------------------------------------------------------------
# Enums — spec-compliant per a2a.proto
# ---------------------------------------------------------------------------


class Role(int):
    """Role enum per a2a.proto:245-252.

    Spec values:
      ROLE_UNSPECIFIED = 0
      ROLE_USER = 1       (client to server)
      ROLE_AGENT = 2     (server to client)
    """
    ROLE_UNSPECIFIED = 0
    ROLE_USER = 1
    ROLE_AGENT = 2


class TaskState(enum.Enum):
    """Canonical task states per Google A2A v1.0 spec (a2a.proto:187-208).

    Spec integer values:
      TASK_STATE_SUBMITTED = 1
      WORKING = 2
      COMPLETED = 3
      FAILED = 4
      CANCELED = 5
      INPUT_REQUIRED = 6
      REJECTED = 7
      AUTH_REQUIRED = 8
    """
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}
ACTIVE_STATES = {"submitted", "working", "input_required", "auth_required"}
AUTH_STATES = {"auth_required", "rejected"}


def is_terminal_state(state: str) -> bool:
    return str(state or "").lower() in TERMINAL_STATES



# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


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
    """Build a SendMessage JSON-RPC payload.

    Per spec: Message.role is Role.ROLE_USER (1).
    Per spec: Part uses oneof pattern {"text": "..."}.
    """
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
            "id": task_id,
            "message": {
                "message_id": str(uuid.uuid4()),
                "role": Role.ROLE_USER,
                "parts": [{"text": message}],
                "metadata": metadata,
            },
        },
    }
    return payload


def build_task_get_payload(task_id: str, request_id: Optional[str] = None) -> dict:
    """Build a GetTask JSON-RPC payload per a2a.proto:654-664."""
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "GetTask",
        "params": {"id": task_id},
    }


def build_task_cancel_payload(task_id: str, request_id: Optional[str] = None) -> dict:
    """Build a CancelTask JSON-RPC payload per a2a.proto."""
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "CancelTask",
        "params": {"id": task_id},
    }


def extract_text_from_parts(parts) -> str:
    """Extract text from Parts using spec oneof pattern.

    Per spec a2a.proto:221-242, Part is:
      oneof content { string text, bytes raw, string url, google.protobuf.Value data }
    """
    chunks = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        # Spec oneof: {"text": "..."} directly
        if isinstance(part.get("text"), str):
            chunks.append(part["text"])
        # Legacy: skip type-tagged format
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
    """Parse a task result from JSON-RPC response.

    Per spec: Task has status (TaskStatus with state, message, timestamp),
    artifacts (repeated Artifact), etc.
    """
    rpc_result = rpc_result or {}
    inner = rpc_result.get("task", {}) if isinstance(rpc_result, dict) else {}
    if not inner:
        inner = rpc_result

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