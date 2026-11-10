"""Tests for the research_runs state-machine helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.research.runs import (
    IDEMPOTENCY_TTL,
    find_by_idempotency,
    find_in_flight_run,
    find_latest_run,
    mark_cancelled,
    mark_done,
    mark_failed,
    mark_phase,
    reclaim_orphaned_runs,
    start_run,
)
from bifrost.storage.repository import EntityRepository


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_conversation(session_factory: async_sessionmaker) -> uuid.UUID:
    async with session_factory() as session:
        repo = EntityRepository(session)
        conv = await repo.create_conversation(
            session_id="rr-session",
            title="research runs test",
            scope_repo_ids=[],
        )
        await session.commit()
        return conv.id


@pytest.mark.asyncio
async def test_start_and_advance_phases(session_factory_for) -> None:
    cid = await _make_conversation(session_factory_for)
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
        idempotency_key="abc-123",
    )
    assert isinstance(run_id, uuid.UUID)

    async with session_factory_for() as session:
        rec = await find_in_flight_run(session, cid)
    assert rec is not None
    assert rec.id == run_id
    assert rec.status == "pending"

    await mark_phase(
        session_factory_for,
        run_id,
        "scoping",
        phase_data_patch={"scope_repos": ["orders"]},
    )
    await mark_phase(session_factory_for, run_id, "fanout")
    await mark_phase(session_factory_for, run_id, "synthesizing")

    async with session_factory_for() as session:
        rec = await find_in_flight_run(session, cid)
    assert rec is not None
    assert rec.status == "synthesizing"
    assert rec.phase_data == {"scope_repos": ["orders"]}


@pytest.mark.asyncio
async def test_mark_done_clears_in_flight(session_factory_for) -> None:
    cid = await _make_conversation(session_factory_for)
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
    )
    await mark_done(
        session_factory_for,
        run_id,
        final_answer_text="The answer is 42.",
        total_cost_usd=2.34,
    )

    async with session_factory_for() as session:
        in_flight = await find_in_flight_run(session, cid)
        latest = await find_latest_run(session, cid)
    assert in_flight is None
    assert latest is not None
    assert latest.status == "done"
    assert latest.final_answer_text == "The answer is 42."
    assert latest.total_cost_usd == 2.34
    assert latest.finished_at is not None


@pytest.mark.asyncio
async def test_mark_failed_records_error(session_factory_for) -> None:
    cid = await _make_conversation(session_factory_for)
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
    )
    await mark_failed(
        session_factory_for,
        run_id,
        error_message="Synthesis blew up: connection reset.",
    )
    async with session_factory_for() as session:
        latest = await find_latest_run(session, cid)
    assert latest is not None
    assert latest.status == "failed"
    assert "Synthesis blew up" in (latest.error_message or "")


@pytest.mark.asyncio
async def test_mark_cancelled_flips_in_flight_row(session_factory_for) -> None:
    """A cancel against a row mid-fanout must transition it to
    ``cancelled``. This is the fix for the Stop-button bug where a
    page refresh after Stop re-attached to a stale 'in flight' row
    and rendered a phantom pipeline."""
    cid = await _make_conversation(session_factory_for)
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
    )
    await mark_phase(session_factory_for, run_id, "fanout")

    changed = await mark_cancelled(session_factory_for, run_id)
    assert changed is True

    async with session_factory_for() as session:
        in_flight = await find_in_flight_run(session, cid)
        latest = await find_latest_run(session, cid)
    assert in_flight is None
    assert latest is not None
    assert latest.status == "cancelled"
    assert latest.finished_at is not None


@pytest.mark.asyncio
async def test_mark_cancelled_does_not_trample_done(session_factory_for) -> None:
    """If the worker finished naturally a hair before /cancel landed,
    the row is already ``done``. mark_cancelled must NOT overwrite a
    real terminal state — the user would otherwise see a successful
    answer flip to cancelled. Returns False to signal no-op."""
    cid = await _make_conversation(session_factory_for)
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
    )
    await mark_done(
        session_factory_for,
        run_id,
        final_answer_text="real answer",
    )

    changed = await mark_cancelled(session_factory_for, run_id)
    assert changed is False

    async with session_factory_for() as session:
        latest = await find_latest_run(session, cid)
    assert latest is not None
    assert latest.status == "done"
    assert latest.final_answer_text == "real answer"


@pytest.mark.asyncio
async def test_idempotency_returns_recent_done_run(session_factory_for) -> None:
    cid = await _make_conversation(session_factory_for)
    key = f"unique-{uuid.uuid4()}"
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
        idempotency_key=key,
    )
    await mark_done(
        session_factory_for,
        run_id,
        final_answer_text="cached!",
    )

    async with session_factory_for() as session:
        rec = await find_by_idempotency(session, cid, key)
    assert rec is not None
    assert rec.id == run_id
    assert rec.final_answer_text == "cached!"


@pytest.mark.asyncio
async def test_idempotency_ignores_failed_runs(session_factory_for) -> None:
    cid = await _make_conversation(session_factory_for)
    key = f"failed-{uuid.uuid4()}"
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
        idempotency_key=key,
    )
    await mark_failed(session_factory_for, run_id, error_message="boom")

    async with session_factory_for() as session:
        rec = await find_by_idempotency(session, cid, key)
    assert rec is None


@pytest.mark.asyncio
async def test_reclaim_orphaned_runs(session_factory_for) -> None:
    """A run started >10min ago that's still in_flight should get
    reclaimed to ``failed`` so a fresh POST isn't blocked forever."""
    cid = await _make_conversation(session_factory_for)
    run_id = await start_run(
        session_factory_for,
        conversation_id=cid,
        turn_id=1,
    )
    await mark_phase(session_factory_for, run_id, "fanout")

    # Backdate it artificially.
    from sqlalchemy import update as sql_update

    from bifrost.storage.models import ResearchRun

    async with session_factory_for() as session:
        await session.execute(
            sql_update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .values(started_at=datetime.now(UTC) - timedelta(minutes=30)),
        )
        await session.commit()

    n = await reclaim_orphaned_runs(
        session_factory_for,
        older_than=timedelta(minutes=10),
    )
    assert n >= 1

    async with session_factory_for() as session:
        latest = await find_latest_run(session, cid)
    assert latest is not None
    assert latest.status == "failed"


@pytest.mark.asyncio
async def test_unique_constraint_on_conv_turn(session_factory_for) -> None:
    """Two runs for the same (conv, turn) must collide. Caller resumes
    the existing run via find_in_flight_run instead."""
    cid = await _make_conversation(session_factory_for)
    await start_run(session_factory_for, conversation_id=cid, turn_id=7)

    with pytest.raises(Exception):  # IntegrityError or wrapper
        await start_run(session_factory_for, conversation_id=cid, turn_id=7)


@pytest.mark.asyncio
async def test_idempotency_ttl_constant() -> None:
    """Sanity: keys older than TTL should be ignored. Document the
    intended duration so future tweaks are visible in test output."""
    assert timedelta(hours=24) == IDEMPOTENCY_TTL
