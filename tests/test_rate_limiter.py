"""Unit tests for the token-bucket RateLimiter (threading implementation)."""

import threading
import time
import pytest

from hermes_agent_a2a.rate_limiter import RateLimiter, RateLimitConfig


def _limiter(**kw):
    cfg = RateLimitConfig(
        enabled=True,
        requests_per_window=kw.pop("requests_per_window", 100),
        window_seconds=kw.pop("window_seconds", 60),
        burst_multiplier=kw.pop("burst_multiplier", 2.0),
        header_name=kw.pop("header_name", "X-Forwarded-For"),
        cleanup_interval_seconds=kw.pop("cleanup_interval_seconds", 300),
        max_entries=kw.pop("max_entries", 10000),
    )
    return RateLimiter(config=cfg)


class TestTokenBucket:
    def test_allow_returns_tuple(self):
        limiter = _limiter(requests_per_window=10)
        allowed, retry = limiter.allow("a")
        assert allowed is True
        assert retry == 0
        assert limiter.requests_allowed == 1

    def test_independent_buckets(self):
        limiter = _limiter(requests_per_window=10)
        for _ in range(10):
            assert limiter.allow("a")[0] is True
        for _ in range(10):
            assert limiter.allow("b")[0] is True

    def test_burst_allowance(self):
        limiter = _limiter(requests_per_window=10, burst_multiplier=2.0)
        for i in range(20):
            assert limiter.allow("a")[0] is True, f"burst {i+1}"
        allowed, retry = limiter.allow("a")
        assert allowed is False
        assert retry > 0

    def test_exhaustion_blocks(self):
        limiter = _limiter(requests_per_window=5, burst_multiplier=1.0)
        for _ in range(5):
            assert limiter.allow("a")[0] is True
        allowed, retry = limiter.allow("a")
        assert allowed is False
        assert retry > 0
        assert limiter.requests_blocked == 1

    def test_retry_after_positive(self):
        limiter = _limiter(requests_per_window=1, burst_multiplier=1.0)
        assert limiter.allow("a")[0] is True
        allowed, retry = limiter.allow("a")
        assert allowed is False
        assert retry >= 1

    def test_refill_over_time(self):
        # 2 tokens per second, burst=1 => 2 total tokens
        limiter = _limiter(requests_per_window=2, window_seconds=1,
                           burst_multiplier=1.0)
        assert limiter.allow("a")[0] is True
        assert limiter.allow("a")[0] is True
        assert limiter.allow("a")[0] is False
        time.sleep(0.6)  # ~1.2 tokens refilled
        allowed, _ = limiter.allow("a")
        assert allowed is True

    def test_metrics_counters(self):
        limiter = _limiter(requests_per_window=2, burst_multiplier=1.0)
        assert limiter.allow("a")[0] is True
        assert limiter.allow("a")[0] is True
        assert limiter.allow("a")[0] is False
        assert limiter.requests_allowed == 2
        assert limiter.requests_blocked == 1


class TestDisabled:
    def test_always_true(self):
        limiter = RateLimiter(config=RateLimitConfig(enabled=False))
        for _ in range(1000):
            allowed, retry = limiter.allow("x")
            assert allowed is True
            assert retry == 0

    def test_active_entries_zero(self):
        limiter = RateLimiter(config=RateLimitConfig(enabled=False))
        limiter.allow("x")
        assert limiter.active_entries == 0

    def test_cleanup_lifecycle(self):
        limiter = RateLimiter(config=RateLimitConfig(enabled=False))
        limiter.start_cleanup()
        limiter.stop_cleanup()


class TestEntryEviction:
    def test_active_entries_grows(self):
        limiter = _limiter(requests_per_window=10)
        for cid in ("a", "b", "c"):
            limiter.allow(cid)
        assert limiter.active_entries == 3

    def test_cleanup_sweeps_stale(self):
        limiter = _limiter(requests_per_window=100, window_seconds=1,
                           max_entries=10000)
        limiter.allow("client-a")
        limiter.allow("client-b")
        assert limiter.active_entries == 2
        time.sleep(5)
        limiter._cleanup_sweep()
        assert limiter.active_entries == 0

    def test_max_entries_enforced(self):
        limiter = _limiter(requests_per_window=10, max_entries=3)
        for i in range(5):
            limiter.allow(f"c-{i}")
        assert limiter.active_entries <= 3

    def test_evict_oldest(self):
        limiter = _limiter(requests_per_window=10, max_entries=3)
        limiter.allow("first")
        limiter.allow("second")
        limiter.allow("third")
        assert limiter.active_entries == 3
        limiter.allow("fourth")
        assert limiter.active_entries == 3
        with limiter._lock:
            assert "fourth" in limiter._buckets


class TestCleanupLifecycle:
    def test_start_stop(self):
        limiter = _limiter(cleanup_interval_seconds=300)
        limiter.start_cleanup()
        assert limiter._cleanup_thread is not None
        assert limiter._cleanup_thread.is_alive()
        limiter.stop_cleanup()
        assert limiter._cleanup_thread is None

    def test_double_start_idempotent(self):
        limiter = _limiter(cleanup_interval_seconds=300)
        limiter.start_cleanup()
        t1 = limiter._cleanup_thread
        limiter.start_cleanup()
        t2 = limiter._cleanup_thread
        assert t1 is t2
        limiter.stop_cleanup()


class TestHeaderConfig:
    def test_header_name_stored(self):
        cfg = RateLimitConfig(header_name="X-Custom-For")
        assert cfg.header_name == "X-Custom-For"

    def test_config_property(self):
        limiter = _limiter(header_name="X-Proxy")
        assert limiter.config.header_name == "X-Proxy"


class TestConcurrencySafety:
    def test_concurrent_allows_under_lock(self):
        import random
        limiter = _limiter(requests_per_window=1000, burst_multiplier=2.0)
        errors = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(100):
                cid = f"client-{random.randint(0, 9)}"
                try:
                    limiter.allow(cid)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert limiter.requests_allowed + limiter.requests_blocked == 800
