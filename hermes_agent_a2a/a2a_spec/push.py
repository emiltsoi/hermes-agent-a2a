"""Google A2A Task Push Notification models — T1-1a.

Spec reference: a2a.proto:325-332 (AuthenticationInfo), a2a.proto:464-478 (TaskPushNotificationConfig).
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class AuthenticationInfo:
    """AuthenticationInfo per a2a.proto:325-332.

    Per spec: scheme (REQUIRED), credentials (optional).
    All fields optional here for flexibility.
    """
    scheme: Optional[str] = None
    credentials: Optional[str] = None


@dataclass
class TaskPushNotificationConfig:
    """TaskPushNotificationConfig per a2a.proto:464-478.

    Per spec: tenant, id (REQUIRED), task_id (REQUIRED), url (REQUIRED), token (opt), authentication (opt).
    """
    id: str
    task_id: str
    url: str
    tenant: Optional[str] = None
    token: Optional[str] = None
    authentication: Optional[AuthenticationInfo] = None
    metadata: Optional[dict] = None


@dataclass
class TaskPushNotificationConfigList:
    """Paginated list of push notification configs.

    Per spec: items (repeated), has_more (bool).
    """
    items: List[TaskPushNotificationConfig] = field(default_factory=list)
    has_more: bool = False


@dataclass
class CreateTaskPushNotificationConfigRequest:
    """CreateTaskPushNotificationConfigRequest per a2a.proto.

    Per spec: tenant, id, task_id, url (REQUIRED), token (opt), authentication (opt), metadata (opt).
    """
    id: str
    task_id: str
    url: str
    tenant: Optional[str] = None
    token: Optional[str] = None
    authentication: Optional[AuthenticationInfo] = None
    metadata: Optional[dict] = None


@dataclass
class CreateTaskPushNotificationConfigResponse:
    """CreateTaskPushNotificationConfigResponse per a2a.proto.

    Fields: config (TaskPushNotificationConfig).
    """
    config: TaskPushNotificationConfig


@dataclass
class GetTaskPushNotificationConfigRequest:
    """GetTaskPushNotificationConfigRequest per a2a.proto.

    Per spec: tenant, id (config_id) — task_id is in URL path.
    """
    task_id: str
    config_id: str
    tenant: Optional[str] = None


@dataclass
class GetTaskPushNotificationConfigResponse:
    """GetTaskPushNotificationConfigResponse per a2a.proto.

    Fields: config (TaskPushNotificationConfig).
    """
    config: TaskPushNotificationConfig


@dataclass
class ListTaskPushNotificationConfigsRequest:
    """ListTaskPushNotificationConfigsRequest per a2a.proto.

    Per spec: tenant, task_id.
    """
    task_id: str
    tenant: Optional[str] = None


@dataclass
class ListTaskPushNotificationConfigsResponse:
    """ListTaskPushNotificationConfigsResponse per a2a.proto.

    Fields: items, has_more.
    """
    items: List[TaskPushNotificationConfig] = field(default_factory=list)
    has_more: bool = False


@dataclass
class DeleteTaskPushNotificationConfigRequest:
    """DeleteTaskPushNotificationConfigRequest per a2a.proto.

    Per spec: tenant, id (config_id) — task_id is in URL path.
    """
    task_id: str
    config_id: str
    tenant: Optional[str] = None


@dataclass
class DeleteTaskPushNotificationConfigResponse:
    """DeleteTaskPushNotificationConfigResponse per a2a.proto.

    Per spec: returns google.protobuf.Empty (no body), HTTP 204.
    """
    pass