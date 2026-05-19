"""Google A2A Task Push Notification models — T1-1a.

Spec reference: Google A2A proto3 spec, tasks/pushNotification methods.
"""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class AuthenticationInfo:
    """Embedded auth info within TaskPushNotificationConfig.

    Per spec: auth_type (string), auth_code (string, optional).
    All fields are optional.
    """
    auth_type: Optional[str] = None
    auth_code: Optional[str] = None


@dataclass
class TaskPushNotificationConfig:
    """Per spec: a registered push notification config for a task.

    Fields: id, task_id, push_transport_type, endpoint, authentication (opt), metadata (opt).
    push_transport_type values: "webhook", "gcm", etc.
    """
    id: str
    task_id: str
    push_transport_type: str
    endpoint: str
    authentication: Optional[AuthenticationInfo] = None
    metadata: Optional[dict] = None


@dataclass
class TaskPushNotificationConfigList:
    """Wrapper for list responses.

    Fields: items (list), has_more (bool).
    """
    items: List[TaskPushNotificationConfig] = field(default_factory=list)
    has_more: bool = False


@dataclass
class CreateTaskPushNotificationConfigRequest:
    """Create a push notification config for a task.

    Fields: id, task_id, push_transport_type, endpoint, authentication (opt), metadata (opt).
    """
    id: str
    task_id: str
    push_transport_type: str
    endpoint: str
    authentication: Optional[AuthenticationInfo] = None
    metadata: Optional[dict] = None


@dataclass
class CreateTaskPushNotificationConfigResponse:
    """Response after creating a push notification config.

    Fields: config (TaskPushNotificationConfig).
    """
    config: TaskPushNotificationConfig


@dataclass
class GetTaskPushNotificationConfigRequest:
    """Request to retrieve a single push notification config.

    Fields: task_id, config_id.
    """
    task_id: str
    config_id: str


@dataclass
class GetTaskPushNotificationConfigResponse:
    """Response with a single push notification config.

    Fields: config (TaskPushNotificationConfig).
    """
    config: TaskPushNotificationConfig


@dataclass
class ListTaskPushNotificationConfigsRequest:
    """Request to list all push notification configs for a task.

    Fields: task_id.
    """
    task_id: str


@dataclass
class ListTaskPushNotificationConfigsResponse:
    """Paginated list of push notification configs for a task.

    Fields: items, has_more.
    """
    items: List[TaskPushNotificationConfig] = field(default_factory=list)
    has_more: bool = False


@dataclass
class DeleteTaskPushNotificationConfigRequest:
    """Request to delete a push notification config.

    Fields: task_id, config_id.
    """
    task_id: str
    config_id: str


@dataclass
class DeleteTaskPushNotificationConfigResponse:
    """Response after deleting a push notification config.

    Fields: config_id.
    """
    config_id: str