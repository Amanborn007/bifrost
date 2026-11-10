"""Pluggable identity layer for Bifrost.

``identity.provider`` selects how a request becomes a ``Principal``:

  * ``none`` — ``BIFROST_OPERATOR_EMAIL`` / ``BIFROST_OPERATOR_GROUPS``,
    else the demo principal when ``BIFROST_DEMO_MODE=true``; loopback
    clients only, and the operator is an admin.
  * ``header`` — a trusted reverse proxy (oauth2-proxy, Pomerium)
    forwards email/groups headers; requires ``identity.trusted_proxy`` and
    only believes peers in ``identity.header.trusted_proxy_cidrs``.
  * ``oidc_jwt`` — ``Authorization: Bearer <JWT>`` verified against a JWKS.

Independently of the provider, ``Authorization: Bearer bft_...`` is an API
token checked against ``api_tokens``. ``identity.roles`` maps IdP groups to
the ``admin`` role; every principal is at least a ``member``.

Public surface:

* ``Principal`` — dataclass with email/groups/roles/source/expires_at
* ``IdentityProvider`` / ``build_identity_provider`` — provider contract
  and factory
* ``IdentityMiddleware`` — gates non-public paths on a resolved principal
* ``CsrfMiddleware`` — origin + ``X-Requested-With`` check for browsers
* ``current_principal`` — FastAPI dependency returning the per-request
  principal stashed by the middleware
"""

from bifrost.api.auth.csrf import CsrfMiddleware
from bifrost.api.auth.middleware import IdentityMiddleware, current_principal, optional_principal
from bifrost.api.auth.principal import (
    PRINCIPAL_SOURCES,
    ROLE_ADMIN,
    ROLE_MEMBER,
    SOURCE_API_TOKEN,
    SOURCE_DEMO_FALLBACK,
    SOURCE_ENV_OVERRIDE,
    SOURCE_OIDC_JWT,
    SOURCE_SSO_HEADER,
    Principal,
)
from bifrost.api.auth.providers import (
    IdentityConfigError,
    IdentityProvider,
    InvalidCredentials,
    build_identity_provider,
)

__all__ = [
    "Principal",
    "IdentityProvider",
    "IdentityConfigError",
    "InvalidCredentials",
    "build_identity_provider",
    "IdentityMiddleware",
    "CsrfMiddleware",
    "current_principal",
    "optional_principal",
    "PRINCIPAL_SOURCES",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "SOURCE_API_TOKEN",
    "SOURCE_DEMO_FALLBACK",
    "SOURCE_ENV_OVERRIDE",
    "SOURCE_OIDC_JWT",
    "SOURCE_SSO_HEADER",
]
