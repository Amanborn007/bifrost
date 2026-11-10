"""Pluggable identity providers.

A provider turns an inbound request into a ``Principal`` (or ``None``
when the request carries no credentials). Three are built in:

* ``none`` — local operator from ``BIFROST_OPERATOR_EMAIL``, else the
  demo principal when demo mode is on. Served to loopback clients only.
* ``header`` — a trusted reverse proxy (oauth2-proxy, Pomerium)
  authenticates the user and forwards email/groups headers.
* ``oidc_jwt`` — ``Authorization: Bearer <JWT>`` verified against a JWKS.

``resolve`` returns ``None`` for "no credentials presented" and raises
``InvalidCredentials`` for "credentials presented but wrong", so the
middleware can answer 401 with the right code.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from starlette.requests import Request

from bifrost.api.auth.jwt_verifier import (
    JWKSVerificationError,
    JWKSVerifier,
)
from bifrost.api.auth.network import Network, is_loopback, parse_networks
from bifrost.api.auth.principal import (
    SOURCE_ENV_OVERRIDE,
    SOURCE_OIDC_JWT,
    SOURCE_SSO_HEADER,
    Principal,
    demo_principal,
    split_groups,
)
from bifrost.api.auth.tokens import ApiTokenAuthenticator
from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import IdentityConfig
from bifrost.errors import ConfigError

logger = logging.getLogger(__name__)


class IdentityConfigError(ConfigError):
    """The identity section is unusable; the app must not start."""

    code = "config.identity"


class InvalidCredentials(Exception):
    """Credentials were presented but failed verification."""


class IdentityProvider(Protocol):
    name: str
    # True only for providers that implement ``async refresh(request)``
    # (an interactive re-verification the UI may trigger).
    interactive_refresh: bool
    # True when a non-``bft_`` ``Authorization: Bearer`` value is this
    # provider's credential (a JWT); other providers reject it.
    handles_bearer: bool

    async def resolve(self, request: Request) -> Principal | None: ...


class NoneProvider:
    name = "none"
    interactive_refresh = False
    handles_bearer = False

    def __init__(
        self,
        *,
        demo_mode: bool,
        demo_email: str,
        operator_email: str | None = None,
        operator_groups: str | None = None,
    ) -> None:
        self._demo_mode = demo_mode
        self._demo_email = demo_email
        self._operator_email = (operator_email or "").strip()
        self._operator_groups = operator_groups

    async def resolve(self, request: Request) -> Principal | None:
        if not is_loopback(request.client.host if request.client else None):
            return None
        if self._operator_email:
            return Principal(
                email=self._operator_email,
                groups=split_groups(self._operator_groups),
                source=SOURCE_ENV_OVERRIDE,
            )
        if self._demo_mode:
            return demo_principal(self._demo_email)
        return None


class HeaderProvider:
    name = "header"
    interactive_refresh = False
    handles_bearer = False

    def __init__(self, *, email_header: str, groups_header: str) -> None:
        self._email_header = email_header
        self._groups_header = groups_header

    async def resolve(self, request: Request) -> Principal | None:
        email = (request.headers.get(self._email_header) or "").strip()
        if not email:
            return None
        return Principal(
            email=email,
            groups=split_groups(request.headers.get(self._groups_header)),
            source=SOURCE_SSO_HEADER,
        )


class OidcJwtProvider:
    name = "oidc_jwt"
    interactive_refresh = False
    handles_bearer = True

    def __init__(self, *, verifier: JWKSVerifier, groups_claim: str) -> None:
        self._verifier = verifier
        self._groups_claim = groups_claim

    async def resolve(self, request: Request) -> Principal | None:
        scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        try:
            # PyJWKClient fetches the JWKS over blocking urllib.
            claims = await asyncio.to_thread(self._verifier.verify_and_decode, token)
        except JWKSVerificationError as exc:
            logger.warning("identity.oidc_jwt.rejected reason=%s", exc)
            raise InvalidCredentials("invalid bearer token") from exc
        return self._principal_from_claims(claims)

    def _principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        email = str(claims.get("email") or claims.get("preferred_username") or "").strip()
        sub = str(claims.get("sub") or "").strip()
        if not email and "@" in sub:
            email = sub
        if not email:
            logger.warning("identity.oidc_jwt.rejected reason=no email/preferred_username/sub")
            raise InvalidCredentials("token carries no email claim")
        raw_groups = claims.get(self._groups_claim) or []
        if isinstance(raw_groups, str):
            groups = split_groups(raw_groups)
        else:
            groups = split_groups(",".join(str(g) for g in raw_groups))
        exp = claims.get("exp")
        return Principal(
            email=email,
            groups=groups,
            source=SOURCE_OIDC_JWT,
            expires_at=datetime.fromtimestamp(exp, UTC) if isinstance(exp, int | float) else None,
        )


def _build_oidc(ident: IdentityConfig, http_timeout_seconds: float) -> OidcJwtProvider:
    missing = [k for k in ("jwks_url", "issuer", "audience") if not getattr(ident.oidc, k)]
    if missing:
        raise IdentityConfigError(
            f"identity.provider=oidc_jwt requires identity.oidc.{', identity.oidc.'.join(missing)}"
        )
    verifier = JWKSVerifier(
        jwk_url=ident.oidc.jwks_url,
        issuer=ident.oidc.issuer,
        audience=ident.oidc.audience,
        http_timeout_seconds=http_timeout_seconds,
    )
    return OidcJwtProvider(verifier=verifier, groups_claim=ident.oidc.groups_claim)


def build_identity_provider(settings: BifrostSettings) -> IdentityProvider:
    """Construct the configured provider, raising ``IdentityConfigError``
    for any combination that would make identity untrustworthy."""
    ident = settings.identity
    if ident.provider != "none" and settings.demo_mode:
        raise IdentityConfigError(
            "demo mode (BIFROST_DEMO_MODE) only works with identity.provider=none; "
            f"it would bypass the {ident.provider} provider"
        )
    if ident.provider == "header":
        if not ident.trusted_proxy:
            raise IdentityConfigError(
                "identity.provider=header trusts proxy-supplied headers, so it "
                "needs identity.trusted_proxy=true to confirm a reverse proxy "
                "strips client-supplied copies"
            )
        _require_roles(ident)
        return HeaderProvider(email_header=ident.header.email, groups_header=ident.header.groups)
    if ident.provider == "oidc_jwt":
        provider = _build_oidc(ident, settings.http_timeout_seconds)
        _require_roles(ident)
        return provider
    return NoneProvider(
        demo_mode=settings.demo_mode,
        demo_email=ident.demo_email,
        operator_email=settings.operator_email,
        operator_groups=settings.operator_groups,
    )


def _require_roles(ident: IdentityConfig) -> None:
    if ident.roles is None:
        raise IdentityConfigError(
            f"identity.provider={ident.provider} needs identity.roles to map IdP "
            "groups to roles, e.g. roles: {admin: [<idp-group>]}; use "
            "roles: {admin: []} to grant admin through API tokens only"
        )


def admin_groups(settings: BifrostSettings) -> frozenset[str]:
    """IdP groups that grant the admin role."""
    roles = settings.identity.roles or {}
    return frozenset(roles.get("admin", ()))


def build_token_authenticator(settings: BifrostSettings) -> ApiTokenAuthenticator | None:
    return ApiTokenAuthenticator(settings.api_tokens) if settings.api_tokens else None


def identity_header_names(settings: BifrostSettings) -> tuple[str, ...]:
    """Header names the identity layer reads; discarded unless the peer is trusted."""
    ident = settings.identity
    return (ident.header.email, ident.header.groups)


def trusted_proxy_networks(settings: BifrostSettings) -> tuple[Network, ...] | None:
    """Peers allowed to set identity headers; ``None`` when the header provider is off."""
    ident = settings.identity
    if ident.provider != "header":
        return None
    return parse_networks(ident.header.trusted_proxy_cidrs)
