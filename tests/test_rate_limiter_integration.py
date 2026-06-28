"""Integration tests for rate limiting at the HTTP level.

Spawns a real A2AServer, sends HTTP requests, and verifies 429 behaviour,
Retry-After header, JSON error body shape, and that non-task endpoints
are unaffected.
"""

import json
import threading
import time
import urllib.request
import urllib.error
import pytest

from hermes_agent_a2a.rate_limiter import RateLimiter, RateLimitConfig
from hermes_agent_a2a.server import A2AServer


def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def a2a_server():
    """Start an A2AServer with rate limiting enabled, yield (host, port, server)."""
    cfg = RateLimitConfig(
        enabled=True,
        requests_per_window=5,
        window_seconds=60,
        burst_multiplier=1.0,
        header_name="X-Forwarded-For",
        cleanup_interval_seconds=300,
        max_entries=10000,
    )
    host = "127.0.0.1"
    port = _find_free_port()
    server = A2AServer(host, port, rate_limit_config=cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert _wait_for_server(host, port), "server failed to start within timeout"
    yield host, port, server
    server.limiter.stop_cleanup()
    server.shutdown()
    thread.join(timeout=2)


def _post(host, port, path, body, headers=None):
    """Send a POST request and return (status, body_dict, response_headers)."""
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer test-token")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        body_bytes = resp.read()
        return resp.status, json.loads(body_bytes) if body_bytes else {}, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        return e.code, json.loads(body_bytes) if body_bytes else {}, dict(e.headers)


def _get(host, port, path):
    """Send a GET request and return (status, body_dict)."""
    url = f"http://{host}:{port}{path}"
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        body_bytes = resp.read()
        return resp.status, json.loads(body_bytes) if body_bytes else {}
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        return e.code, json.loads(body_bytes) if body_bytes else {}


def _wait_for_server(host, port, timeout=3.0):
    """Poll the server until it accepts connections or timeout expires."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect((host, port))
            sock.close()
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    return False

def _send_message_body(text="Hello"):
    return {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "role": 1,
                "parts": [{"text": text}],
            }
        },
        "id": "test-1",
    }


class TestRateLimitExceeded:
    def test_429_after_burst_exhausted(self, a2a_server):
        host, port, server = a2a_server
        for i in range(5):
            status, body, headers = _post(host, port, "/", _send_message_body(f"msg-{i}"))
            assert status == 200, f"request {i} should be allowed, got {status}: {body}"
        status, body, headers = _post(host, port, "/", _send_message_body("blocked"))
        assert status == 429, f"expected 429, got {status}: {body}"

    def test_429_includes_retry_after_header(self, a2a_server):
        host, port, server = a2a_server
        for _ in range(5):
            _post(host, port, "/", _send_message_body())
        status, body, headers = _post(host, port, "/", _send_message_body())
        assert status == 429
        assert "Retry-After" in headers
        retry_after = int(headers["Retry-After"])
        assert retry_after >= 1

    def test_429_json_error_body_shape(self, a2a_server):
        host, port, server = a2a_server
        for _ in range(5):
            _post(host, port, "/", _send_message_body())
        status, body, headers = _post(host, port, "/", _send_message_body())
        assert status == 429
        assert body.get("jsonrpc") == "2.0"
        assert body["error"]["code"] == -32603
        assert "Rate limit exceeded" in body["error"]["message"]
        assert body["error"]["data"] is not None
        assert "retryAfter" in body["error"]["data"]

    def test_different_clients_have_separate_buckets(self, a2a_server):
        host, port, server = a2a_server
        for i in range(5):
            status, _, _ = _post(host, port, "/", _send_message_body(f"a-{i}"),
                                 headers={"X-Forwarded-For": "10.0.0.1"})
            assert status == 200
        status, body, _ = _post(host, port, "/", _send_message_body("a-blocked"),
                                headers={"X-Forwarded-For": "10.0.0.1"})
        assert status == 429
        for i in range(5):
            status, _, _ = _post(host, port, "/", _send_message_body(f"b-{i}"),
                                 headers={"X-Forwarded-For": "10.0.0.2"})
            assert status == 200, f"client B request {i} should succeed"

    def test_x_forwarded_for_first_ip_used(self, a2a_server):
        host, port, server = a2a_server
        for i in range(5):
            status, _, _ = _post(
                host, port, "/", _send_message_body(f"chain-{i}"),
                headers={"X-Forwarded-For": "10.1.1.1, 10.2.2.2, 10.3.3.3"},
            )
            assert status == 200
        status, _, _ = _post(
            host, port, "/", _send_message_body("chain-blocked"),
            headers={"X-Forwarded-For": "10.1.1.1, 10.2.2.2, 10.3.3.3"},
        )
        assert status == 429
        status, _, _ = _post(
            host, port, "/", _send_message_body("different-chain"),
            headers={"X-Forwarded-For": "10.9.9.9, 10.2.2.2"},
        )
        assert status == 200


class TestNonTaskEndpointsUnaffected:
    def test_agent_card_not_rate_limited(self, a2a_server):
        host, port, server = a2a_server
        for _ in range(5):
            _post(host, port, "/", _send_message_body())
        status, body = _get(host, port, "/.well-known/agent.json")
        assert status == 200
        assert "agentId" in body

    def test_health_not_rate_limited(self, a2a_server):
        host, port, server = a2a_server
        for _ in range(5):
            _post(host, port, "/", _send_message_body())
        status, body = _get(host, port, "/health")
        assert status == 200
        assert body.get("status") == "ok"

    def test_list_tasks_get_not_rate_limited(self, a2a_server):
        host, port, server = a2a_server
        for _ in range(5):
            _post(host, port, "/", _send_message_body())
        status, body = _get(host, port, "/tasks")
        assert status == 200
        assert "task" in body

    def test_get_task_not_rate_limited(self, a2a_server):
        host, port, server = a2a_server
        for _ in range(5):
            _post(host, port, "/", _send_message_body())
        status, body = _get(host, port, "/tasks/nonexistent-task-id")
        assert status == 404


def _make_server_with_cfg(cfg):
    host = "127.0.0.1"
    port = _find_free_port()
    return A2AServer(host, port, rate_limit_config=cfg)


class TestCleanupLifecycle:
    def test_cleanup_task_spawned_on_server_init(self):
        cfg = RateLimitConfig(enabled=True)
        server = _make_server_with_cfg(cfg)
        try:
            assert server.limiter._cleanup_thread is not None
            assert server.limiter._cleanup_thread.is_alive()
        finally:
            server.limiter.stop_cleanup()
            server.server_close()

    def test_cleanup_stops_on_shutdown(self):
        cfg = RateLimitConfig(enabled=True)
        server = _make_server_with_cfg(cfg)
        try:
            assert server.limiter._cleanup_thread is not None
            server.limiter.stop_cleanup()
            assert server.limiter._cleanup_thread is None
        finally:
            server.server_close()

    def test_cleanup_runs_periodically(self):
        cfg = RateLimitConfig(enabled=True, cleanup_interval_seconds=1)
        limiter = RateLimiter(config=cfg)
        limiter.start_cleanup()
        time.sleep(1.5)
        # Cleanup loop should have executed at least once
        limiter.stop_cleanup()
        assert limiter._cleanup_thread is None


class TestConcurrentAccess:
    def test_concurrent_requests_respect_limit(self):
        import concurrent.futures
        cfg = RateLimitConfig(
            enabled=True, requests_per_window=10,
            window_seconds=60, burst_multiplier=1.0,
        )
        host = "127.0.0.1"
        port = _find_free_port()
        server = A2AServer(host, port, rate_limit_config=cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert _wait_for_server(host, port), "server failed to start within timeout"
        try:
            def _req(i):
                url = f"http://{host}:{port}/"
                body = json.dumps(_send_message_body(f"concurrent-{i}")).encode()
                req = urllib.request.Request(url, data=body, method="POST")
                req.add_header("Content-Type", "application/json")
                try:
                    resp = urllib.request.urlopen(req, timeout=10)
                    return resp.status, True
                except urllib.error.HTTPError as e:
                    return e.code, False
                except urllib.error.URLError:
                    return 0, False  # timeout or connection error
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                futures = [pool.submit(_req, i) for i in range(30)]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]
            allowed_count = sum(1 for status, ok in results if status == 200)
            blocked_count = sum(1 for status, ok in results if status == 429)
            assert allowed_count <= 10, f"too many allowed: {allowed_count}"
            assert blocked_count >= 20, f"too few blocked: {blocked_count}"
            assert allowed_count + blocked_count == 30,                 f"unexpected status codes: {set(s for s, _ in results)}"
        finally:
            server.limiter.stop_cleanup()
            server.shutdown()
            thread.join(timeout=2)


class TestDisabledBackwardCompatible:
    def test_disabled_rate_limit_never_blocks(self):
        cfg = RateLimitConfig(enabled=False)
        host = "127.0.0.1"
        port = _find_free_port()
        server = A2AServer(host, port, rate_limit_config=cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert _wait_for_server(host, port), "server failed to start within timeout"
        try:
            for i in range(50):
                status, body, _ = _post(host, port, "/", _send_message_body(f"msg-{i}"))
                assert status != 429, f"unexpected 429 on request {i}: {body}"
        finally:
            server.limiter.stop_cleanup()
            server.shutdown()
            thread.join(timeout=2)
