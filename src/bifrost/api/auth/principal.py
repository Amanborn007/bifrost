"""Principal dataclass + source constants.

The JSON shape is what ``/api/v1/auth/me`` returns and what the UI
identity gate consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Source constants — string values are STABLE CONTRACT consumed by the
# frontend identity gate to choose between the verified / demo /
# error branches. Do not rename.
SOURCE_SSO_HEADER = "sso_header"  # trusted reverse-proxy headers
SOURCE_OIDC_JWT = "oidc_jwt"  # Bearer JWT verified against a JWKS
SOURCE_ENV_OVERRIDE = "env_override"  # BIFROST_OPERATOR_EMAIL et al
SOURCE_DEMO_FALLBACK = "demo_fallback"  # demo mode, nothing else available
SOURCE_API_TOKEN = "api_token"  # ``bft_`` bearer token from bifrost.yaml

PRINCIPAL_SOURCES = (
    SOURCE_SSO_HEADER,
    SOURCE_OIDC_JWT,
    SOURCE_ENV_OVERRIDE,
    SOURCE_DEMO_FALLBACK,
    SOURCE_API_TOKEN,
)

ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"
ROLES = (ROLE_MEMBER, ROLE_ADMIN)


@dataclass(frozen=True)
class Principal:
    """The authenticated operator identity for one request.

    ``expires_at`` is the token expiry for ``oidc_jwt`` principals so
    the FE chip can show it; every other source is per-request truth
    and leaves it ``None``.
    """

    email: str
    groups: tuple[str, ...] = ()
    source: str = ""
    expires_at: datetime | None = None
    roles: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """JSON shape returned by /auth/me."""
        out: dict[str, Any] = {
            "email": self.email,
            "groups": list(self.groups),
            "source": self.source,
            "roles": list(self.roles),
        }
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at.isoformat()
        return out

    def has_role(self, role: str) -> bool:
        return role in self.roles


def split_groups(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated group list (e.g. from
    an ``X-Forwarded-Groups`` header or ``BIFROST_OPERATOR_GROUPS``) into a
    de-duplicated, whitespace-trimmed tuple."""
    if not raw:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for raw_g in str(raw).split(","):
        g = raw_g.strip()
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return tuple(out)


def demo_principal(email: str) -> Principal:
    """Synthesise the demo fallback principal for ``email``. Stamped with
    ``source=demo_fallback`` so the FE chip can render the loud
    'demo mode' badge."""
    return Principal(
        email=email,
        source=SOURCE_DEMO_FALLBACK,
    )
