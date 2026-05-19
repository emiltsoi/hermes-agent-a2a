"""Wave E Issue 26 — SubscriptionStore TTL/Cleanup tests.

Tests written to fail BEFORE implementation (TDD).
"""
import time

import pytest


class TestSubscriptionStoreTTL:
    """SubscriptionStore must support automatic cleanup of expired subscriptions."""

    def test_constructor_accepts_max_age_parameter(self):
        """SubscriptionStore(max_age=...) must be accepted."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        # Should not raise
        store = SubscriptionStore(max_age=7200.0)
        assert store._max_age == 7200.0

    def test_default_max_age_is_3600(self):
        """Default max_age should be 3600 seconds (1 hour)."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore()
        assert store._max_age == 3600.0

    def test_cleanup_expired_removes_old_subscriptions(self):
        """_cleanup_expired() must remove subscriptions older than max_age."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore(max_age=1.0)  # 1 second TTL

        # Add a subscription
        sub_id = store.add("ttl-task-1", "https://example.com/cb", "key1")

        # Should still exist
        assert len(store.get("ttl-task-1")) == 1

        # Wait for it to expire
        time.sleep(1.1)

        # _cleanup_expired should remove it
        expired_count = store._cleanup_expired()
        assert expired_count == 1
        assert len(store.get("ttl-task-1")) == 0

    def test_cleanup_expired_does_not_remove_fresh_subscriptions(self):
        """_cleanup_expired() must NOT remove subscriptions within max_age."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore(max_age=3600.0)  # 1 hour TTL

        sub_id = store.add("ttl-task-2", "https://example.com/cb", "key1")

        # Cleanup should remove nothing
        expired_count = store._cleanup_expired()
        assert expired_count == 0
        assert len(store.get("ttl-task-2")) == 1

    def test_add_triggers_lazy_cleanup_when_many_expired(self):
        """add() must call _cleanup_expired() only when >10 subscriptions are expired."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore(max_age=0.1)  # Very short TTL

        # Add 15 subscriptions - first 10 adds won't trigger cleanup
        for i in range(15):
            store.add(f"ttl-lazy-task-{i}", "https://example.com/cb", f"key{i}")

        # Wait for all to expire
        time.sleep(0.2)

        # The 16th add should trigger cleanup since >10 are expired
        store.add("ttl-lazy-trigger", "https://example.com/cb", "trigger-key")

        # After cleanup, most of the expired ones should be gone
        remaining = sum(len(store.get(f"ttl-lazy-task-{i}")) for i in range(15))
        # At least some cleanup should have happened
        assert remaining < 15, "Cleanup should have removed expired subscriptions"

    def test_cleanup_returns_count_of_removed_subscriptions(self):
        """_cleanup_expired() must return the count of removed subscriptions."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore(max_age=0.1)

        # Add several subscriptions
        for i in range(5):
            store.add(f"ttl-count-task-{i}", "https://example.com/cb", f"key{i}")

        time.sleep(0.2)

        removed = store._cleanup_expired()
        assert removed == 5

    def test_cleanup_empty_store_returns_zero(self):
        """_cleanup_expired() on empty store returns 0."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore(max_age=3600.0)
        removed = store._cleanup_expired()
        assert removed == 0

    def test_subscription_created_at_is_accessible(self):
        """Subscription.created_at must be accessible for TTL calculations."""
        from hermes_agent_a2a.subscription_store import SubscriptionStore

        store = SubscriptionStore()
        before = time.time()
        sub_id = store.add("ttl-access-task", "https://example.com/cb", "key1")
        after = time.time()

        subs = store.get("ttl-access-task")
        assert len(subs) == 1
        assert before <= subs[0].created_at <= after
