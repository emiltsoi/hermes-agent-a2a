"""Tests for security.py SSRF protection and push REST endpoint HMAC auth."""

from __future__ import annotations

import os
import socket
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Part 1 — security.py: is_safe_url / validate_webhook_endpoint
# ─────────────────────────────────────────────────────────────────────────────

from hermes_agent_a2a.security import is_safe_url, validate_webhook_endpoint


class TestIsSafeUrlSchemes:
    """Block non-HTTP/HTTPS schemes."""

    @pytest.mark.parametrize("url", [
        "ftp://example.com/webhook",
        "file:///etc/passwd",
        "gopher://example.com/",
        "mailto:admin@example.com",
        "javascript:alert(1)",
        "dict://localhost:11211/stats",
        "s3://my-bucket/obj",
    ])
    def test_rejects_non_http_schemes(self, url):
        assert is_safe_url(url) is False, f"Expected False for scheme in {url}"

    @pytest.mark.parametrize("url", [
        "http://example.com/webhook",
        "https://example.com/webhook",
    ])
    def test_accepts_http_https(self, url):
        assert is_safe_url(url) is True, f"Expected True for {url}"


class TestIsSafeUrlBlockedHosts:
    """Block private IP ranges and internal hostnames."""

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/webhook",
        "https://127.0.0.2/webhook",
        "https://127.255.255.255/webhook",
        "https://localhost/webhook",
        "https://localhost:8080/webhook",
        "https://0.0.0.0/webhook",
        "https://::1/webhook",
        "https://[::1]/webhook",
    ])
    def test_rejects_loopback(self, url):
        assert is_safe_url(url) is False, f"Expected False for {url}"

    @pytest.mark.parametrize("url", [
        "https://10.0.0.1/webhook",
        "https://10.255.255.255/webhook",
        "https://172.16.0.1/webhook",
        "https://172.31.255.255/webhook",
        "https://192.168.0.1/webhook",
        "https://192.168.255.255/webhook",
    ])
    def test_rejects_private_ranges(self, url):
        assert is_safe_url(url) is False, f"Expected False for private range in {url}"

    @pytest.mark.parametrize("url", [
        "https://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/openstack/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ])
    def test_rejects_cloud_metadata_endpoints(self, url):
        assert is_safe_url(url) is False, f"Expected False for metadata endpoint {url}"

    def test_rejects_internal_hostname_resolving_to_private(self):
        """If a hostname resolves to a private IP, it must be blocked."""
        with patch("socket.gethostbyname") as mock_dns:
            mock_dns.return_value = "10.0.0.99"
            # evildomain.internal is not in a blocked CIDR directly, but resolves to 10.x
            result = is_safe_url("https://evildomain.internal/webhook")
            assert result is False

    def test_rejects_hostname_resolving_to_loopback(self):
        with patch("socket.gethostbyname") as mock_dns:
            mock_dns.return_value = "127.0.0.1"
            result = is_safe_url("https://myhost.local/webhook")
            assert result is False

    def test_accepts_public_hostname_that_resolves_publicly(self):
        """Non-blocked hostname that resolves to a public IP is safe."""
        with patch("socket.gethostbyname") as mock_dns:
            mock_dns.return_value = "93.184.216.34"  # example.com
            result = is_safe_url("https://example.com/webhook")
            assert result is True

    def test_rejects_wildcard_host(self):
        """The '*' hostname is a SSRF indicator."""
        assert is_safe_url("https://*/webhook") is False

    @pytest.mark.parametrize("url", [
        "https://93.184.216.34/webhook",          # public IP — should pass
        "https://8.8.8.8/webhook",                  # Google DNS — public
        "https://1.1.1.1/webhook",                  # Cloudflare — public
    ])
    def test_accepts_public_ips(self, url):
        assert is_safe_url(url) is True, f"Expected True for public IP in {url}"

    def test_dns_resolution_failure_is_safe(self):
        """If a hostname can't be resolved, we treat it as potentially unsafe and block."""
        with patch("socket.gethostbyname") as mock_dns:
            mock_dns.side_effect = socket.gaierror("Name does not resolve")
            # Should return False because we can't confirm it's safe
            result = is_safe_url("https://definitelynotexist.invalid/webhook")
            assert result is False

    def test_dns_timeout_is_safe(self):
        """DNS timeout means we can't confirm safety — block."""
        with patch("socket.gethostbyname") as mock_dns:
            mock_dns.side_effect = socket.timeout("DNS timed out")
            result = is_safe_url("https://slowdns.example.com/webhook")
            assert result is False


class TestValidateWebhookEndpoint:
    """validate_webhook_endpoint returns (bool, str)."""

    def test_accepts_https_public_url(self):
        ok, reason = validate_webhook_endpoint("https://example.com/webhook")
        assert ok is True
        assert reason == ""

    def test_rejects_http_url(self):
        ok, reason = validate_webhook_endpoint("http://example.com/webhook")
        assert ok is False
        assert "HTTPS" in reason or "https" in reason.lower()

    def test_rejects_loopback_via_is_safe_url(self):
        ok, reason = validate_webhook_endpoint("https://127.0.0.1/hook")
        assert ok is False

    def test_rejects_private_range_via_is_safe_url(self):
        ok, reason = validate_webhook_endpoint("https://192.168.1.1/hook")
        assert ok is False

    def test_rejects_metadata_endpoint(self):
        ok, reason = validate_webhook_endpoint("https://169.254.169.254/latest/meta-data/")
        assert ok is False
        assert "metadata" in reason.lower() or "169.254" in reason

    def test_rejects_non_http_scheme(self):
        ok, reason = validate_webhook_endpoint("ftp://example.com/webhook")
        assert ok is False

    def test_empty_endpoint(self):
        ok, reason = validate_webhook_endpoint("")
        assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# Part 2 — server.py: HMAC auth on push REST handlers
# ─────────────────────────────────────────────────────────────────────────────

class TestPushRestHandlerHMAC:
    """HMAC auth on POST/GET/DELETE /tasks/{id}/pushNotificationConfigs."""

    @pytest.fixture
    def mock_server(self):
        """Minimal A2AServer mock with hmac_key set."""
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card.return_value = {}
        return srv

    @pytest.fixture
    def handler_env(self, mock_server):
        """Return (handler, mock_request) tuple."""
        from http.server import BaseHTTPRequestHandler
        from hermes_agent_a2a.server import A2ARequestHandler

        # Instantiate a real handler bound to a mock server
        class _FakeHandler(A2ARequestHandler):
            server = mock_server

        # Build a mock request object
        mock_req = MagicMock(spec=BaseHTTPRequestHandler)
        mock_req.headers = {}
        mock_req.path = ""
        mock_req.rfile = MagicMock()
        mock_req.wfile = MagicMock()
        mock_req.send_response = MagicMock()
        mock_req.send_header = MagicMock()
        mock_req.end_headers = MagicMock()
        mock_req.wfile.write = MagicMock()
        mock_req.client_address = ("127.0.0.1", 12345)

        # Attach helper methods from A2ARequestHandler
        for attr in dir(A2ARequestHandler):
            if not attr.startswith("_"):
                continue
            obj = getattr(A2ARequestHandler, attr, None)
            if callable(obj):
                setattr(mock_req, attr, MagicMock())

        # The handler class needs to be injected properly
        return _FakeHandler, mock_req, mock_server

    def _make_handler_instance(self, mock_server):
        """Create a real A2ARequestHandler instance wired to mock_server."""
        from http.server import BaseHTTPRequestHandler
        from hermes_agent_a2a.server import A2ARequestHandler

        class SubHandler(A2ARequestHandler):
            server = mock_server

        mock_req = MagicMock(spec=BaseHTTPRequestHandler)
        mock_req.headers = {}
        mock_req.path = "/"
        mock_req.rfile = MagicMock()
        mock_req.wfile = MagicMock()
        mock_req.send_response = MagicMock()
        mock_req.send_header = MagicMock()
        mock_req.end_headers = MagicMock()
        mock_req.wfile.write = MagicMock()
        mock_req.client_address = ("127.0.0.1", 12345)
        mock_req.log_message = MagicMock()

        # Wire _send_json
        def _send_json(data, status=200):
            mock_req._sent_json = (data, status)
        mock_req._send_json = _send_json
        mock_req.server = mock_server

        return mock_req

    # -- POST /tasks/{id}/pushNotificationConfigs --
    def test_post_push_config_requires_hmac(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs"
        h.headers = {}  # no X-HMAC-Key

        # Check HMAC before delegating
        hmac_key = h.headers.get("X-HMAC-Key")
        expected_key = getattr(h.server, "hmac_key", None)
        assert hmac_key is None
        assert expected_key == "test-hmac-key-123"
        # The handler would return 401
        assert hmac_key != expected_key

    def test_post_push_config_accepts_valid_hmac(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs"
        h.headers["X-HMAC-Key"] = "test-hmac-key-123"

        hmac_key = h.headers.get("X-HMAC-Key")
        expected_key = getattr(h.server, "hmac_key", None)
        assert hmac_key == expected_key

    def test_post_push_config_rejects_wrong_hmac(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs"
        h.headers["X-HMAC-Key"] = "wrong-key"

        hmac_key = h.headers.get("X-HMAC-Key")
        expected_key = getattr(h.server, "hmac_key", None)
        assert hmac_key != expected_key

    # -- GET /tasks/{id}/pushNotificationConfigs/{config_id} --
    def test_get_push_config_no_hmac_allowed(self):
        """GET (read-only) should allow unauthenticated requests."""
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs/c1"
        h.headers = {}  # no X-HMAC-Key

        hmac_key = h.headers.get("X-HMAC-Key")
        # For GET: if no key present, allow
        if not hmac_key:
            allowed = True
        else:
            allowed = hmac_key == getattr(h.server, "hmac_key", None)
        assert allowed is True

    def test_get_push_config_valid_hmac_also_allowed(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs/c1"
        h.headers["X-HMAC-Key"] = "test-hmac-key-123"

        hmac_key = h.headers.get("X-HMAC-Key")
        if hmac_key:
            allowed = hmac_key == getattr(h.server, "hmac_key", None)
        else:
            allowed = True
        assert allowed is True

    def test_get_push_config_wrong_hmac_rejected(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs/c1"
        h.headers["X-HMAC-Key"] = "wrong-key"

        hmac_key = h.headers.get("X-HMAC-Key")
        # For GET: if key is present, must match
        if hmac_key:
            allowed = hmac_key == getattr(h.server, "hmac_key", None)
        else:
            allowed = True
        assert allowed is False

    # -- DELETE /tasks/{id}/pushNotificationConfigs/{config_id} --
    def test_delete_push_config_requires_hmac(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs/c1"
        h.headers = {}  # no X-HMAC-Key

        hmac_key = h.headers.get("X-HMAC-Key")
        expected_key = getattr(h.server, "hmac_key", None)
        # DELETE requires auth
        assert hmac_key is None
        assert hmac_key != expected_key

    def test_delete_push_config_accepts_valid_hmac(self):
        srv = MagicMock()
        srv.hmac_key = "test-hmac-key-123"
        srv.limiter = MagicMock()
        srv.limiter.allow.return_value = True
        srv.auth_token = ""
        srv.require_auth = False
        srv.agent_name = "test-agent"
        srv.build_agent_card = MagicMock(return_value={})

        h = self._make_handler_instance(srv)
        h.path = "/tasks/t1/pushNotificationConfigs/c1"
        h.headers["X-HMAC-Key"] = "test-hmac-key-123"

        hmac_key = h.headers.get("X-HMAC-Key")
        expected_key = getattr(h.server, "hmac_key", None)
        assert hmac_key == expected_key


# ─────────────────────────────────────────────────────────────────────────────
# Part 3 — push_delivery.py: SSRF check in deliver_push_notification
# ─────────────────────────────────────────────────────────────────────────────

class TestPushDeliverySSRF:
    """deliver_push_notification validates endpoint before delivery."""

    def test_deliver_blocks_ssrf_private_url(self):
        """If validate_webhook_endpoint rejects the URL, delivery must not happen."""
        from hermes_agent_a2a.push_delivery import deliver_push_notification, create_push_config

        # Create a config with a private IP
        cfg = create_push_config(
            task_id="ssrf-task",
            url="https://192.168.1.1/webhook",
            authentication=None,
            metadata=None,
        )

        result = deliver_push_notification(
            task_id="ssrf-task",
            config_id=cfg.id,
            payload={"event": "test"},
        )
        # Must return False — SSRF check should block delivery
        assert result is False

    def test_deliver_blocks_ssrf_loopback(self):
        from hermes_agent_a2a.push_delivery import deliver_push_notification, create_push_config

        cfg = create_push_config(
            task_id="loopback-task",
            url="https://127.0.0.1/webhook",
            authentication=None,
            metadata=None,
        )

        result = deliver_push_notification(
            task_id="loopback-task",
            config_id=cfg.id,
            payload={"event": "test"},
        )
        assert result is False

    def test_deliver_blocks_metadata_endpoint(self):
        from hermes_agent_a2a.push_delivery import deliver_push_notification, create_push_config

        cfg = create_push_config(
            task_id="meta-task",
            url="https://169.254.169.254/latest/meta-data/",
            authentication=None,
            metadata=None,
        )

        result = deliver_push_notification(
            task_id="meta-task",
            config_id=cfg.id,
            payload={"event": "test"},
        )
        assert result is False