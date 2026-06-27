"""Request-level rate limiting with token bucket algorithm.

L10 infrastructure layer — no imports from L6 config or higher layers.
Uses only Python stdlib. Thread-safe via threading.Lock (the A2A server is
a ThreadingHTTPServer, not an asyncio event loop).

Metrics:
    requests_allowed — cumulative allowed requests
    requests_blocked — cumulative blocked requests
    active_entries   — current number of tracked caller buckets
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    """Bridge from L6 config layer to L10 rate limiter.

    No direct yaml dependency — constructed from environment variables or
    dict by the L7/L8 integration layer in server.py.

    Field semantics:
        enabled:                    Whether rate limiting is active.
        requests_per_window:        Steady-state requests allowed per window_seconds.
        window_seconds:             Refill window duration.
        burst_multiplier:           Max bucket capacity = requests_per_window * burst_multiplier.
        header_name:                HTTP header used for caller identity (e.g. X-Forwarded-For).
        cleanup_interval_seconds:   How often stale entries are swept.
        max_entries:                Maximum number of tracked caller buckets.
    """

    enabled: bool = False
    requests_per_window: int = 100
    window_seconds: int = 60
    burst_multiplier: float = 2.0
    header_name: str = "X-Forwarded-For"
    cleanup_interval_seconds: int = 300
    max_entries: int = 10000


class RateLimiter:
    """Token bucket rate limiter — thread-safe, single-process only.

    Each caller gets a token bucket. Tokens refill at a steady rate.
    Burst capacity is requests_per_window * burst_multiplier.
    """

    def __init__(self, config: RateLimitConfig | None = None):
        self._config = config or RateLimitConfig()

        # Per-caller bucket: client_id -> {"tokens": float, "last_refill": float}
        self._buckets: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

        # Bare counters — exposed directly for metrics reads
        self.requests_allowed: int = 0
        self.requests_blocked: int = 0

        # Background cleanup
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_stop = threading.Event()

    # ── public API ──────────────────────────────────────────────────────

    @property
    def config(self) -> RateLimitConfig:
        """Expose the current configuration (read-only)."""
        return self._config

    @property
    def active_entries(self) -> int:
        """Number of currently tracked caller buckets."""
        with self._lock:
            return len(self._buckets)

    def allow(self, client_id: str, cost: float = 1.0) -> tuple[bool, int]:
        """Check whether *client_id* is allowed to make a request.

        Returns:
            (True, 0)      — request allowed.
            (False, secs)  — rate limited; *secs* is the suggested Retry-After delay.

        When the limiter is disabled via config, always returns (True, 0).
        """
        if not self._config.enabled:
            self.requests_allowed += 1
            return True, 0

        now = time.monotonic()
        max_tokens = float(self._config.requests_per_window) * self._config.burst_multiplier
        refill_rate = float(self._config.requests_per_window) / float(self._config.window_seconds)
        # Conservative Retry-After: time needed to accrue one full token
        retry_after = int(self._config.window_seconds / self._config.requests_per_window) + 1

        with self._lock:
            # Enforce max entry cap before insertion
            if client_id not in self._buckets and len(self._buckets) >= self._config.max_entries:
                self._evict_oldest()

            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = {"tokens": max_tokens - cost, "last_refill": now}
                self._buckets[client_id] = bucket
                self.requests_allowed += 1
                return True, 0

            # Refill
            elapsed = now - bucket["last_refill"]
            bucket["tokens"] = min(max_tokens, bucket["tokens"] + elapsed * refill_rate)
            bucket["last_refill"] = now

            if bucket["tokens"] >= cost:
                bucket["tokens"] -= cost
                self.requests_allowed += 1
                return True, 0
            else:
                self.requests_blocked += 1
                return False, retry_after

    # ── cleanup lifecycle ───────────────────────────────────────────────

    def start_cleanup(self) -> None:
        """Spawn a background daemon thread that periodically sweeps stale entries."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._cleanup_stop.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="a2a-rate-limiter-cleanup"
        )
        self._cleanup_thread.start()

    def stop_cleanup(self) -> None:
        """Signal and join the background cleanup thread."""
        self._cleanup_stop.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=2.0)
            self._cleanup_thread = None

    # ── internal ────────────────────────────────────────────────────────

    def _evict_oldest(self) -> None:
        """Evict the least-recently-refilled bucket. Caller must hold _lock."""
        if not self._buckets:
            return
        oldest_key = min(
            self._buckets.keys(),
            key=lambda k: self._buckets[k].get("last_refill", 0.0),
        )
        del self._buckets[oldest_key]

    def _cleanup_sweep(self) -> None:
        """Remove entries that have not been accessed recently.

        Staleness threshold is twice the burst duration so that callers who
        paused for a short time do not lose their bucket state.
        """
        now = time.monotonic()
        # A caller is stale if it has not been seen for 2x full burst window
        cfg = self._config
        burst_duration = (
            cfg.requests_per_window
            * cfg.burst_multiplier
            / (cfg.requests_per_window / cfg.window_seconds)
        )
        threshold = now - burst_duration * 2
        with self._lock:
            stale = [
                cid
                for cid, b in self._buckets.items()
                if b.get("last_refill", 0.0) < threshold
            ]
            for cid in stale:
                del self._buckets[cid]

    def _cleanup_loop(self) -> None:
        """Background cleanup — waits on a threading.Event with timeout."""
        interval = self._config.cleanup_interval_seconds
        while not self._cleanup_stop.wait(interval):
            try:
                self._cleanup_sweep()
            except Exception:
                logger.warning("Rate limiter cleanup sweep failed", exc_info=True)
