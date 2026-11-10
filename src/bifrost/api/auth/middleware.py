"""IdentityMiddleware — gates every non-public route on a principal
resolved by the configured ``IdentityProvider``.

* Public allowlist for liveness probes + the auth routes themselves (the
  UI identity gate calls /api/v1/auth/me unauthenticated).
* The configured identity headers are stripped from the request before
  anything reads them unless the ``header`` provider is active and the
  peer is inside ``identity.header.trusted_proxy_cidrs``, so a client
  cannot fake them.
* ``Authorization: Bearer bft_...`` is checked against ``api_tokens``; any
  other bearer value goes to the JWT provider (``oidc_jwt``) or is
  rejected, never ignored.
* Roles are resolved here, once, and carried on the principal.
* Unauthenticated requests get a 401 with a stable JSON envelope
  (``identity_required`` / ``invalid_credentials``) the UI can branch on.
* The resolved principal is stashed at ``request.state.principal`` for
  ``current_principal``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from bifrost.api.auth.network import Network, peer_in
from bifrost.api.auth.principal import Principal
from bifrost.api.auth.providers import IdentityProvider, InvalidCredentials
from bifrost.api.auth.roles import assign_roles
from bifrost.api.auth.tokens import TOKEN_PREFIX, ApiTokenAuthenticator

logger = logging.getLogger(__name__)


# Public path allowlist. Keep MINIMAL — anything here reaches handlers
# without an authenticated caller. See per-entry rationale.
DEFAULT_PUBLIC_PATHS: tuple[str, ...] = (
    "/health",  # FastAPI app's bare health endpoint
    "/api/v1/health",  # Liveness/readiness probe
    "/api/v1/health/claude-cli",  # FE banner, no identity needed
    "/api/v1/auth/me",  # FE identity gate's resolve probe
    "/api/v1/auth/refresh",  # FE identity gate's verify button
    "/api/v1/auth/logout",  # Always allow signing out
    "/docs",  # FastAPI Swagger
    "/redoc",  # FastAPI Redoc
    "/openapi.json",  # FastAPI OpenAPI spec
)


# Public PATH PREFIXES — same idea but for prefix-matched routes
# (e.g. /api/v1/health/* covers /health/redis etc.). Use sparingly.
DEFAULT_PUBLIC_PREFIXES: tuple[str, ...] = ("/api/v1/health/",)


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


class IdentityMiddleware(BaseHTTPMiddleware):
    """Reject requests the identity provider cannot resolve.

    ``identity_headers`` are removed from the request before resolution
    unless ``trusted_networks`` is set and the peer is inside it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        provider: IdentityProvider,
        admin_groups: Collection[str] = (),
        tokens: ApiTokenAuthenticator | None = None,
        identity_headers: Iterable[str] = (),
        trusted_networks: tuple[Network, ...] | None = None,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
        public_prefixes: Iterable[str] = DEFAULT_PUBLIC_PREFIXES,
    ) -> None:
        super().__init__(app)
        self._provider = provider
        self._admin_groups = frozenset(admin_groups)
        self._tokens = tokens
        self._identity_headers = frozenset(h.lower() for h in identity_headers)
        self._trusted_networks = trusted_networks
        self._public_paths = frozenset(public_paths)
        self._public_prefixes = tuple(public_prefixes)

    def _is_public(self, path: str) -> bool:
        if path in self._public_paths:
            return True
        return any(path.startswith(prefix) for prefix in self._public_prefixes)

    def _strip_untrusted_identity_headers(self, request: Request) -> None:
        peer = request.client.host if request.client else None
        if self._trusted_networks is not None and peer_in(peer, self._trusted_networks):
            return
        request.scope["headers"] = [
            (k, v)
            for k, v in request.scope.get("headers") or ()
            if k.decode("latin-1").lower() not in self._identity_headers
        ]

    async def _resolve(self, request: Request) -> Principal | None:
        token = _bearer_token(request)
        if token is not None and token.startswith(TOKEN_PREFIX):
            principal = self._tokens.authenticate(token) if self._tokens else None
            if principal is None:
                raise InvalidCredentials("invalid api token")
            return principal
        if token is not None and not self._provider.handles_bearer:
            raise InvalidCredentials("bearer credential is not accepted by this server")
        principal = await self._provider.resolve(request)
        if principal is None:
            return None
        return assign_roles(
            principal, provider_name=self._provider.name, admin_groups=self._admin_groups
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._identity_headers:
            self._strip_untrusted_identity_headers(request)

        rejected: str | None = None
        principal: Principal | None = None
        try:
            principal = await self._resolve(request)
        except InvalidCredentials as exc:
            rejected = str(exc)
            logger.info("auth.invalid_credentials path=%s reason=%s", request.url.path, rejected)
        if principal is not None:
            request.state.principal = principal

        if self._is_public(request.url.path):
            return await call_next(request)

        if principal is None:
            logger.info("auth.identity_missing path=%s rejected=%s", request.url.path, rejected)
            return JSONResponse(
                status_code=401,
                content={
                    "error": rejected or "missing identity",
                    "detail": (
                        "Sign in through your identity provider "
                        f"(identity.provider={self._provider.name})."
                    ),
                    "code": "invalid_credentials" if rejected else "identity_required",
                },
            )

        return await call_next(request)


# ── per-request dependency ───────────────────────────────────────────


def current_principal(request: Request) -> Principal:
    """FastAPI dependency that returns the principal stashed by the
    middleware. Raises 401 if the middleware didn't run (test client
    pinning a route that bypasses the app stack, etc.)."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing identity",
        )
    return principal


def optional_principal(request: Request) -> Principal | None:
    """Variant for routes that want the identity if available but
    don't reject when absent (e.g. /auth/me itself)."""
    return getattr(request.state, "principal", None)
