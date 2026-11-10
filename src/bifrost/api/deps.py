"""FastAPI dependency injection providers.

Provides async DB sessions and settings as FastAPI dependencies.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bifrost.api.auth.middleware import current_principal
from bifrost.api.auth.principal import ROLE_ADMIN, ROLES, Principal
from bifrost.api.auth.roles import RoleRequired
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.storage.repository import EntityRepository

# Module-level engine and session factory — initialized lazily.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory(settings: BifrostSettings) -> async_sessionmaker[AsyncSession]:
    """Create or return the cached async session factory.

    Public so that background workers (e.g. ExtractionWorker) can create
    their own short-lived sessions independent of request scope.
    """
    global _engine, _session_factory  # noqa: PLW0603
    if _session_factory is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
        )
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db(
    settings: Annotated[BifrostSettings, Depends(get_settings)],
) -> AsyncIterator[AsyncSession]:
    """Yield an async DB session, rolling back on error.

    Post-commit hook firing: after a successful ``commit()``, drains
    any pending Jira-sync hooks stashed on the session by
    ``task_lifecycle.transition()``. The hooks open NEW sessions so
    they see the just-committed state — Jira projections never race
    against the originating transition.
    """
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
            # Drain any post-commit Jira hooks. Failures here are
            # logged inside fire_pending_jira_hooks and never
            # propagate — Jira is a projection, never load-bearing.
            try:
                from bifrost.actions.task_lifecycle import (
                    fire_pending_jira_hooks,
                )

                await fire_pending_jira_hooks(
                    session,
                    session_factory=factory,
                )
            except Exception:  # noqa: BLE001
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "post-commit Jira hook drain failed (non-fatal)",
                )
        except Exception:
            await session.rollback()
            raise


async def get_repository(
    settings: Annotated[BifrostSettings, Depends(get_settings)],
) -> AsyncIterator[EntityRepository]:
    """Yield an EntityRepository backed by an async DB session.

    Under ``identity.provider=none`` the single local operator owns every
    conversation, so the per-session ownership check is skipped.
    """
    factory = get_session_factory(settings)
    skip_session_check = settings.identity.provider == "none"
    async with factory() as session:
        repo = EntityRepository(session, skip_session_check=skip_session_check)
        try:
            yield repo
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@functools.cache
def require_role(role: str) -> Callable[[Request], Awaitable[Principal]]:
    """Dependency factory: the caller must hold ``role``.

    Every decision is written to the audit log with the actor, so a
    mutation behind this gate can be traced to a principal. The returned
    dependency is cached per role so FastAPI runs it once per request.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; roles are {ROLES}")

    async def _gate(request: Request) -> Principal:
        # Lazy import: bifrost.api.audit imports from this module (for
        # get_session_factory), so a top-level import would deadlock.
        from bifrost.api.audit import record_event

        principal = current_principal(request)
        common = {
            "actor": principal.email,
            "resource": str(request.url.path),
            "method": request.method,
            "correlation_id": getattr(request.state, "correlation_id", None),
            "source_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }
        metadata = {
            "required_role": role,
            "roles": list(principal.roles),
            "source": principal.source,
        }
        if not principal.has_role(role):
            await record_event(
                event_type=f"{role}.access.denied", status_code=403, metadata=metadata, **common
            )
            raise RoleRequired(f"this action requires the {role} role", details=metadata)
        await record_event(
            event_type=f"{role}.access.granted", status_code=200, metadata=metadata, **common
        )
        return principal

    _gate.required_role = role  # type: ignore[attr-defined]
    _gate.__name__ = f"require_{role}"
    return _gate


# Type aliases for use in route signatures
Settings = Annotated[BifrostSettings, Depends(get_settings)]
DB = Annotated[AsyncSession, Depends(get_db)]
Repo = Annotated[EntityRepository, Depends(get_repository)]
AdminOnly = Depends(require_role(ROLE_ADMIN))
