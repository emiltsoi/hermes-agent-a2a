"""Google A2A-shaped helpers plus Hermes metadata extensions."""

from .agent_card import (
    AgentProvider,
    AgentSkill,
    AgentCapabilities,
    AgentInterface,
    ExtendedAgentCard,
    build_extended_agent_card,
    skill_names,
    validate_skill,
    # Legacy aliases (DEPRECATED)
    Provider,
    Skill,
)
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
from .push import (
    AuthenticationInfo,
    TaskPushNotificationConfig,
    TaskPushNotificationConfigList,
    CreateTaskPushNotificationConfigRequest,
    CreateTaskPushNotificationConfigResponse,
    GetTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigResponse,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    DeleteTaskPushNotificationConfigRequest,
    DeleteTaskPushNotificationConfigResponse,
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
    # Agent Card models (spec-compliant)
    "AgentProvider",
    "AgentSkill",
    "AgentCapabilities",
    "AgentInterface",
    "ExtendedAgentCard",
    "build_extended_agent_card",
    "skill_names",
    "validate_skill",
    # Legacy Agent Card models (DEPRECATED)
    "Provider",
    "Skill",
    # Push notification models (T1-1a)
    "AuthenticationInfo",
    "TaskPushNotificationConfig",
    "TaskPushNotificationConfigList",
    "CreateTaskPushNotificationConfigRequest",
    "CreateTaskPushNotificationConfigResponse",
    "GetTaskPushNotificationConfigRequest",
    "GetTaskPushNotificationConfigResponse",
    "ListTaskPushNotificationConfigsRequest",
    "ListTaskPushNotificationConfigsResponse",
    "DeleteTaskPushNotificationConfigRequest",
    "DeleteTaskPushNotificationConfigResponse",
]
