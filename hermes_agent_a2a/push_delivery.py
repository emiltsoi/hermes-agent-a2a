"""Push notification delivery with retry and HMAC signing.

PushDelivery delivers signed payloads to webhook URLs with exponential
backoff retry.  HMAC-SHA256 signatures are added as X-Hub-Signature-256.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY = 0.5  # seconds


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
            req = self._build_request(url, payload, hmac_key)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                if 200 <= status < 300:
                    return True
                logger.warning(
                    "[PushDelivery] Non-2xx delivery to %s: status=%d",
                    url, status,
                )
                return False
        except urllib.error.HTTPError as e:
            if e.code == 401 or e.code == 403:
                logger.warning(
                    "[PushDelivery] HMAC verification failed (auth rejected) for %s: HTTP %d",
                    url, e.code,
                )
            else:
                logger.warning(
                    "[PushDelivery] HTTP error delivering to %s: HTTP %d %s",
                    url, e.code, e.reason,
                )
            return False
        except urllib.error.URLError as e:
            logger.warning(
                "[PushDelivery] URL error delivering to %s: %s",
                url, e.reason,
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


# Module-level singleton
_pusher: Optional[PushDelivery] = None
_pusher_lock = __import__("threading").Lock()


def get_push_delivery() -> PushDelivery:
    """Return the module-level PushDelivery singleton."""
    global _pusher
    with _pusher_lock:
        if _pusher is None:
            _pusher = PushDelivery()
        return _pusher