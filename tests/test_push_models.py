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
        assert info.auth_type is None
        assert info.auth_code is None

    def test_full_fields(self):
        info = AuthenticationInfo(auth_type="bearer", auth_code="secret123")
        assert info.auth_type == "bearer"
        assert info.auth_code == "secret123"

    def test_partial_fields(self):
        info = AuthenticationInfo(auth_type="hmac")
        assert info.auth_type == "hmac"
        assert info.auth_code is None


class TestTaskPushNotificationConfig:
    def test_required_fields(self):
        cfg = TaskPushNotificationConfig(id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook")
        assert cfg.id == "c1"
        assert cfg.task_id == "t1"
        assert cfg.push_transport_type == "webhook"
        assert cfg.endpoint == "https://example.com/hook"
        assert cfg.authentication is None
        assert cfg.metadata is None

    def test_with_authentication(self):
        auth = AuthenticationInfo(auth_type="bearer", auth_code="tok")
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", push_transport_type="webhook",
            endpoint="https://example.com/hook",
            authentication=auth
        )
        assert cfg.authentication.auth_type == "bearer"
        assert cfg.authentication.auth_code == "tok"

    def test_with_metadata(self):
        meta = {"key": "value", "count": 42}
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", push_transport_type="webhook",
            endpoint="https://example.com/hook",
            metadata=meta
        )
        assert cfg.metadata == {"key": "value", "count": 42}

    def test_push_transport_type_not_webhook(self):
        cfg = TaskPushNotificationConfig(
            id="c2", task_id="t1", push_transport_type="gcm", endpoint="https://gcm.example.com"
        )
        assert cfg.push_transport_type == "gcm"


class TestTaskPushNotificationConfigList:
    def test_items_and_has_more(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook"
        )
        lst = TaskPushNotificationConfigList(items=[cfg], has_more=False)
        assert len(lst.items) == 1
        assert lst.items[0].id == "c1"
        assert lst.has_more is False

    def test_has_more_true(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook"
        )
        lst = TaskPushNotificationConfigList(items=[cfg], has_more=True)
        assert lst.has_more is True


class TestCreateTaskPushNotificationConfigRequest:
    def test_required_fields(self):
        req = CreateTaskPushNotificationConfigRequest(
            id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook"
        )
        assert req.id == "c1"
        assert req.task_id == "t1"
        assert req.push_transport_type == "webhook"
        assert req.endpoint == "https://example.com/hook"
        assert req.authentication is None
        assert req.metadata is None

    def test_with_auth_and_metadata(self):
        auth = AuthenticationInfo(auth_type="hmac", auth_code="secret")
        meta = {"env": "prod"}
        req = CreateTaskPushNotificationConfigRequest(
            id="c1", task_id="t1", push_transport_type="webhook",
            endpoint="https://example.com/hook",
            authentication=auth, metadata=meta
        )
        assert req.authentication.auth_type == "hmac"
        assert req.metadata == {"env": "prod"}


class TestCreateTaskPushNotificationConfigResponse:
    def test_config_wrapped(self):
        cfg = TaskPushNotificationConfig(
            id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook"
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
            id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook"
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
            id="c1", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook1"
        )
        cfg2 = TaskPushNotificationConfig(
            id="c2", task_id="t1", push_transport_type="webhook", endpoint="https://example.com/hook2"
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
    def test_config_id(self):
        resp = DeleteTaskPushNotificationConfigResponse(config_id="c1")
        assert resp.config_id == "c1"