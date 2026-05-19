"""T3-4 — Push silent failure logging in hooks.py.

Tests that TaskStateChangeHook logs push delivery failures at WARNING level
with task_id and subscription context (not silently swallowed at DEBUG).
"""
from unittest.mock import MagicMock, patch

import pytest

from hermes_agent_a2a.hooks import TaskStateChangeHook
from hermes_agent_a2a.subscription_store import Subscription


class TestPushFailureLogging:
    """Push delivery failures must be logged at WARNING level, not DEBUG."""

    def _make_hook(self, subs, pusher_return_ok=False):
        """Build a hook with injectable subscription_store and push_delivery."""
        hook = TaskStateChangeHook()
        store = MagicMock()
        store.get.return_value = subs
        hook._subscription_store = store
        pusher = MagicMock()
        pusher.deliver_with_retry.return_value = pusher_return_ok
        hook._push_delivery = pusher
        return hook, pusher

    def test_delivery_failure_is_logged_at_warning(self, caplog):
        """When deliver_with_retry returns False, a WARNING must be emitted."""
        sub = Subscription(
            subscription_id="sub-1",
            task_id="task-abc",
            url="https://example.com/hook",
            hmac_key="secret",
        )
        hook, pusher = self._make_hook([sub])

        hook.on_state_change("task-abc", "working", "completed")

        pusher.deliver_with_retry.assert_called_once()
        # Must be WARNING, not DEBUG
        assert caplog.record_tuples, "Expected at least one log record"
        levels = [r.levelno for r in caplog.records]
        assert any(lvl >= 30 for lvl in levels), (
            f"Expected WARNING (30) or higher, got levels={levels}"
        )

    def test_delivery_failure_log_contains_task_id(self, caplog):
        """The failure log must include the task_id."""
        sub = Subscription(
            subscription_id="sub-2",
            task_id="task-xyz",
            url="https://webhook.example.com/push",
            hmac_key="hmac-secret",
        )
        hook, pusher = self._make_hook([sub])

        hook.on_state_change("task-xyz", "submitted", "working")

        pusher.deliver_with_retry.assert_called_once()
        task_ids_in_log = [
            r.message for r in caplog.records if "task-xyz" in r.message
        ]
        assert task_ids_in_log, (
            f"task_id 'task-xyz' not found in any log message: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )

    def test_delivery_failure_log_contains_url(self, caplog):
        """The failure log should include the subscription URL."""
        sub = Subscription(
            subscription_id="sub-3",
            task_id="task-123",
            url="https://myapp.io/notifications",
            hmac_key="key123",
        )
        hook, pusher = self._make_hook([sub])

        hook.on_state_change("task-123", "working", "failed")

        assert caplog.record_tuples, "Expected at least one log record"
        msgs = " ".join(r.message for r in caplog.records)
        assert "myapp.io" in msgs, f"URL not found in log messages: {msgs}"

    def test_success_is_not_logged_at_warning(self, caplog):
        """When deliver_with_retry returns True, no WARNING should be emitted."""
        sub = Subscription(
            subscription_id="sub-ok",
            task_id="task-ok",
            url="https://ok.example.com/hook",
            hmac_key="secret",
        )
        hook, pusher = self._make_hook([sub], pusher_return_ok=True)

        hook.on_state_change("task-ok", "working", "completed")

        levels = [r.levelno for r in caplog.records]
        assert not any(lvl >= 30 for lvl in levels), (
            f"Unexpected WARNING/ERROR on success: "
            f"[(r.levelname, r.message) for r in caplog.records]"
        )

    def test_multiple_failures_all_logged(self, caplog):
        """Each failed subscription should generate its own WARNING."""
        subs = [
            Subscription("sub-a", "task-multi", "https://a.com/h", "key"),
            Subscription("sub-b", "task-multi", "https://b.com/h", "key"),
        ]
        hook, pusher = self._make_hook(subs)

        hook.on_state_change("task-multi", "working", "completed")

        assert pusher.deliver_with_retry.call_count == 2
        warning_count = sum(1 for r in caplog.records if r.levelno >= 30)
        assert warning_count == 2, (
            f"Expected 2 WARNING logs, got {warning_count}: "
            f"[(r.levelname, r.message) for r in caplog.records]"
        )

    def test_hook_does_not_crash_if_pusher_raises(self):
        """If deliver_with_retry raises, the hook must not propagate the exception."""
        sub = Subscription("sub-crash", "task-crash", "https://crash.com/h", "k")
        hook = TaskStateChangeHook()
        store = MagicMock()
        store.get.return_value = [sub]
        hook._subscription_store = store
        pusher = MagicMock()
        pusher.deliver_with_retry.side_effect = RuntimeError("network is down")
        hook._push_delivery = pusher

        # Must not raise
        hook.on_state_change("task-crash", "working", "completed")

    def test_hook_does_not_crash_if_store_is_unavailable(self):
        """If get() raises, the hook must not propagate the exception."""
        hook = TaskStateChangeHook()
        store = MagicMock()
        store.get.side_effect = RuntimeError("store unavailable")
        hook._subscription_store = store
        hook._push_delivery = MagicMock()

        # Must not raise
        hook.on_state_change("task-store-down", "working", "completed")