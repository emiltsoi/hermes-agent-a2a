"""Google A2A-shaped task payload builders and result parsers."""

import uuid
from typing import Optional

TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}
ACTIVE_STATES = {"submitted", "working"}


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
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": message}],
                "metadata": metadata,
            },
        },
    }
    if skill:
        payload["params"]["metadata"] = {"skill": skill}
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


def parse_task_result(rpc_result: dict, default_task_id: str = "") -> dict:
    rpc_result = rpc_result or {}
    status = rpc_result.get("status", {}) if isinstance(rpc_result, dict) else {}
    state = status.get("state", "unknown") if isinstance(status, dict) else "unknown"
    task_id = rpc_result.get("id", default_task_id) if isinstance(rpc_result, dict) else default_task_id
    chunks = []

    for artifact in rpc_result.get("artifacts", []) or []:
        if isinstance(artifact, dict):
            text = extract_text_from_parts(artifact.get("parts", []))
            if text:
                chunks.append(text)

    status_message = status.get("message", {}) if isinstance(status, dict) else {}
    if isinstance(status_message, dict):
        text = extract_text_from_parts(status_message.get("parts", []))
        if text:
            chunks.append(text)

    direct_message = rpc_result.get("message", {}) if isinstance(rpc_result, dict) else {}
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
