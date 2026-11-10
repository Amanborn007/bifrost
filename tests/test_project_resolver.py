"""Unit tests for project_resolver — picks the right Jira project from
explicit override / existing jira_key / conversation scope."""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import text

from bifrost.actions.project_resolver import (
    all_known_projects,
    project_from_jira_key,
    resolve_for_task,
)
from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings

# ── pure helpers — no DB required ───────────────────────────────────


def test_project_from_jira_key_extracts_prefix() -> None:
    assert project_from_jira_key("DLVR-1234") == "DLVR"
    assert project_from_jira_key("plat-99") == "PLAT"


def test_project_from_jira_key_rejects_garbage() -> None:
    assert project_from_jira_key(None) is None
    assert project_from_jira_key("") is None
    assert project_from_jira_key("not a key") is None
    assert project_from_jira_key("DLVR-abc") is None
    assert project_from_jira_key("123-456") is None  # prefix must start alpha


# ── DB-backed tests ──────────────────────────────────────────────────


@pytest.fixture
def factory():
    return get_session_factory(get_settings())


@pytest.mark.asyncio
async def test_resolve_for_task_explicit_override_wins(factory) -> None:
    """An explicit project_key short-circuits all other resolution."""
    async with factory() as session:
        result = await resolve_for_task(
            session,
            task_id=str(_uuid.uuid4()),  # bogus id; never queried
            explicit="GIVEN",
        )
    assert result is not None
    assert result.project_key == "GIVEN"
    assert result.source == "explicit"


@pytest.mark.asyncio
async def test_resolve_for_task_returns_none_when_task_missing(
    factory,
) -> None:
    async with factory() as session:
        result = await resolve_for_task(
            session,
            task_id=str(_uuid.uuid4()),
        )
    assert result is None


@pytest.mark.asyncio
async def test_resolve_for_task_derives_from_existing_jira_key(
    factory,
) -> None:
    """If a task is already linked to DLVR-NNN, derive project=DLVR."""
    task_id = str(_uuid.uuid4())
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO tasks (id, title, state, intent_text, "
                "created_from, jira_key) "
                "VALUES (CAST(:id AS uuid), 'test', 'draft_intent', "
                "'i', 'direct', :k)"
            ),
            {"id": task_id, "k": "DLVR-1234"},
        )
        await session.commit()
    try:
        async with factory() as session:
            r = await resolve_for_task(session, task_id=task_id)
        assert r is not None
        assert r.project_key == "DLVR"
        assert r.source == "jira_key"
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM tasks WHERE id = CAST(:id AS uuid)"),
                {"id": task_id},
            )
            await session.commit()


@pytest.mark.asyncio
async def test_all_known_projects_returns_real_data(factory) -> None:
    """Smoke — returns the live ``repo_ownership.jira_project_key``
    set ordered by repo count."""
    async with factory() as session:
        projects = await all_known_projects(session)
    # Bifrost dev DB has many repos — at minimum some projects exist
    assert isinstance(projects, list)
    # Each is uppercase
    for p in projects:
        assert p == p.upper()
