"""Unit tests for the PR observer — single-cycle behaviour with a
fake gh runner."""

from __future__ import annotations

import json
import uuid as _uuid

import pytest
from sqlalchemy import text

from bifrost.actions.pr_observer import _looks_like_gh_pr_url, poll_one_cycle
from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings

# ── pure helpers ─────────────────────────────────────────────────────


def test_looks_like_gh_pr_url_accepts_canonical_form() -> None:
    assert _looks_like_gh_pr_url(
        "https://github.com/AcmeCo/bifrost/pull/1234",
    )
    assert _looks_like_gh_pr_url(
        "http://github.com/AcmeCo/bifrost/pull/1",
    )


def test_looks_like_gh_pr_url_rejects_other_shapes() -> None:
    assert not _looks_like_gh_pr_url("")
    assert not _looks_like_gh_pr_url("https://github.com/AcmeCo/bifrost")
    assert not _looks_like_gh_pr_url("https://gitlab.com/x/y/pull/1")
    assert not _looks_like_gh_pr_url(
        "https://github.com/AcmeCo/bifrost/issues/1234",
    )


# ── DB-backed cycle tests ──────────────────────────────────────────


@pytest.fixture
def factory():
    return get_session_factory(get_settings())


async def _seed_success_task_with_pr(
    factory,
    *,
    pr_url: str,
) -> str:
    """Insert a task in SUCCESS with an open PR and return its id."""
    task_id = str(_uuid.uuid4())
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO tasks (id, title, state, intent_text, "
                "created_from, pr_url) "
                "VALUES (CAST(:id AS uuid), 'pr-observer test', "
                "'success', 'i', 'direct', :url)"
            ),
            {"id": task_id, "url": pr_url},
        )
        await session.commit()
    return task_id


async def _delete_task(factory, task_id: str) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM tasks WHERE id = CAST(:id AS uuid)"),
            {"id": task_id},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_observer_no_op_when_no_open_pr_tasks(factory) -> None:
    counts = await poll_one_cycle(factory, gh_runner=_unused_gh)
    assert counts["merged"] == 0


@pytest.mark.asyncio
async def test_observer_transitions_merged_pr_to_pr_merged(factory) -> None:
    pr = "https://github.com/AcmeCo/bifrost/pull/4242"
    task_id = await _seed_success_task_with_pr(factory, pr_url=pr)
    try:

        async def fake_gh(argv: list[str]):
            assert argv[0] == "gh"
            assert pr in argv
            payload = json.dumps({"state": "MERGED", "mergedAt": "2026-05-04T18:00:00Z"})
            return 0, payload.encode("utf-8"), b""

        counts = await poll_one_cycle(factory, gh_runner=fake_gh)
        assert counts["merged"] == 1

        # Task should now be in pr_merged.
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM tasks WHERE id = CAST(:id AS uuid)"),
                    {"id": task_id},
                )
            ).first()
        assert row is not None
        assert row[0] == "pr_merged"
    finally:
        await _delete_task(factory, task_id)


@pytest.mark.asyncio
async def test_observer_skips_open_prs(factory) -> None:
    pr = "https://github.com/AcmeCo/bifrost/pull/4243"
    task_id = await _seed_success_task_with_pr(factory, pr_url=pr)
    try:

        async def fake_gh(argv: list[str]):
            payload = json.dumps({"state": "OPEN", "mergedAt": None})
            return 0, payload.encode("utf-8"), b""

        counts = await poll_one_cycle(factory, gh_runner=fake_gh)
        assert counts["merged"] == 0
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM tasks WHERE id = CAST(:id AS uuid)"),
                    {"id": task_id},
                )
            ).first()
        assert row[0] == "success"  # unchanged
    finally:
        await _delete_task(factory, task_id)


@pytest.mark.asyncio
async def test_observer_handles_gh_failure_gracefully(factory) -> None:
    pr = "https://github.com/AcmeCo/bifrost/pull/4244"
    task_id = await _seed_success_task_with_pr(factory, pr_url=pr)
    try:

        async def fake_gh(_argv: list[str]):
            return 1, b"", b"gh: not authenticated"

        counts = await poll_one_cycle(factory, gh_runner=fake_gh)
        # gh failure → no merged transitions, but no exception
        assert counts["merged"] == 0
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM tasks WHERE id = CAST(:id AS uuid)"),
                    {"id": task_id},
                )
            ).first()
        assert row[0] == "success"
    finally:
        await _delete_task(factory, task_id)


async def _unused_gh(_argv: list[str]):
    raise AssertionError("gh runner should not have been called")
