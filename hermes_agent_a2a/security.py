"""A2A security utilities — prompt injection filtering, redaction, audit, SSRF protection, JWS signing."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import urlparse

import jwt  # PyJWT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JWS signing and verification — A2A message authentication
# ---------------------------------------------------------------------------

_JWS_ALGORITHM = "HS256"
_JWS_EXPIRATION_SECONDS = 5 * 60  # 5 minutes


def sign_payload(payload: dict, secret_key: str) -> str:
    """Sign a dict payload as a JWS compact-serialized token.

    Includes ``iat`` (issued-at) and ``exp`` (expiration, +5 min) claims.
    Uses HS256 explicitly to prevent algorithm confusion attacks.
    """
    now = int(time.time())
    claims = {
        **payload,
        "iat": now,
        "exp": now + _JWS_EXPIRATION_SECONDS,
    }
    return jwt.encode(claims, secret_key, algorithm=_JWS_ALGORITHM)


def verify_jwt(token: str, secret_key: str) -> tuple[bool, dict | str]:
    """Verify a JWS compact token.

    Returns:
        (True, payload_dict)  — valid HS256 token verified against the secret
        (False, error_reason) — invalid, expired, tampered, or wrong algorithm

    Security notes:
    - Explicitly uses ``jwt.algorithms.HS256`` to reject algorithm confusion
      attacks (e.g. a token that declares ``alg: none`` or ``alg: RS256``).
    - PyJWT performs constant-time signature comparison internally.
    """
    try:
        options = {"verify_signature": True}
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[_JWS_ALGORITHM],
            options=options,
        )
        return True, dict(payload)
    except jwt.ExpiredSignatureError:
        return False, "Token has expired"
    except jwt.InvalidSignatureError:
        return False, "Signature verification failed"
    except jwt.InvalidAlgorithmError:
        return False, "Invalid or unsupported algorithm"
    except jwt.DecodeError as e:
        return False, f"Malformed token: {e}"
    except Exception as e:
        return False, f"Verification failed: {e}"


def authenticate_message(token: str, secret_key: str) -> bool:
    """Convenience wrapper — returns True if the token is a valid, unexpired JWS."""
    valid, _ = verify_jwt(token, secret_key)
    return valid

INJECTION_PATTERNS = [
    re.compile(r"(?i)<\s*system\s*>.*?<\s*/\s*system\s*>", re.DOTALL),
    re.compile(r"(?i)\[INST\].*?\[/INST\]", re.DOTALL),
    re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions?"),
    re.compile(r"(?i)you\s+are\s+now\s+"),
    re.compile(r"(?i)new\s+system\s+prompt"),
    re.compile(r"(?i)disregard\s+(all\s+)?(prior|earlier|above)"),
    re.compile(r"(?i)override\s+(your\s+)?(instructions?|rules?|guidelines?)"),
    re.compile(r"<\|im_(start|end)\|>"),
    re.compile(r"(?m)^(Human|Assistant|System)\s*:", re.MULTILINE),
]


def sanitize_inbound(text: str, max_length: int = 50_000) -> str:
    if len(text) > max_length:
        text = text[:max_length] + "\n[... message truncated for safety]"
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            logger.warning("Prompt injection pattern detected in A2A message")
            text = pattern.sub("[FILTERED]", text)
    return text


SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token|credential)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(sk-[a-zA-Z0-9]{20,})"),
    re.compile(r"(?i)(ghp_[a-zA-Z0-9]{20,})"),
    re.compile(r"(?i)(xoxb-[a-zA-Z0-9-]+)"),
    # Email pattern is disabled - too broad, redacts legitimate contact info
    # re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]


def filter_outbound(text: str) -> str:
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text.strip()



_AUDIT_MAX_SIZE = 10 * 1024 * 1024  # 10 MB

# ---------------------------------------------------------------------------
# SSRF protection — webhook endpoint validation
# ---------------------------------------------------------------------------

# Cloud metadata endpoints — never allow delivery to these
_METADATA_HOSTS = frozenset({
    "169.254.169.254",      # AWS / Azure / GCP metadata
    "metadata.google.internal",  # GCP metadata
})

# Private-use CIDRs (RFC 1918 / RFC 3927 / RFC 6761)
_PRIVATE_CIDRS: list[ipaddress._BaseNetwork] = [
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),       # private
    ipaddress.ip_network("172.16.0.0/12"),    # private
    ipaddress.ip_network("192.168.0.0/16"),   # private
    ipaddress.ip_network("169.254.0.0/16"),   # link-local (includes 169.254.169.254)
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 private
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _is_private_ip(ip_str: str) -> bool:
    """Return True if ip_str is a private/internal IP address."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in _PRIVATE_CIDRS)
    except ValueError:
        return False


