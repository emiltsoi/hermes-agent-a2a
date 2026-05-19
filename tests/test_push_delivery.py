"""T1-1b — TaskPushNotificationConfig CRUD + delivery tests.

Tests written to fail BEFORE implementation.
Run with: pytest tests/test_push_delivery.py -v
"""
from unittest.mock import patch, MagicMock

import pytest

from hermes_agent_a2a.a2a_spec.push import (
    AuthenticationInfo,
    TaskPushNotificationConfig,
)


# ---------------------------------------------------------------------------
# CRUD Operations
# ---------------------------------------------------------------------------

class TestCreatePushConfig:
    """create_push_config(task_id, push_transport_type, endpoint, ...) → TaskPushNotificationConfig."""

    def test_create_returns_config(self):
        """create_push_config must return a TaskPushNotificationConfig."""
        from hermes_agent_a2a.push_delivery import create_push_config
        cfg = create_push_config("task-1", "webhook", "https://example.com/hook")
        assert isinstance(cfg, TaskPushNotificationConfig)

    def test_create_sets_required_fields(self):
        """Created config must have correct task_id, push_transport_type, endpoint."""
        from hermes_agent_a2a.push_delivery import create_push_config
        cfg = create_push_config("t-x", "webhook", "https://x.com/h")
        assert cfg.task_id == "t-x"
        assert cfg.push_transport_type == "webhook"
        assert cfg.endpoint == "https://x.com/h"

    def test_create_generates_id(self):
        """create_push_config must generate a config_id."""
        from hermes_agent_a2a.push_delivery import create_push_config
        cfg = create_push_config("t-2", "gcm", "https://gcm.example.com")
        assert cfg.id, "config must have a non-empty id"
        assert isinstance(cfg.id, str)

    def test_create_with_authentication(self):
        """authentication dict is stored on the config."""
        from hermes_agent_a2a.push_delivery import create_push_config
        auth = AuthenticationInfo(auth_type="bearer", auth_code="tok123")
        cfg = create_push_config("t-3", "webhook", "https://x.com/h", authentication=auth)
        assert cfg.authentication is not None
        assert cfg.authentication.auth_type == "bearer"
        assert cfg.authentication.auth_code == "tok123"

    def test_create_with_metadata(self):
        """metadata dict is stored on the config."""
        from hermes_agent_a2a.push_delivery import create_push_config
        meta = {"env": "prod"}
        cfg = create_push_config("t-4", "webhook", "https://x.com/h", metadata=meta)
        assert cfg.metadata == {"env": "prod"}

    def test_create_idempotent_per_task(self):
        """Two creates for same task_id return configs with distinct ids."""
        from hermes_agent_a2a.push_delivery import create_push_config
        c1 = create_push_config("t-5", "webhook", "https://c1.com")
        c2 = create_push_config("t-5", "webhook", "https://c2.com")
        assert c1.id != c2.id, "Each create must generate a unique config id"


