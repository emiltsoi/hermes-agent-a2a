"""Google A2A-shaped helpers plus Hermes metadata extensions."""

from .hermes_ext import build_hermes_metadata
from .tasks import (
    TERMINAL_STATES,
    build_task_cancel_payload,
    build_task_get_payload,
    build_task_send_payload,
    extract_text_from_parts,
    is_terminal_state,
    parse_json_rpc_error,
    parse_task_result,
)

__all__ = [
    "TERMINAL_STATES",
    "build_hermes_metadata",
    "build_task_cancel_payload",
    "build_task_get_payload",
    "build_task_send_payload",
    "extract_text_from_parts",
    "is_terminal_state",
    "parse_json_rpc_error",
    "parse_task_result",
]