def is_safe_url(url: str) -> bool:
    """Return True if the URL is safe to deliver a webhook to.

    Blocks:
    - Non-HTTP(S) schemes
    - Loopback / private IP ranges (127.x, 10.x, 172.16–31.x, 192.168.x, 169.254.x)
    - Hostnames that resolve to any of the above
    - Well-known cloud metadata endpoints (169.254.169.254, metadata.google.internal)
    - Internal host literals: localhost, 0.0.0.0, *
    """
    if not url or url.strip() == "":
        return False

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    # urlparse.hostname is None for unbracketed IPv6 like "::1" because it
    # can't distinguish host "::1" from a port. Detect it via netloc colons.
    raw_host = parsed.netloc
    if parsed.hostname is not None:
        hostname = parsed.hostname
    elif raw_host.startswith("["):
        # bracketed IPv6: [::1] -> ::1
        hostname = raw_host.split("]")[0][1:]
    else:
        # Unbracketed: if netloc has multiple colons, treat whole netloc as the host
        if raw_host.count(":") >= 2:
            hostname = raw_host
        else:
            hostname = raw_host.split(":")[0]
    host = parsed.netloc

    # Block non-HTTP(S) schemes
    if scheme not in ("http", "https"):
        return False

    # Block wildcard / bare asterisk (SSRF probe)
    if hostname == "*" or host.strip() == "*":
        return False

    # Block literal internal addresses
    if hostname in ("localhost", "0.0.0.0"):
        return False

    # Block IPv6 loopback
    if hostname in ("::1", "[::1]"):
        return False

    # Block known metadata hosts
    if hostname in _METADATA_HOSTS or hostname.startswith("metadata.google.internal"):
        return False

    # Block direct private/public-border IPs
    if hostname and _is_private_ip(hostname):
        return False

    # For hostnames (not raw IPs), do a DNS lookup and check the resolved IP
    if hostname and not hostname[0].isdigit():
        try:
            socket.setdefaulttimeout(5.0)
            resolved = socket.gethostbyname(hostname)
            if _is_private_ip(resolved):
                return False
        except (socket.gaierror, socket.timeout, OSError):
            # If we can't resolve, treat as unsafe rather than allow
            return False

    return True


# ---------------------------------------------------------------------------
# SSRF protection — URL and host validation (consolidated)
# ---------------------------------------------------------------------------


def validate_host(host: str) -> str:
    """Validate a host string for SSRF safety.

    Rejects loopback addresses and private network IPs (10.x, 172.16-31.x,
    192.168.x, 169.254.x, IPv6 loopback/private/link-local). Delegates to
    :func:`_is_private_ip` for full CIDR coverage.

    Returns the host unchanged if safe; raises ``ValueError`` if unsafe.

    Args:
        host: A hostname or IP address string.

    Raises:
        ValueError: If the host resolves to a loopback or private address.
    """
    # urlparse requires a scheme; prefix to extract the host component.
    parsed = urlparse(f"http://{host}")
    netloc = parsed.netloc.split(":")[0]
    if netloc in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise ValueError(
            f"Host ({host}) resolves to a loopback address; "
            "refusing to deliver to an internal endpoint"
        )
    if _is_private_ip(netloc):
        raise ValueError(
            f"Host ({host}) resolves to a private network address "
            f"({netloc}); refusing SSRF delivery"
        )
    return host


def validate_target_url(url: str, allow_loopback: bool = False) -> str:
    """Validate and SSRF-protect a target URL.

    Blocks non-HTTP(S) schemes, loopback addresses (when
    ``allow_loopback=False``), and private network IPs via
    :func:`_is_private_ip`. Unlike :func:`is_safe_url`, this does NOT
    perform DNS resolution — it validates the URL text and the IP
    it literally contains. DNS rebinding protection belongs at the
    transport layer, not at URL-validation time.

    Args:
        url: The target URL to validate.
        allow_loopback: If True, allow loopback addresses (fleet-internal).

    Returns:
        The URL string unchanged if safe.

    Raises:
        ValueError: If the URL is unsafe (SSRF risk).
    """
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("A2A URL must be an http(s) URL")

    host = parsed.netloc.split(":")[0]
    if host in ("0.0.0.0",):
        raise ValueError("A2A URL cannot be the unspecified address")

    if not allow_loopback:
        if host in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("A2A URL cannot point to loopback addresses")
        # Reject private network IPs
        if _is_private_ip(host):
            raise ValueError(
                f"URL resolves to private network ({host}); "
                "refusing SSRF delivery"
            )
    return url


def validate_webhook_endpoint(endpoint: str) -> tuple[bool, str]:
    """Validate a webhook endpoint for push notification delivery.

    Returns:
        (True, "")            — endpoint is safe and HTTPS
        (False, <reason>)     — endpoint is unsafe or not HTTPS
    """
    if not endpoint or not endpoint.strip():
        return False, "Empty endpoint URL"

    if not endpoint.lower().startswith("https://"):
        return False, "Webhook endpoint must use HTTPS"

    if not is_safe_url(endpoint):
        return False, f"Webhook endpoint is not allowed: {endpoint} (SSRF protection)"

    return True, ""


class AuditLogger:
    def __init__(self, log_path: Optional[Path] = None):
        if log_path is None:
            from .persistence import _get_hermes_home
            log_path = Path(_get_hermes_home()) / "a2a_audit.jsonl"
        self.log_path = log_path
        self._lock = Lock()

    def _rotate_if_needed(self) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > _AUDIT_MAX_SIZE:
                rotated = self.log_path.with_suffix(".jsonl.old")
                if rotated.exists():
                    rotated.unlink()
                self.log_path.rename(rotated)
        except Exception:
            pass

    def log(self, event_type: str, data: dict) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        try:
            with self._lock:
                # Hold lock across rotation check to prevent TOCTOU
                self._rotate_if_needed()
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("Failed to write A2A audit log", exc_info=True)


audit = AuditLogger()
