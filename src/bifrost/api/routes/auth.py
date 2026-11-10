"""Auth endpoints: ``/auth/me``, ``/auth/refresh``, ``/auth/logout``.

Mounted at ``/api/v1/auth/*``; all three are PUBLIC (allowed by
``IdentityMiddleware``'s allowlist) because the UI identity gate must
bootstrap before it has an identity. ``/me`` reports the resolved
principal plus the active provider and its capabilities; ``/refresh``
only does something for providers that declare ``interactive_refresh``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bifrost.api.auth.principal import Principal
from bifrost.api.auth.providers import IdentityProvider, InvalidCredentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _provider(request: Request) -> IdentityProvider:
    provider: IdentityProvider = request.app.state.identity_provider
    return provider


def _envelope(provider: IdentityProvider) -> dict[str, Any]:
    return {
        "provider": provider.name,
        "capabilities": {"interactive_refresh": provider.interactive_refresh},
    }


@router.get("/me")
async def get_me(request: Request) -> JSONResponse:
    """Return the authenticated principal for the current request, or a
    401 carrying the provider + capabilities so the UI gate can decide
    what to offer."""
    provider = _provider(request)
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        return JSONResponse(
            status_code=401,
            content={
                "error": "missing identity",
                "code": "identity_required",
                **_envelope(provider),
            },
        )
    return JSONResponse(content={**principal.to_json(), **_envelope(provider)})


@router.post("/refresh")
async def refresh_auth(request: Request) -> JSONResponse:
    """Interactive re-verification, for providers that support it."""
    provider = _provider(request)
    refresh = getattr(provider, "refresh", None)
    if not provider.interactive_refresh or refresh is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "refresh not supported",
                "detail": f"identity.provider={provider.name} has no interactive refresh",
                "code": "refresh_unsupported",
                **_envelope(provider),
            },
        )
    try:
        principal: Principal = await refresh(request)
    except InvalidCredentials as exc:
        return JSONResponse(
            status_code=401,
            content={"error": str(exc), "code": "invalid_credentials", **_envelope(provider)},
        )
    return JSONResponse(content={**principal.to_json(), **_envelope(provider)})


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Record the sign-out and return the principal the provider still
    resolves. Providers hold no server-side session, so header/OIDC
    sign-out happens at the proxy or IdP."""
    provider = _provider(request)
    caller: Principal | None = getattr(request.state, "principal", None)
    logger.info(
        "auth.logout by=%s provider=%s", caller.email if caller else "<unknown>", provider.name
    )
    if caller is None:
        return JSONResponse(
            content={"email": "", "groups": [], "source": "", **_envelope(provider)}
        )
    return JSONResponse(content={**caller.to_json(), **_envelope(provider)})
