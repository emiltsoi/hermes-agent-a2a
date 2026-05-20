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
    """create_push_config(task_id, url, authentication, metadata) → TaskPushNotificationConfig."""

    def test_create_returns_config(self):
        """create_push_config must return a TaskPushNotificationConfig."""
        from hermes_agent_a2a.push_delivery import create_push_config
        cfg = create_push_config("task-1", "https://example.com/hook", None, None)
        assert isinstance(cfg, TaskPushNotificationConfig)

    def test_create_sets_required_fields(self):
        """Created config must have correct task_id and url."""
        from hermes_agent_a2a.push_delivery import create_push_config
        cfg = create_push_config("t-x", "https://x.com/h", None, None)
        assert cfg.task_id == "t-x"
        assert cfg.url == "https://x.com/h"

    def test_create_generates_id(self):
        """create_push_config must generate a config_id."""
        from hermes_agent_a2a.push_delivery import create_push_config
        cfg = create_push_config("t-2", "https://gcm.example.com", None, None)
        assert cfg.id, "config must have a non-empty id"
        assert isinstance(cfg.id, str)

    def test_create_with_authentication(self):
        """authentication dict is stored on the config."""
        from hermes_agent_a2a.push_delivery import create_push_config
        auth = AuthenticationInfo(scheme="bearer", credentials="tok123")
        cfg = create_push_config("t-3", "https://x.com/h", authentication=auth, metadata=None)
        assert cfg.authentication is not None
        assert cfg.authentication.scheme == "bearer"
        assert cfg.authentication.credentials == "tok123"

    def test_create_with_metadata(self):
        """metadata dict is stored on the config."""
        from hermes_agent_a2a.push_delivery import create_push_config
        meta = {"env": "prod"}
        cfg = create_push_config("t-4", "https://x.com/h", None, metadata=meta)
        assert cfg.metadata == {"env": "prod"}

    def test_create_idempotent_per_task(self):
        """Two creates for same task_id return configs with distinct ids."""
        from hermes_agent_a2a.push_delivery import create_push_config
        c1 = create_push_config("t-5", "https://c1.com", None, None)
        c2 = create_push_config("t-5", "https://c2.com", None, None)
        assert c1.id != c2.id, "Each create must generate a unique config id"


