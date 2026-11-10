"""JWKS-backed JWT signature verifier for the ``oidc_jwt`` identity provider.

Fetches signing keys from a JWKS endpoint and checks signature, expiry,
issuer and audience before any claim is read. The JWKS is cached in
memory and refreshed lazily on a ``kid`` miss (common when the IdP
rotates signing keys); the cache TTL bounds how long a revoked key keeps
being accepted (default 1 hour).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


# Default TTL for JWKS in-memory cache. Long enough to amortise the
# round-trip across many requests, short enough that a key rotation
# is picked up within an hour even when the kid happens to be reused.
DEFAULT_JWKS_CACHE_TTL_SECONDS = 3600

# Default per-request timeout for fetching JWKS; IdPs typically
# respond in <100ms, so 5s is a generous upper bound.
DEFAULT_JWKS_HTTP_TIMEOUT_SECONDS = 5.0


class JWKSVerificationError(Exception):
    """Raised when JWT verification fails for any reason.

    Wraps the underlying PyJWT exception with a stable error class so
    callers (the identity providers) don't depend on PyJWT's exception hierarchy.
    """


@dataclass
class JWKSVerifier:
    """Verify JWTs against a JWKS endpoint.

    Instances are cheap to construct but the underlying ``PyJWKClient``
    caches keys, so prefer a process-singleton in production.

    Attributes:
        jwk_url: the JWKS endpoint URL.
        issuer: Expected ``iss`` claim; ``None`` skips issuer check.
        audience: Expected ``aud`` claim; ``None`` skips audience check.
        cache_ttl_seconds: How long to trust a fetched key.
        algorithms: Allowed signing algorithms. RS256 is the
            common default; ES256 is the elliptic-curve variant some tenants
            use. Do NOT include HS256 — symmetric keys in a public
            JWKS would defeat the verifier.
    """

    jwk_url: str
    issuer: str | None = None
    audience: str | None = None
    cache_ttl_seconds: int = DEFAULT_JWKS_CACHE_TTL_SECONDS
    http_timeout_seconds: float = DEFAULT_JWKS_HTTP_TIMEOUT_SECONDS
    algorithms: tuple[str, ...] = ("RS256", "ES256")

    _client: PyJWKClient | None = field(default=None, init=False, repr=False)
    _client_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _client_built_at: float = field(default=0.0, init=False, repr=False)

    def _get_client(self) -> PyJWKClient:
        """Return the ``PyJWKClient``, refreshing it when the TTL elapses.

        ``PyJWKClient`` caches keys internally for the lifetime of the
        instance. Rebuilding the client on TTL forces a fresh fetch,
        which lets the operator rotate signing keys without restarting
        the service.
        """
        now = time.monotonic()
        with self._client_lock:
            if self._client is None or (now - self._client_built_at) > self.cache_ttl_seconds:
                self._client = PyJWKClient(
                    self.jwk_url,
                    cache_keys=True,
                    timeout=self.http_timeout_seconds,
                )
                self._client_built_at = now
            return self._client

    def verify_and_decode(self, jwt_text: str) -> dict[str, Any]:
        """Verify the signature + claims and return the decoded payload.

        Raises ``JWKSVerificationError`` on any failure: malformed
        token, wrong signature, expired, wrong issuer/audience,
        unsupported algorithm, or JWKS-fetch failure.
        """
        try:
            signing_key = self._get_client().get_signing_key_from_jwt(jwt_text)
        except (
            jwt.PyJWKClientError,
            jwt.DecodeError,
            jwt.PyJWTError,
            httpx.HTTPError,
            OSError,
        ) as exc:
            # ``get_signing_key_from_jwt`` parses the JWT header to
            # extract the ``kid`` before hitting the JWKS. A garbage
            # token raises ``DecodeError`` here, not the network
            # errors we'd naively expect. Catch the full PyJWT base
            # class so any future error subclass also lands here.
            raise JWKSVerificationError(
                f"JWKS fetch / kid lookup / token-shape failure: {exc}"
            ) from exc

        options: dict[str, Any] = {"require": ["exp", "iat"]}
        verify_kwargs: dict[str, Any] = {
            "algorithms": list(self.algorithms),
            "options": options,
        }
        if self.audience is not None:
            verify_kwargs["audience"] = self.audience
        else:
            options["verify_aud"] = False
        if self.issuer is not None:
            verify_kwargs["issuer"] = self.issuer

        try:
            return jwt.decode(jwt_text, signing_key.key, **verify_kwargs)
        except jwt.ExpiredSignatureError as exc:
            raise JWKSVerificationError("JWT expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise JWKSVerificationError(
                f"JWT issuer mismatch (expected {self.issuer!r}): {exc}"
            ) from exc
        except jwt.InvalidAudienceError as exc:
            raise JWKSVerificationError(
                f"JWT audience mismatch (expected {self.audience!r}): {exc}"
            ) from exc
        except jwt.InvalidSignatureError as exc:
            raise JWKSVerificationError("JWT signature invalid") from exc
        except jwt.InvalidAlgorithmError as exc:
            raise JWKSVerificationError(f"JWT signed with disallowed algorithm: {exc}") from exc
        except jwt.PyJWTError as exc:
            # Catch-all for any other PyJWT-raised error. Keep the
            # original exception chained so logs preserve the cause.
            raise JWKSVerificationError(f"JWT verification failed: {exc}") from exc
