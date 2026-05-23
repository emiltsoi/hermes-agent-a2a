"""Regression tests for SEC-01: SSRF in handle_announce.

Verifies that handle_announce validates registry_url with is_safe_url()
before making any outbound HTTP request, blocking:
- Private CIDRs: 10.x, 172.16-31.x, 192.168.x, 169.254.x
- Loopback: 127.0.0.1, ::1, localhost, 0.0.0.0
- Cloud metadata: 169.254.169.254, metadata.google.internal
- Unresolvable hostnames (fail-closed)
"""

import pytest
from unittest.mock import patch, MagicMock

from hermes_agent_a2a import tool_handlers as tools


class TestHandleAnnounceSSRFBlocksPrivateIPs:
    """SSRF protection must block private IP ranges."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8081/announce",
        "http://127.0.0.1/announce",
        "http://localhost:8081/announce",
        "http://localhost/announce",
        "http://0.0.0.0:8081/announce",
        "http://0.0.0.0/announce",
        "http://10.0.0.1:8081/announce",
        "http://10.255.255.255:8081/announce",
        "http://172.16.0.1:8081/announce",
        "http://172.31.255.255:8081/announce",
        "http://192.168.0.1:8081/announce",
        "http://192.168.255.255:8081/announce",
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.169.254/announce",
    ])
    def test_rejects_private_ip_urls(self, monkeypatch, url):
        """Unsafe URLs must not reach _http_request."""
        monkeypatch.setenv("A2A_REGISTRY_URL", url)
        monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

        fake_card = {"name": "test-agent", "url": "http://test-agent:8081"}
        mock_server = MagicMock()
        mock_server.build_agent_card.return_value = fake_card

        with patch.object(tools, "_ensure_server", return_value=mock_server):
            with patch.object(tools, "_http_request") as mock_http:
                result = tools.handle_announce()

        assert result.get("announced") is False
        assert "not safe" in result.get("error", "").lower()
        mock_http.assert_not_called()


class TestHandleAnnounceSSRFBlocksMetadataEndpoints:
    """Cloud metadata endpoints must be blocked."""

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/latest/user-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata.google.internal/instance/",
    ])
    def test_rejects_metadata_endpoints(self, monkeypatch, url):
        """Metadata URLs must be rejected."""
        monkeypatch.setenv("A2A_REGISTRY_URL", url)
        monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

        fake_card = {"name": "test-agent", "url": "http://test-agent:8081"}
        mock_server = MagicMock()
        mock_server.build_agent_card.return_value = fake_card

        with patch.object(tools, "_ensure_server", return_value=mock_server):
            with patch.object(tools, "_http_request") as mock_http:
                result = tools.handle_announce()

        assert result.get("announced") is False
        mock_http.assert_not_called()


class TestHandleAnnounceSSRFAllowsSafeURLs:
    """Safe URLs must pass through to _http_request."""

    def test_allows_safe_https_url(self, monkeypatch):
        """Public HTTPS registry URL must be allowed (is_safe_url returns True)."""
        monkeypatch.setenv("A2A_REGISTRY_URL", "https://registry.example.com/announce")
        monkeypatch.delenv("A2A_REGISTRY_AUTH_TOKEN", raising=False)

        fake_card = {"name": "test-agent", "url": "http://test-agent:8081"}
        mock_server = MagicMock()
        mock_server.build_agent_card.return_value = fake_card
        registry_response = {"status": "ok"}

        with patch.object(tools, "_ensure_server", return_value=mock_server):
            with patch.object(tools, "_http_request", return_value=registry_response) as mock_http:
                with patch("hermes_agent_a2a.security.is_safe_url", return_value=True):
                    result = tools.handle_announce()

        assert result.get("announced") is True
        mock_http.assert_called_once()
