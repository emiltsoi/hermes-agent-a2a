"""Webhook push notification subscriptions.

Stores subscriptions: task_id → (url, hmac_key, created_at).
Triggered by TaskStateChangeHook on task state transitions.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional


@dataclass
class Subscription:
    """A push notification subscription for a task."""
    subscription_id: str
    task_id: str
    url: str
    hmac_key: str
    created_at: float = field(default_factory=time.time)


class SubscriptionStore:
    """Thread-safe store for push notification subscriptions.

    Contract:
        add(task_id: str, url: str, hmac_key: str) → str  # returns subscription_id
        remove(subscription_id: str) → bool
        get(task_id: str) → list[Subscription]
    """

    def __init__(self):
        self._store: dict[str, Subscription] = {}  # subscription_id → Subscription
        self._by_task: dict[str, set[str]] = {}    # task_id → set of subscription_ids
        self._lock = Lock()

    def add(self, task_id: str, url: str, hmac_key: str) -> str:
        """Register a new subscription for a task.

        Returns the new subscription_id.
        """
        with self._lock:
            sub_id = str(uuid.uuid4())
            sub = Subscription(
                subscription_id=sub_id,
                task_id=task_id,
                url=url,
                hmac_key=hmac_key,
            )
            self._store[sub_id] = sub
            self._by_task.setdefault(task_id, set()).add(sub_id)
            return sub_id

    def remove(self, subscription_id: str) -> bool:
        """Remove a subscription by ID.

        Returns True if the subscription existed, False otherwise.
        """
        with self._lock:
            sub = self._store.pop(subscription_id, None)
            if sub is None:
                return False
            self._by_task.get(sub.task_id, set()).discard(subscription_id)
            return True

    def get(self, task_id: str) -> list[Subscription]:
        """Get all active subscriptions for a task.

        Returns a list of Subscription objects (may be empty).
        """
        with self._lock:
            sub_ids = self._by_task.get(task_id, set())
            return [self._store[sid] for sid in sub_ids if sid in self._store]


# Module-level singleton (lazy)
_store: Optional[SubscriptionStore] = None
_store_lock = Lock()


def get_subscription_store() -> SubscriptionStore:
    """Return the module-level SubscriptionStore singleton."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SubscriptionStore()
        return _store