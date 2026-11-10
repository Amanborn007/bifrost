"""Role resolution and the error raised when a role is missing.

Two roles exist. Every authenticated principal is a ``member``; ``admin``
comes from an IdP group listed under ``identity.roles.admin``, from an API
token's ``roles``, or from the ``none`` provider's local operator.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection

from bifrost.api.auth.principal import ROLE_ADMIN, ROLE_MEMBER, ROLES, Principal
from bifrost.errors import AuthError


class RoleRequired(AuthError):
    """The caller is authenticated but lacks the role the route needs."""

    code = "auth.role_required"
    http_status = 403


def canonical_roles(granted: Collection[str]) -> tuple[str, ...]:
    """Every principal is a member; order follows ``ROLES``."""
    wanted = {ROLE_MEMBER, *granted}
    return tuple(r for r in ROLES if r in wanted)


def assign_roles(
    principal: Principal, *, provider_name: str, admin_groups: Collection[str]
) -> Principal:
    """Return ``principal`` with its roles resolved. Called once per request."""
    is_admin = provider_name == "none" or any(g in admin_groups for g in principal.groups)
    return dataclasses.replace(principal, roles=canonical_roles([ROLE_ADMIN] if is_admin else []))
