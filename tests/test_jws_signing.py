"""Tests for JWS signing and verification in security.py."""

from __future__ import annotations

import time

import json as _json

import jwt
import pytest

from hermes_agent_a2a.security import (
    authenticate_message,
    sign_payload,
    verify_jwt,
)


SECRET_A = "test-secret-key-a"
SECRET_B = "test-secret-key-b"


class TestSignAndVerifyRoundTrip:
    def test_sign_and_verify_round_trip(self):
        payload = {"sub": "agent-123", "role": "assistant"}
        token = sign_payload(payload, SECRET_A)
        valid, decoded = verify_jwt(token, SECRET_A)
        assert valid is True
        assert decoded["sub"] == "agent-123"
        assert decoded["role"] == "assistant"


class TestVerifyValidToken:
    def test_verify_valid_token(self):
        payload = {"user": "alice"}
        token = sign_payload(payload, SECRET_A)
        valid, result = verify_jwt(token, SECRET_A)
        assert valid is True
        assert isinstance(result, dict)
        assert result["user"] == "alice"


class TestVerifyTamperedToken:
    def test_verify_tampered_token(self):
        payload = {"data": "original"}
        token = sign_payload(payload, SECRET_A)
        # Tamper with the payload part of the JWS compact string
        parts = token.split(".")
        from jwt.api_jws import base64url_encode
        tampered_payload = base64url_encode(b"tampered").decode()
        tampered_parts = [parts[0], tampered_payload, parts[2]]
        tampered_token = ".".join(tampered_parts)
        valid, reason = verify_jwt(tampered_token, SECRET_A)
        assert valid is False
        assert reason is not None


class TestVerifyExpiredToken:
    def test_verify_expired_token(self):
        now = int(time.time())
        expired_payload = {
            "sub": "agent",
            "iat": now - 600,
            "exp": now - 1,  # already expired
        }
        token = jwt.encode(expired_payload, SECRET_A, algorithm="HS256")
        valid, reason = verify_jwt(token, SECRET_A)
        assert valid is False
        assert "expired" in reason.lower()


class TestVerifyWrongSecret:
    def test_verify_wrong_secret(self):
        payload = {"data": "secret"}
        token = sign_payload(payload, SECRET_A)
        valid, reason = verify_jwt(token, SECRET_B)
        assert valid is False
        assert "signature" in reason.lower() or "failed" in reason.lower()


class TestRejectAlgNone:
    def test_reject_alg_none(self):
        # Manually construct a token with alg: none using base64url encoding
        from jwt.api_jws import base64url_encode
        header = base64url_encode(_json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload = base64url_encode(_json.dumps({"sub": "test"}).encode())
        unsigned_token = f"{header.decode()}.{payload.decode()}."
        valid, reason = verify_jwt(unsigned_token, SECRET_A)
        assert valid is False
        assert "algorithm" in reason.lower() or "signature" in reason.lower()


class TestRejectNonHS256:
    def test_reject_non_hs256(self):
        # Manually construct a token with alg: RS256 (asymmetric) to simulate algorithm confusion
        # PyJWT validates the algorithm against the allowed list during decode
        from jwt.api_jws import base64url_encode
        header = base64url_encode(_json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = base64url_encode(_json.dumps({"sub": "test"}).encode())
        fake_sig = base64url_encode(b"fakesignature")
        rs256_token = f"{header.decode()}.{payload.decode()}.{fake_sig.decode()}"
        valid, reason = verify_jwt(rs256_token, SECRET_A)
        assert valid is False
        assert "algorithm" in reason.lower() or "signature" in reason.lower()


class TestIatAndExpClaims:
    def test_iat_and_exp_claims(self):
        payload = {"msg": "hello"}
        token = sign_payload(payload, SECRET_A)
        # Decode without verification to inspect claims
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert "iat" in decoded
        assert "exp" in decoded
        assert isinstance(decoded["iat"], int)
        assert isinstance(decoded["exp"], int)
        assert decoded["exp"] > decoded["iat"]


class TestAuthenticateMessage:
    def test_authenticate_message_true(self):
        payload = {"sub": "agent-1"}
        token = sign_payload(payload, SECRET_A)
        assert authenticate_message(token, SECRET_A) is True

    def test_authenticate_message_false(self):
        payload = {"sub": "agent-1"}
        token = sign_payload(payload, SECRET_A)
        # Use wrong secret
        assert authenticate_message(token, SECRET_B) is False
        # Use tampered token
        tampered = token[:-5] + "XXXXX"
        assert authenticate_message(tampered, SECRET_A) is False
        # Use expired token
        now = int(time.time())
        expired_payload = {
            "sub": "agent",
            "iat": now - 600,
            "exp": now - 1,
        }
        expired_token = jwt.encode(expired_payload, SECRET_A, algorithm="HS256")
        assert authenticate_message(expired_token, SECRET_A) is False
