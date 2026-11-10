"""Tests for the JWKS-backed OIDC JWT verifier.

We generate an RSA key pair in-process, sign tokens with the private
half, and verify them by stubbing the ``JWKSVerifier``'s PyJWKClient
to return a key constructed from the public half. This is faster
than running a fake JWKS HTTP server and avoids depending on the
network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bifrost.api.auth.jwt_verifier import JWKSVerificationError, JWKSVerifier

# ── shared key pair fixture ─────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, bytes]:
    """Generate one RSA key pair for the whole test module.

    Returns the private key object and the PEM-encoded public key.
    Generation is ~100ms so re-using it across tests is the polite
    thing to do.
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public_pem


def _sign(private: Any, claims: dict[str, Any]) -> str:
    """Encode ``claims`` as a JWT signed with the test RSA key."""
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-kid"})


@dataclass
class _StubSigningKey:
    """Mimics PyJWKClient.get_signing_key_from_jwt's return value."""

    key: Any


class _StubPyJWKClient:
    """Drop-in replacement for the real PyJWKClient.

    The real client makes an HTTPS request to fetch the JWKS; tests
    can't depend on that, so we monkeypatch it to return a static key
    object that PyJWT will accept.
    """

    def __init__(self, public_pem: bytes) -> None:
        # Re-parse the PEM into a key object PyJWT can use.
        self._key = serialization.load_pem_public_key(public_pem)

    def get_signing_key_from_jwt(self, _jwt_text: str) -> _StubSigningKey:
        return _StubSigningKey(key=self._key)


# ── happy path ──────────────────────────────────────────────────────


class TestHappyPath:
    def test_verifies_well_formed_token(
        self,
        rsa_keypair: tuple[Any, bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        private, public_pem = rsa_keypair
        now = int(time.time())
        token = _sign(
            private,
            {
                "iat": now,
                "exp": now + 300,
                "sub": "user@example.com",
                "email": "user@example.com",
                "groups": ["bifrost-admins"],
            },
        )
        v = JWKSVerifier(jwk_url="https://stub/keys")
        monkeypatch.setattr(v, "_get_client", lambda: _StubPyJWKClient(public_pem))
        claims = v.verify_and_decode(token)
        assert claims["email"] == "user@example.com"
        assert claims["groups"] == ["bifrost-admins"]

    def test_verifies_with_issuer_and_audience(
        self,
        rsa_keypair: tuple[Any, bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        private, public_pem = rsa_keypair
        now = int(time.time())
        token = _sign(
            private,
            {
                "iat": now,
                "exp": now + 300,
                "sub": "user@example.com",
                "iss": "https://idp.example.com/oauth2/default",
                "aud": "bifrost",
            },
        )
        v = JWKSVerifier(
            jwk_url="https://stub/keys",
            issuer="https://idp.example.com/oauth2/default",
            audience="bifrost",
        )
        monkeypatch.setattr(v, "_get_client", lambda: _StubPyJWKClient(public_pem))
        assert v.verify_and_decode(token)["sub"] == "user@example.com"


# ── failure paths ───────────────────────────────────────────────────


class TestFailures:
    def test_expired_token_rejected(
        self,
        rsa_keypair: tuple[Any, bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        private, public_pem = rsa_keypair
        now = int(time.time())
        token = _sign(
            private,
            {"iat": now - 600, "exp": now - 300, "sub": "x@y"},
        )
        v = JWKSVerifier(jwk_url="https://stub/keys")
        monkeypatch.setattr(v, "_get_client", lambda: _StubPyJWKClient(public_pem))
        with pytest.raises(JWKSVerificationError, match="expired"):
            v.verify_and_decode(token)

    def test_wrong_signature_rejected(
        self,
        rsa_keypair: tuple[Any, bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sign with a DIFFERENT key but ask the verifier to trust the
        # original public key. The signature won't validate.
        other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _, public_pem = rsa_keypair
        now = int(time.time())
        token = _sign(other_private, {"iat": now, "exp": now + 300, "sub": "x@y"})
        v = JWKSVerifier(jwk_url="https://stub/keys")
        monkeypatch.setattr(v, "_get_client", lambda: _StubPyJWKClient(public_pem))
        with pytest.raises(JWKSVerificationError, match="signature"):
            v.verify_and_decode(token)

    def test_wrong_issuer_rejected(
        self,
        rsa_keypair: tuple[Any, bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        private, public_pem = rsa_keypair
        now = int(time.time())
        token = _sign(
            private,
            {"iat": now, "exp": now + 300, "sub": "x@y", "iss": "https://evil"},
        )
        v = JWKSVerifier(
            jwk_url="https://stub/keys",
            issuer="https://idp.example.com/oauth2/default",
        )
        monkeypatch.setattr(v, "_get_client", lambda: _StubPyJWKClient(public_pem))
        with pytest.raises(JWKSVerificationError, match="issuer"):
            v.verify_and_decode(token)

    def test_wrong_audience_rejected(
        self,
        rsa_keypair: tuple[Any, bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        private, public_pem = rsa_keypair
        now = int(time.time())
        token = _sign(
            private,
            {"iat": now, "exp": now + 300, "sub": "x@y", "aud": "not-bifrost"},
        )
        v = JWKSVerifier(jwk_url="https://stub/keys", audience="bifrost")
        monkeypatch.setattr(v, "_get_client", lambda: _StubPyJWKClient(public_pem))
        with pytest.raises(JWKSVerificationError, match="audience"):
            v.verify_and_decode(token)

    def test_malformed_token_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        v = JWKSVerifier(jwk_url="https://stub/keys")
        # Don't even bother stubbing the client — PyJWT rejects the
        # shape before it touches the JWKS.
        with pytest.raises(JWKSVerificationError):
            v.verify_and_decode("not.a.jwt")