class TestGetPushConfig:
    """get_push_config(task_id, config_id) → TaskPushNotificationConfig."""

    def test_get_returns_config(self):
        """get_push_config must return the registered config."""
        from hermes_agent_a2a.push_delivery import create_push_config, get_push_config
        created = create_push_config("t-get-1", "https://get.com/h", None, None)
        retrieved = get_push_config("t-get-1", created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.task_id == "t-get-1"
        assert retrieved.url == "https://get.com/h"

    def test_get_returns_none_for_unknown_config_id(self):
        """Unknown config_id must return None."""
        from hermes_agent_a2a.push_delivery import create_push_config, get_push_config
        create_push_config("t-get-2", "https://x.com/h", None, None)
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
        c1 = create_push_config("t-list-1", "https://l1.com", None, None)
        c2 = create_push_config("t-list-1", "https://l2.com", None, None)
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
        created = create_push_config("t-del-1", "https://del.com/h", None, None)
        result = delete_push_config("t-del-1", created.id)
        assert result == created.id

    def test_delete_removes_config(self):
        """Deleted config is no longer retrievable."""
        from hermes_agent_a2a.push_delivery import create_push_config, get_push_config, delete_push_config
        created = create_push_config("t-del-2", "https://del2.com/h", None, None)
        delete_push_config("t-del-2", created.id)
        result = get_push_config("t-del-2", created.id)
        assert result is None

    def test_delete_returns_config_id_string(self):
        """delete_push_config must return a string config_id."""
        from hermes_agent_a2a.push_delivery import create_push_config, delete_push_config
        created = create_push_config("t-del-3", "https://del3.com/h", None, None)
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

    def test_deliver_posts_to_url(self):
        """deliver_push_notification must POST payload to the config's url."""
        from hermes_agent_a2a.push_delivery import create_push_config, deliver_push_notification

        created = create_push_config("t-dlv-1", "https://dlv.example.com/hook", None, None)
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

        created = create_push_config("t-dlv-2", "https://dlv2.example.com/h", None, None)
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

        created = create_push_config("t-dlv-3", "https://dlv3.example.com/h", None, None)
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

        created = create_push_config("t-dlv-4", "https://dlv4.example.com/h", None, None)
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

        created = create_push_config("t-dlv-5", "https://dlv5.example.com/h", None, None)
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


# ---------------------------------------------------------------------------
# HMAC Verification
# ---------------------------------------------------------------------------

from hermes_agent_a2a.push_delivery import verify_hmac


class TestVerifyHMACValidSignature:
    """verify_hmac returns True when signature is correct."""

    def test_valid_hmac_key_produces_correct_signature(self):
        """A valid HMAC key produces a signature that verify_hmac accepts."""
        payload = {"event": "task.completed", "task_id": "t-1"}
        hmac_key = "my-secret-webhook-key"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(payload, hmac_key)

        assert verify_hmac(payload, signature, hmac_key) is True

    def test_valid_signature_with_nested_payload(self):
        """verify_hmac accepts valid signature for nested JSON payload."""
        payload = {
            "kind": "artifact",
            "contextId": "ctx-1",
            "taskId": "t-2",
            "artifact": {"name": "test", "parts": [{"text": "hello"}]},
        }
        hmac_key = "another-secret"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(payload, hmac_key)

        assert verify_hmac(payload, signature, hmac_key) is True

    def test_valid_signature_with_empty_payload(self):
        """verify_hmac accepts valid signature for empty dict payload."""
        payload = {}
        hmac_key = "empty-payload-key"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(payload, hmac_key)

        assert verify_hmac(payload, signature, hmac_key) is True


class TestVerifyHMACWrongKey:
    """verify_hmac returns False when wrong key is used."""

    def test_wrong_hmac_key_returns_false(self):
        """Using a different key than the one used to sign produces invalid signature."""
        payload = {"event": "task.completed"}
        signing_key = "correct-key"
        wrong_key = "wrong-key"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(payload, signing_key)

        assert verify_hmac(payload, signature, wrong_key) is False

    def test_tampered_key_returns_false(self):
        """A key with extra characters appended is rejected."""
        payload = {"foo": "bar"}
        correct_key = "secret-key"
        tampered_key = "secret-key-extra"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(payload, correct_key)

        assert verify_hmac(payload, signature, tampered_key) is False

    def test_empty_wrong_key_returns_false(self):
        """An empty string as key produces a different signature."""
        payload = {"test": "data"}
        real_key = "real-key"
        empty_key = ""

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(payload, real_key)

        assert verify_hmac(payload, signature, empty_key) is False


class TestVerifyHMACTamperedPayload:
    """verify_hmac returns False when payload has been tampered with."""

    def test_tampered_payload_is_detected_and_rejected(self):
        """Modifying payload after signing causes verification to fail."""
        original_payload = {"event": "task.completed", "task_id": "t-1"}
        tampered_payload = {"event": "task.completed", "task_id": "t-999"}
        hmac_key = "my-secret"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(original_payload, hmac_key)

        assert verify_hmac(tampered_payload, signature, hmac_key) is False

    def test_added_field_is_detected(self):
        """Adding a field to payload after signing is detected."""
        original_payload = {"event": "task.started"}
        modified_payload = {"event": "task.started", "extra": "value"}
        hmac_key = "key-123"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(original_payload, hmac_key)

        assert verify_hmac(modified_payload, signature, hmac_key) is False

    def test_removed_field_is_detected(self):
        """Removing a field from payload after signing is detected."""
        original_payload = {"event": "task.completed", "task_id": "t-1"}
        modified_payload = {"event": "task.completed"}
        hmac_key = "key-456"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(original_payload, hmac_key)

        assert verify_hmac(modified_payload, signature, hmac_key) is False

    def test_modified_nested_value_is_detected(self):
        """Modifying a nested value in payload is detected."""
        original_payload = {
            "kind": "artifact",
            "artifact": {"name": "original"},
        }
        modified_payload = {
            "kind": "artifact",
            "artifact": {"name": "modified"},
        }
        hmac_key = "nested-key"

        from hermes_agent_a2a.push_delivery import PushDelivery
        pusher = PushDelivery()
        signature = pusher._sign(original_payload, hmac_key)

        assert verify_hmac(modified_payload, signature, hmac_key) is False


class TestVerifyHMACMissingSignature:
    """verify_hmac handles missing/empty X-Hub-Signature-256 header gracefully."""

    def test_missing_signature_header_returns_false(self):
        """None signature (missing header) is rejected."""
        payload = {"event": "test"}
        hmac_key = "any-key"

        assert verify_hmac(payload, None, hmac_key) is False

    def test_empty_string_signature_returns_false(self):
        """Empty string signature is rejected."""
        payload = {"event": "test"}
        hmac_key = "any-key"

        assert verify_hmac(payload, "", hmac_key) is False

    def test_whitespace_only_signature_returns_false(self):
        """Whitespace-only signature is rejected."""
        payload = {"event": "test"}
        hmac_key = "any-key"

        assert verify_hmac(payload, "   ", hmac_key) is False

    def test_invalid_format_signature_returns_false(self):
        """Signature without sha256= prefix is rejected."""
        payload = {"event": "test"}
        hmac_key = "any-key"

        # This is the raw hexdigest without the sha256= prefix
        import hashlib
        import hmac as _hmac
        import json
        raw_sig = _hmac.new(
            hmac_key.encode(),
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(),
            hashlib.sha256,
        ).hexdigest()

        assert verify_hmac(payload, raw_sig, hmac_key) is False

    def test_wrong_prefix_signature_returns_false(self):
        """Signature with wrong algorithm prefix is rejected."""
        payload = {"event": "test"}
        hmac_key = "any-key"

        # sha1= prefix instead of sha256=
        wrong_prefix_sig = "sha1=" + "a" * 64

        assert verify_hmac(payload, wrong_prefix_sig, hmac_key) is False