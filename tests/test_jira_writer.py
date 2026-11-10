"""Unit tests for jira_writer — every Jira write must:

  1. Log to ``task_jira_audit`` BEFORE returning (success or failure).
  2. Surface ``JiraWriteSkipped`` when MCP isn't available so the
     caller can degrade gracefully.
  3. Surface ``JiraWriteFailed`` on MCP errors with the audit row
     already recorded.

The actual ``claude -p`` subprocess is mocked via the module's
``_run_jira_write`` symbol so we don't need a live Atlassian MCP."""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import text

import bifrost.actions.jira_writer as jw
from bifrost.actions.jira_writer import (
    JiraIssueCreate,
    JiraWriteFailed,
    JiraWriteSkipped,
    add_comment,
    create_issue,
    transition_issue,
)
from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings


@pytest.fixture
def factory():
    return get_session_factory(get_settings())


async def _seed_task(factory) -> str:
    task_id = str(_uuid.uuid4())
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO tasks (id, title, state, intent_text, "
                "created_from) "
                "VALUES (CAST(:id AS uuid), 'jira-writer test', "
                "'draft_intent', 'i', 'direct')"
            ),
            {"id": task_id},
        )
        await session.commit()
    return task_id


async def _delete_task_and_audit(factory, task_id: str) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM task_jira_audit WHERE task_id = CAST(:id AS uuid)"),
            {"id": task_id},
        )
        await session.execute(
            text("DELETE FROM tasks WHERE id = CAST(:id AS uuid)"),
            {"id": task_id},
        )
        await session.commit()


async def _audit_rows(factory, task_id: str) -> list[dict]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT action, ok, jira_key, error_message "
                        "FROM task_jira_audit "
                        "WHERE task_id = CAST(:id AS uuid) "
                        "ORDER BY created_at"
                    ),
                    {"id": task_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ── create_issue ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_issue_writes_audit_and_stamps_jira_key(
    factory,
    monkeypatch,
) -> None:
    task_id = await _seed_task(factory)
    try:

        async def fake_run(**_kwargs):
            return {
                "ok": True,
                "key": "DLVR-9999",
                "url": "https://acmeco.atlassian.net/browse/DLVR-9999",
                "raw": {"id": "10001"},
            }

        monkeypatch.setattr(jw, "_run_jira_write", fake_run)

        async with factory() as session:
            result = await create_issue(
                session,
                task_id=task_id,
                body=JiraIssueCreate(
                    project_key="DLVR",
                    summary="Test issue",
                    description="Body",
                ),
                actor="user",
            )
            await session.commit()

        assert result.key == "DLVR-9999"

        rows = await _audit_rows(factory, task_id)
        assert len(rows) == 1
        assert rows[0]["action"] == "create"
        assert rows[0]["ok"] is True
        assert rows[0]["jira_key"] == "DLVR-9999"

        # tasks.jira_key was stamped
        async with factory() as session:
            jira_key = (
                await session.execute(
                    text("SELECT jira_key FROM tasks WHERE id = CAST(:id AS uuid)"),
                    {"id": task_id},
                )
            ).scalar_one()
        assert jira_key == "DLVR-9999"
    finally:
        await _delete_task_and_audit(factory, task_id)


@pytest.mark.asyncio
async def test_create_issue_audit_recorded_on_mcp_error(
    factory,
    monkeypatch,
) -> None:
    task_id = await _seed_task(factory)
    try:

        async def fake_run(**_kwargs):
            return {"ok": False, "error": "permission denied on project DLVR"}

        monkeypatch.setattr(jw, "_run_jira_write", fake_run)

        async with factory() as session:
            with pytest.raises(JiraWriteFailed):
                await create_issue(
                    session,
                    task_id=task_id,
                    body=JiraIssueCreate(
                        project_key="DLVR",
                        summary="Test issue",
                        description="Body",
                    ),
                )
            await session.commit()

        rows = await _audit_rows(factory, task_id)
        assert len(rows) == 1
        assert rows[0]["action"] == "create"
        assert rows[0]["ok"] is False
        assert "permission denied" in (rows[0]["error_message"] or "").lower()
    finally:
        await _delete_task_and_audit(factory, task_id)


@pytest.mark.asyncio
async def test_create_issue_skipped_propagates(
    factory,
    monkeypatch,
) -> None:
    """JiraWriteSkipped (MCP not configured) bypasses the audit row
    so the caller can degrade gracefully without recording a fake
    failure for every unauthenticated dev session."""
    task_id = await _seed_task(factory)
    try:

        async def fake_run(**_kwargs):
            raise JiraWriteSkipped("Claude CLI not authenticated")

        monkeypatch.setattr(jw, "_run_jira_write", fake_run)

        async with factory() as session:
            with pytest.raises(JiraWriteSkipped):
                await create_issue(
                    session,
                    task_id=task_id,
                    body=JiraIssueCreate(
                        project_key="DLVR",
                        summary="x",
                        description="x",
                    ),
                )
            await session.commit()

        rows = await _audit_rows(factory, task_id)
        assert rows == []  # no audit row for skipped path
    finally:
        await _delete_task_and_audit(factory, task_id)


# ── transition_issue ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transition_audits_jira_state_before_after(
    factory,
    monkeypatch,
) -> None:
    task_id = await _seed_task(factory)
    try:

        async def fake_run(**_kwargs):
            return {
                "ok": True,
                "transition_id": "21",
                "jira_state_before": "To Do",
                "jira_state_after": "In Progress",
            }

        monkeypatch.setattr(jw, "_run_jira_write", fake_run)

        async with factory() as session:
            await transition_issue(
                session,
                task_id=task_id,
                jira_key="DLVR-1234",
                target_state="In Progress",
                actor="system",
                before_state="plan_ready",
                after_state="executing",
            )
            await session.commit()

        rows = await _audit_rows(factory, task_id)
        assert len(rows) == 1
        assert rows[0]["action"] == "transition"
        assert rows[0]["ok"] is True
    finally:
        await _delete_task_and_audit(factory, task_id)


