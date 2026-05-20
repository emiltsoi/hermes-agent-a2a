"""T1-1a — Push notification model tests.

Tests written to fail BEFORE implementation (TDD).
Run with: pytest tests/test_push_models.py -v
"""
import pytest

from hermes_agent_a2a.a2a_spec import (
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


class TestAuthenticationInfo:
    def test_all_fields_optional(self):
        info = AuthenticationInfo()
        assert info.scheme is None
        assert info.credentials is None

    def test_full_fields(self):
        info = AuthenticationInfo(scheme="bearer", credentials="secret123")
        assert info.scheme == "bearer"
        assert info.credentials == "secret123"

    def test_partial_fields(self):
        info = AuthenticationInfo(scheme="hmac")
        assert info.scheme == "hmac"
        assert info.credentials is None


class TestTaskPushNotificationConfig:
    def test_required_fields(self):
        cfg = TaskPushNotificationConfig(id="c1", task_id="t1", url="https://example.com/hook")
        assert cfg.id == "c1"
        assert cfg.task_id == "t1"
        assert cfg.url == "https://example.com/hook"
        assert cfg.authentication is None
        assert cfg.metadata is None

    def test_with_authentication(self):
        auth = AuthenticationInfo(scheme="bearer", credentials="tok")
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook",
            authentication=auth
        )
        assert cfg.authentication.scheme == "bearer"
        assert cfg.authentication.credentials == "tok"

    def test_with_metadata(self):
        meta = {"key": "value", "count": 42}
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook",
            metadata=meta
        )
        assert cfg.metadata == {"key": "value", "count": 42}


class TestTaskPushNotificationConfigList:
    def test_items_and_has_more(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook"
        )
        lst = TaskPushNotificationConfigList(items=[cfg], has_more=False)
        assert len(lst.items) == 1
        assert lst.items[0].id == "c1"
        assert lst.has_more is False

    def test_has_more_true(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook"
        )
        lst = TaskPushNotificationConfigList(items=[cfg], has_more=True)
        assert lst.has_more is True


class TestCreateTaskPushNotificationConfigRequest:
    def test_required_fields(self):
        req = CreateTaskPushNotificationConfigRequest(
            id="c1", task_id="t1", url="https://example.com/hook"
        )
        assert req.id == "c1"
        assert req.task_id == "t1"
        assert req.url == "https://example.com/hook"
        assert req.authentication is None
        assert req.metadata is None

    def test_with_auth_and_metadata(self):
        auth = AuthenticationInfo(scheme="hmac", credentials="secret")
        meta = {"env": "prod"}
        req = CreateTaskPushNotificationConfigRequest(
            id="c1", task_id="t1", url="https://example.com/hook",
            authentication=auth, metadata=meta
        )
        assert req.authentication.scheme == "hmac"
        assert req.metadata == {"env": "prod"}


class TestCreateTaskPushNotificationConfigResponse:
    def test_config_wrapped(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook"
        )
        resp = CreateTaskPushNotificationConfigResponse(config=cfg)
        assert resp.config.id == "c1"


class TestGetTaskPushNotificationConfigRequest:
    def test_fields(self):
        req = GetTaskPushNotificationConfigRequest(task_id="t1", config_id="c1")
        assert req.task_id == "t1"
        assert req.config_id == "c1"


class TestGetTaskPushNotificationConfigResponse:
    def test_config_wrapped(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook"
        )
        resp = GetTaskPushNotificationConfigResponse(config=cfg)
        assert resp.config.id == "c1"


class TestListTaskPushNotificationConfigsRequest:
    def test_task_id(self):
        req = ListTaskPushNotificationConfigsRequest(task_id="t1")
        assert req.task_id == "t1"


class TestListTaskPushNotificationConfigsResponse:
    def test_items_and_has_more(self):
        cfg1 = TaskPushNotificationConfig(
            id="c1", task_id="t1", url="https://example.com/hook1"
        )
        cfg2 = TaskPushNotificationConfig(
            id="c2", task_id="t1", url="https://example.com/hook2"
        )
        resp = ListTaskPushNotificationConfigsResponse(items=[cfg1, cfg2], has_more=False)
        assert len(resp.items) == 2
        assert resp.has_more is False


class TestDeleteTaskPushNotificationConfigRequest:
    def test_fields(self):
        req = DeleteTaskPushNotificationConfigRequest(task_id="t1", config_id="c1")
        assert req.task_id == "t1"
        assert req.config_id == "c1"


class TestDeleteTaskPushNotificationConfigResponse:
    # Per spec: DeleteTaskPushNotificationConfigResponse is google.protobuf.Empty (no fields)
    pass