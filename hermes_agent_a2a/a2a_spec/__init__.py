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
)
from .hermes_ext import build_hermes_metadata
from .tasks import (
    TERMINAL_STATES,
    SendMessageConfiguration,
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
    "SendMessageConfiguration",
    "build_hermes_metadata",
    "build_task_cancel_payload",
    "build_task_get_payload",
    "build_task_send_payload",
    "extract_text_from_parts",
    "is_terminal_state",
    "parse_json_rpc_error",
    "parse_task_result",
    # Agent Card models
    "AgentProvider",
    "AgentSkill",
    "AgentCapabilities",
    "AgentInterface",
    "ExtendedAgentCard",
    "build_extended_agent_card",
    "skill_names",
    "validate_skill",
    # Push notification models
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
