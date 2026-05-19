"""Push notification delivery with retry and HMAC signing.

PushDelivery delivers signed payloads to webhook URLs with exponential
backoff retry.  HMAC-SHA256 signatures are added as X-Hub-Signature-256.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
import urllib.error
import urllib.request
from typing import Optional

import httpx

from hermes_agent_a2a.a2a_spec.push import (
    AuthenticationInfo,
    TaskPushNotificationConfig,
)
from hermes_agent_a2a.security import validate_webhook_endpoint

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY = 0.5  # seconds

# Module-level httpx.Client for connection pooling (Issue 16)
_http_client: httpx.Client = httpx.Client(timeout=10.0)


def verify_hmac(payload: dict, signature: Optional[str], hmac_key: str) -> bool:
    """Verify HMAC-SHA256 signature of a JSON payload.

    Args:
        payload:   The JSON payload dict to verify.
        signature: The X-Hub-Signature-256 header value (e.g. "sha256=abc123").
                   May be None if header is missing.
        hmac_key:  The secret key used for HMAC verification.

    Returns:
        True if signature is valid and matches payload, False otherwise.
        Returns False if signature is None/empty (missing header).
    """
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        hmac_key.encode(),
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


class PushDelivery:
    """Delivers signed push notifications to subscriber webhook URLs.

    Contract:
        deliver(url: str, payload: dict, hmac_key: str) → bool
        deliver_with_retry(url: str, payload: dict, hmac_key: str, max_attempts: int = 3) → bool
    """

    def __init__(self, base_delay: float = _DEFAULT_BASE_DELAY):
        self._base_delay = base_delay

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sign(self, payload: dict, hmac_key: str) -> str:
        """Sign a JSON payload with HMAC-SHA256.

        Returns the signature in the format used by X-Hub-Signature-256.
        """
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return "sha256=" + hmac.new(
            hmac_key.encode(), body, hashlib.sha256
        ).hexdigest()

    def _build_request(self, url: str, payload: dict, hmac_key: str) -> urllib.request.Request:
        """Build a signed HTTP POST request for a push payload."""
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        sig = self._sign(payload, hmac_key)
        return urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
            method="POST",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deliver(
        self,
        url: str,
        payload: dict,
        hmac_key: str,
        timeout: float = 10.0,
    ) -> bool:
        """Deliver a signed push notification to a webhook URL.

        Signs the payload with HMAC-SHA256 and POSTs it.

        Returns True on a 2xx response, False on any failure.
        """
        try:
            body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            sig = self._sign(payload, hmac_key)
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            }
            resp = _http_client.post(url, content=body, headers=headers, timeout=timeout)
            status = resp.status_code
            if 200 <= status < 300:
                return True
            logger.warning(
                "[PushDelivery] Non-2xx delivery to %s: status=%d",
                url, status,
            )
            return False
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401 or e.response.status_code == 403:
                logger.warning(
                    "[PushDelivery] HMAC verification failed (auth rejected) for %s: HTTP %d",
                    url, e.response.status_code,
                )
            else:
                logger.warning(
                    "[PushDelivery] HTTP error delivering to %s: HTTP %d",
                    url, e.response.status_code,
                )
            return False
        except httpx.TimeoutException:
            logger.warning(
                "[PushDelivery] Timeout delivering to %s", url,
            )
            return False
        except httpx.ConnectError as e:
            logger.warning(
                "[PushDelivery] Connection error delivering to %s: %s",
                url, e,
            )
            return False
        except Exception as e:
            logger.warning(
                "[PushDelivery] Unexpected error delivering to %s: %s",
                url, e,
            )
            return False

    def deliver_with_retry(
        self,
        url: str,
        payload: dict,
        hmac_key: str,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        """Deliver with exponential backoff retry.

        Retries up to max_attempts times on non-2xx/timeout errors.
        The delay between attempts doubles each time (exponential backoff).

        Returns True if any attempt succeeds, False if all fail.
        """
        for attempt in range(max_attempts):
            if self.deliver(url, payload, hmac_key):
                return True
            # Sleep before next attempt (skip on last attempt)
            if attempt < max_attempts - 1:
                delay = self._base_delay * (2 ** attempt)
                logger.debug(
                    "[PushDelivery] Retry %d/%d for %s in %.1fs",
                    attempt + 1, max_attempts, url, delay,
                )
                time.sleep(delay)
        logger.warning(
            "[PushDelivery] All %d attempts failed for %s",
            max_attempts, url,
        )
        return False


# ---------------------------------------------------------------------------
# In-memory config store
# ---------------------------------------------------------------------------

_config_store: dict[str, TaskPushNotificationConfig] = {}
_config_store_lock = threading.Lock()


def create_push_config(
    task_id: str,
    push_transport_type: str,
    endpoint: str,
    authentication: Optional[AuthenticationInfo] = None,
    metadata: Optional[dict] = None,
) -> TaskPushNotificationConfig:
    """Create a push notification config for a task.

    Generates a unique config_id using uuid4.
    """
    config_id = str(uuid.uuid4())
    cfg = TaskPushNotificationConfig(
        id=config_id,
        task_id=task_id,
        push_transport_type=push_transport_type,
        endpoint=endpoint,
        authentication=authentication,
        metadata=metadata,
    )
    with _config_store_lock:
        _config_store[config_id] = cfg
    return cfg


def get_push_config(task_id: str, config_id: str) -> Optional[TaskPushNotificationConfig]:
    """Retrieve a single push notification config by task_id and config_id.

    Returns None if task_id has no matching config or config_id is unknown.
    """
    with _config_store_lock:
        cfg = _config_store.get(config_id)
        if cfg is not None and cfg.task_id == task_id:
            return cfg
        return None


def list_push_configs(task_id: str) -> list[TaskPushNotificationConfig]:
    """List all push notification configs registered for a task."""
    with _config_store_lock:
        return [cfg for cfg in _config_store.values() if cfg.task_id == task_id]


def delete_push_config(task_id: str, config_id: str) -> Optional[str]:
    """Delete a push notification config.

    Returns the deleted config_id on success, None if not found.
    """
    with _config_store_lock:
        cfg = _config_store.get(config_id)
        if cfg is None or cfg.task_id != task_id:
            return None
        del _config_store[config_id]
        return config_id


def deliver_push_notification(
    task_id: str,
    config_id: str,
    payload: dict,
    timeout: float = 10.0,
) -> bool:
    """Deliver a JSON payload to the configured push notification endpoint.

    Uses an httpx synchronous client to POST the payload.
    Handles httpx.TimeoutException and httpx.ConnectError gracefully,
    returning False without raising.

    Returns True on a 2xx response, False on any failure.
    """
    cfg = get_push_config(task_id, config_id)
    if cfg is None:
        logger.warning(
            "[PushDelivery] No push config found for task_id=%s config_id=%s",
            task_id, config_id,
        )
        return False

    safe, reason = validate_webhook_endpoint(cfg.endpoint)
    if not safe:
        logger.warning(
            "[PushDelivery] SSRF blocked for push config %s: %s",
            config_id, reason,
        )
        return False

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(cfg.endpoint, json=payload)
            if 200 <= resp.status_code < 300:
                return True
            logger.warning(
                "[PushDelivery] Non-2xx delivery to %s: status=%d",
                cfg.endpoint, resp.status_code,
            )
            return False
    except httpx.TimeoutException:
        logger.warning(
            "[PushDelivery] Timeout delivering to %s", cfg.endpoint,
        )
        return False
    except httpx.ConnectError as e:
        logger.warning(
            "[PushDelivery] Connection error delivering to %s: %s", cfg.endpoint, e,
        )
        return False
    except Exception as e:
        logger.warning(
            "[PushDelivery] Unexpected error delivering to %s: %s", cfg.endpoint, e,
        )
        return False


def deliver_artifact_push(
    task_id: str,
    config_id: str,
    context_id: str,
    artifact: dict,
    metadata: Optional[dict] = None,
    timeout: float = 10.0,
) -> bool:
    """Deliver a TaskArtifactUpdateEvent as a push notification.

    Builds a payload matching the TaskArtifactUpdateEvent SSE format and
    delivers it to the configured push webhook endpoint.

    Args:
        task_id:    The task that generated the artifact.
        config_id:  The push notification config ID for this subscription.
        context_id: The context ID for this task.
        artifact:   The A2A artifact dict.
        metadata:   Optional event metadata.
        timeout:    HTTP timeout in seconds.

    Returns:
        True on a 2xx response, False on any failure.
    """
    payload = {
        "kind": "artifact",
        "contextId": context_id,
        "taskId": task_id,
        "artifact": artifact,
        "metadata": metadata or {},
    }
    return deliver_push_notification(task_id, config_id, payload, timeout=timeout)


# ---------------------------------------------------------------------------
# Module-level singleton (existing PushDelivery class)
# ---------------------------------------------------------------------------

# Module-level singleton
_pusher: Optional[PushDelivery] = None
_pusher_lock = threading.Lock()


def get_push_delivery() -> PushDelivery:
    """Return the module-level PushDelivery singleton."""
    global _pusher
    with _pusher_lock:
        if _pusher is None:
            _pusher = PushDelivery()
        return _pusher