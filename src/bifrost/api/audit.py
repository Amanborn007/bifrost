"""Audit log recorder -- writes one row to ``audit_log`` per security event.

S11 section 16.1 / S12 section 9.3. The table is append-only at the DB
layer (the baseline migration installs triggers that raise on UPDATE/DELETE), so
callers don't need to worry about mutation safety.

Design notes:

  * One small async helper, ``record_event``, used by any middleware or
    route that wants to log. Opens its own short-lived session so it
    doesn't entangle with the request's transaction (a 500 in the
    business logic must not roll back an audit row -- the whole point
    of the audit table is that it captures attempted access regardless
    of outcome).

  * Best-effort: any DB failure logs a warning and returns silently.
    Refusing the request because audit couldn't write would be a
    self-DoS vector. Audit writes should add observability, not
    create new failure modes.

  * Event names use a dotted namespace (auth.identity.missing,
    admin.access.denied, permission.denied) so future filter UIs can
    group by prefix.
"""

from __future__ import annotations

import logging
from typing import Any

from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings
from bifrost.storage.models import AuditLog

logger = logging.getLogger(__name__)


async def record_event(
    *,
    event_type: str,
    actor: str | None = None,
    resource: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    correlation_id: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a single audit_log row. Best-effort; swallows DB errors.

    Callers should pass whatever they have honest values for. None
    means "did not measure," not "value is empty."
    """
    try:
        factory = get_session_factory(get_settings())
        async with factory() as session:
            row = AuditLog(
                event_type=event_type,
                actor=actor,
                resource=resource,
                method=method,
                status_code=status_code,
                correlation_id=correlation_id,
                source_ip=source_ip,
                user_agent=user_agent,
                audit_metadata=metadata or {},
            )
            session.add(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 -- intentional fire-and-forget
        # Audit writes must never break a request. Log loudly so it shows
        # up in dashboards, but do not propagate.
        logger.warning(
            "audit.record_failed event_type=%s actor=%s error=%s",
            event_type,
            actor,
            exc,
        )
