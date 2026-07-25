"""A2A tool registration."""

import json
import logging

from . import schemas
from .tool_handlers import (
    _dict_args_handler,
    handle_announce,
    handle_cancel_protocol_task,
    handle_discover,
    handle_get_metrics,
    handle_list,
    handle_run_local_agent_task,
    handle_run_remote_agent_task,
    handle_send_protocol_task,
    handle_help,
    set_runtime_callbacks,
)

logger = logging.getLogger(__name__)


def _serialize_dict_handler(handler):
    """Wrap a handler that returns a dict so the tool registry sees a JSON string.

    Hermes-agent commit f8361d29c (July 9 2026) enforces that tool handlers
    return str or a _multimodal dict.  Our A2A handlers return plain dicts,
    which triggers ``"Tool handler returned unsupported result type: dict"``.
    This wrapper serializes dict returns to json.dumps(str) so they pass the
    registry contract.  Internal callers call the raw handler directly and still
    receive dicts.
    """
    def wrapper(*args, **kwargs):
        result = handler(*args, **kwargs)
        if isinstance(result, dict):
            return json.dumps(result)
        return result
    return wrapper


def register(registry, ensure_server=None, get_vault_resolver=None) -> None:
    set_runtime_callbacks(ensure_server=ensure_server, get_vault_resolver=get_vault_resolver)
    registry.register_tool(
        name=schemas.A2A_HELP["name"],
        toolset="a2a",
        schema=schemas.A2A_HELP,
        handler=_serialize_dict_handler(_dict_args_handler(handle_help)),
    )
    registry.register_tool(
        name=schemas.A2A_DISCOVER["name"],
        toolset="a2a",
        schema=schemas.A2A_DISCOVER,
        handler=_serialize_dict_handler(_dict_args_handler(handle_discover)),
    )
    registry.register_tool(
        name=schemas.A2A_LIST["name"],
        toolset="a2a",
        schema=schemas.A2A_LIST,
        handler=_serialize_dict_handler(_dict_args_handler(handle_list)),
    )
    registry.register_tool(
        name=schemas.A2A_ANNOUNCE["name"],
        toolset="a2a",
        schema=schemas.A2A_ANNOUNCE,
        handler=_serialize_dict_handler(_dict_args_handler(handle_announce)),
    )
    registry.register_tool(
        name=schemas.A2A_CALL["name"],
        toolset="a2a",
        schema=schemas.A2A_CALL,
        handler=_serialize_dict_handler(_dict_args_handler(handle_send_protocol_task)),
    )
    registry.register_tool(
        name=schemas.A2A_CANCEL_PROTOCOL_TASK["name"],
        toolset="a2a",
        schema=schemas.A2A_CANCEL_PROTOCOL_TASK,
        handler=_serialize_dict_handler(_dict_args_handler(handle_cancel_protocol_task)),
    )
    registry.register_tool(
        name=schemas.A2A_RUN_LOCAL_AGENT_TASK["name"],
        toolset="a2a",
        schema=schemas.A2A_RUN_LOCAL_AGENT_TASK,
        handler=_serialize_dict_handler(_dict_args_handler(handle_run_local_agent_task)),
    )
    registry.register_tool(
        name=schemas.A2A_RUN_REMOTE_AGENT_TASK["name"],
        toolset="a2a",
        schema=schemas.A2A_RUN_REMOTE_AGENT_TASK,
        handler=_serialize_dict_handler(_dict_args_handler(handle_run_remote_agent_task)),
    )
    registry.register_tool(
        name=schemas.A2A_GET_METRICS["name"],
        toolset="a2a",
        schema=schemas.A2A_GET_METRICS,
        handler=_serialize_dict_handler(handle_get_metrics),
    )
    logger.info("[A2A] Phase 3 tools registered")


__all__ = ["register"]