@pytest.mark.asyncio
async def test_transition_failure_records_audit_and_raises(
    factory,
    monkeypatch,
) -> None:
    task_id = await _seed_task(factory)
    try:

        async def fake_run(**_kwargs):
            return {"ok": False, "error": "no path from To Do → Done"}

        monkeypatch.setattr(jw, "_run_jira_write", fake_run)

        async with factory() as session:
            with pytest.raises(JiraWriteFailed):
                await transition_issue(
                    session,
                    task_id=task_id,
                    jira_key="DLVR-1234",
                    target_state="Done",
                )
            await session.commit()

        rows = await _audit_rows(factory, task_id)
        assert len(rows) == 1
        assert rows[0]["ok"] is False
    finally:
        await _delete_task_and_audit(factory, task_id)


# ── add_comment ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_comment_audits(factory, monkeypatch) -> None:
    task_id = await _seed_task(factory)
    try:

        async def fake_run(**_kwargs):
            return {"ok": True, "comment_id": "c-12345"}

        monkeypatch.setattr(jw, "_run_jira_write", fake_run)

        async with factory() as session:
            await add_comment(
                session,
                task_id=task_id,
                jira_key="DLVR-1234",
                body="Bifrost is now executing on branch `bifrost/abc`.",
            )
            await session.commit()

        rows = await _audit_rows(factory, task_id)
        assert len(rows) == 1
        assert rows[0]["action"] == "comment"
        assert rows[0]["ok"] is True
    finally:
        await _delete_task_and_audit(factory, task_id)


# ── find_jira_key_in_text helper ────────────────────────────────────


def test_find_jira_key_extracts_first_canonical_key() -> None:
    assert jw.find_jira_key_in_text("see DLVR-1234 for context") == "DLVR-1234"
    assert jw.find_jira_key_in_text("dlvr-99 lowercase doesn't match") is None
    assert jw.find_jira_key_in_text("") is None
    assert jw.find_jira_key_in_text(None) is None  # type: ignore[arg-type]
