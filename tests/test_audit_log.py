"""Tests for the central append-only ``audit_log``.

Covers the AuditLog model round-trips, and the append-only trigger blocks
UPDATE / DELETE.

Note: the test schema is built via ``Base.metadata.create_all`` (not
Alembic), so the append-only trigger from the baseline migration is not present
by default. The trigger tests install the trigger DDL themselves (the
same SQL the migration runs) so they exercise the real guard logic.
Inserts use ``flush`` (never ``commit``) to preserve the per-test
rollback isolation in ``conftest``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text, update

from bifrost.storage.models import AuditLog

# Same DDL the baseline migration installs.
_TRIGGER_DDL = (
    """
    CREATE OR REPLACE FUNCTION audit_log_prevent_mutation()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only; UPDATE/DELETE blocked'
            USING ERRCODE = 'check_violation';
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;",
    """
    CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_prevent_mutation();
    """,
    "DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;",
    """
    CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_prevent_mutation();
    """,
)


async def _install_trigger(session) -> None:
    for ddl in _TRIGGER_DDL:
        await session.execute(text(ddl))
    await session.flush()


def _is_append_only_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "append-only" in msg or "check_violation" in msg


@pytest.mark.asyncio
async def test_audit_row_inserts_and_reads_back(db_session) -> None:
    row = AuditLog(
        event_type="admin.access.denied",
        actor="alice@acmeco.example",
        resource="/api/v1/admin/learning",
        method="GET",
        status_code=401,
        correlation_id="cid-123",
        source_ip="10.1.2.3",
        user_agent="pytest",
        audit_metadata={"reason": "missing header"},
    )
    db_session.add(row)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(AuditLog).where(AuditLog.correlation_id == "cid-123"))
    ).scalar_one()
    assert fetched.event_type == "admin.access.denied"
    assert fetched.actor == "alice@acmeco.example"
    assert fetched.status_code == 401
    assert fetched.audit_metadata["reason"] == "missing header"
    assert fetched.id is not None  # gen_random_uuid() server default
    assert fetched.timestamp is not None  # now() server default


@pytest.mark.asyncio
async def test_append_only_blocks_update(db_session) -> None:
    await _install_trigger(db_session)
    row = AuditLog(event_type="admin.access.granted", actor="a@b.com")
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(Exception) as exc:  # noqa: PT011 -- DBAPIError subclass
        await db_session.execute(
            update(AuditLog).where(AuditLog.id == row.id).values(actor="evil@x.com")
        )
        await db_session.flush()
    assert _is_append_only_error(exc.value)


@pytest.mark.asyncio
async def test_append_only_blocks_delete(db_session) -> None:
    await _install_trigger(db_session)
    row = AuditLog(event_type="admin.access.granted", actor="a@b.com")
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(Exception) as exc:  # noqa: PT011 -- DBAPIError subclass
        await db_session.execute(delete(AuditLog).where(AuditLog.id == row.id))
        await db_session.flush()
    assert _is_append_only_error(exc.value)