class TestGetPushConfig:
    """get_push_config(task_id, config_id) → TaskPushNotificationConfig."""

    def test_get_returns_config(self):
        """get_push_config must return the registered config."""
        from hermes_agent_a2a.push_delivery import create_push_config, get_push_config
        created = create_push_config("t-get-1", "webhook", "https://get.com/h")
        retrieved = get_push_config("t-get-1", created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.task_id == "t-get-1"
        assert retrieved.endpoint == "https://get.com/h"

    def test_get_returns_none_for_unknown_config_id(self):
        """Unknown config_id must return None."""
        from hermes_agent_a2a.push_delivery import create_push_config, get_push_config
        create_push_config("t-get-2", "webhook", "https://x.com/h")
        result = get_push_config("t-get-2", "nonexistent-config-id")
        assert result is None

    def test_get_returns_none_for_unknown_task_id(self):
        """Unknown task_id must return None."""
        from hermes_agent_a2a.push_delivery import get_push_config
        result = get_push_config("nonexistent-task-xyz", "any-config")
        assert result is None


class TestListPushConfigs:
    """list_push_configs(task_id) → list[TaskPushNotificationConfig]."""

    def test_list_returns_all_configs_for_task(self):
        """list_push_configs returns all configs registered for the task_id."""
        from hermes_agent_a2a.push_delivery import create_push_config, list_push_configs
        c1 = create_push_config("t-list-1", "webhook", "https://l1.com")
        c2 = create_push_config("t-list-1", "webhook", "https://l2.com")
        configs = list_push_configs("t-list-1")
        assert len(configs) == 2
        ids = {c.id for c in configs}
        assert ids == {c1.id, c2.id}

    def test_list_returns_empty_for_unknown_task(self):
        """Unknown task returns empty list."""
        from hermes_agent_a2a.push_delivery import list_push_configs
        configs = list_push_configs("nonexistent-task-abc")
        assert configs == []


class TestDeletePushConfig:
    """delete_push_config(task_id, config_id) → config_id string."""

    def test_delete_returns_config_id(self):
        """delete_push_config must return the deleted config_id."""
        from hermes_agent_a2a.push_delivery import create_push_config, delete_push_config
        created = create_push_config("t-del-1", "webhook", "https://del.com/h")
        result = delete_push_config("t-del-1", created.id)
        assert result == created.id

    def test_delete_removes_config(self):
        """Deleted config is no longer retrievable."""
        from hermes_agent_a2a.push_delivery import create_push_config, get_push_config, delete_push_config
        created = create_push_config("t-del-2", "webhook", "https://del2.com/h")
        delete_push_config("t-del-2", created.id)
        result = get_push_config("t-del-2", created.id)
        assert result is None

    def test_delete_returns_config_id_string(self):
        """delete_push_config must return a string config_id."""
        from hermes_agent_a2a.push_delivery import create_push_config, delete_push_config
        created = create_push_config("t-del-3", "webhook", "https://del3.com/h")
        result = delete_push_config("t-del-3", created.id)
        assert isinstance(result, str)
        assert result == created.id

    def test_delete_unknown_returns_none(self):
        """Deleting an unknown config_id returns None."""
        from hermes_agent_a2a.push_delivery import delete_push_config
        result = delete_push_config("t-del-4", "nonexistent-id")
        assert result is None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class TestDeliverPushNotification:
    """deliver_push_notification(task_id, config_id, payload) → bool."""

    def test_deliver_posts_to_endpoint(self):
        """deliver_push_notification must POST payload to the config's endpoint."""
        from hermes_agent_a2a.push_delivery import create_push_config, deliver_push_notification

        created = create_push_config("t-dlv-1", "webhook", "https://dlv.example.com/hook")
        payload = {"event": "task.completed", "task_id": "t-dlv-1"}

        # Patch validate_webhook_endpoint at the location where push_delivery looks it up
        with patch("hermes_agent_a2a.push_delivery.validate_webhook_endpoint", return_value=(True, "")):
            with patch("hermes_agent_a2a.push_delivery.httpx.Client") as mock_client_cls:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_client = MagicMock()
                mock_client.post.return_value = mock_resp
                mock_client_cls.return_value.__enter__.return_value = mock_client

                deliver_push_notification("t-dlv-1", created.id, payload)

                mock_client.post.assert_called_once()
                call_args = mock_client.post.call_args
                args, kwargs = call_args
                # url is passed as first positional arg to client.post()
                assert args[0] == "https://dlv.example.com/hook"
                assert kwargs.get("json") == payload

    def test_deliver_returns_true_on_2xx(self):
        """deliver_push_notification returns True on 2xx response."""
        from hermes_agent_a2a.push_delivery import create_push_config, deliver_push_notification

        created = create_push_config("t-dlv-2", "webhook", "https://dlv2.example.com/h")
        with patch("hermes_agent_a2a.push_delivery.validate_webhook_endpoint", return_value=(True, "")):
            with patch("httpx.Client") as mock_client_cls:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_client = MagicMock()
                mock_client.post.return_value = mock_resp
                mock_client_cls.return_value.__enter__.return_value = mock_client

                result = deliver_push_notification("t-dlv-2", created.id, {"foo": "bar"})
                assert result is True

    def test_deliver_returns_false_on_non_2xx(self):
        """deliver_push_notification returns False on non-2xx response."""
        from hermes_agent_a2a.push_delivery import create_push_config, deliver_push_notification

        created = create_push_config("t-dlv-3", "webhook", "https://dlv3.example.com/h")
        with patch("httpx.Client") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_client = MagicMock()
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value.__enter__.return_value = mock_client

            result = deliver_push_notification("t-dlv-3", created.id, {"foo": "bar"})
            assert result is False

    def test_deliver_handles_timeout_exception(self):
        """TimeoutException is caught and delivery returns False without raising."""
        import httpx
        from hermes_agent_a2a.push_delivery import create_push_config, deliver_push_notification

        created = create_push_config("t-dlv-4", "webhook", "https://dlv4.example.com/h")
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.post.side_effect = httpx.TimeoutException("timed out")
            mock_client_cls.return_value.__enter__.return_value = mock_client

            result = deliver_push_notification("t-dlv-4", created.id, {"foo": "bar"})
            assert result is False

    def test_deliver_handles_connect_error(self):
        """ConnectError is caught and delivery returns False without raising."""
        import httpx
        from hermes_agent_a2a.push_delivery import create_push_config, deliver_push_notification

        created = create_push_config("t-dlv-5", "webhook", "https://dlv5.example.com/h")
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client_cls.return_value.__enter__.return_value = mock_client

            result = deliver_push_notification("t-dlv-5", created.id, {"foo": "bar"})
            assert result is False

    def test_deliver_unknown_config_returns_false(self):
        """deliver_push_notification for unknown config_id returns False."""
        from hermes_agent_a2a.push_delivery import deliver_push_notification
        result = deliver_push_notification("nonexistent-task", "nonexistent-config", {"foo": "bar"})
        assert result is False