"""Unit tests for the asks_router — auto-routing logic, lifecycle
transitions, and the Haiku-draft fallback."""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import text

from bifrost.actions import asks_router
from bifrost.actions.asks_router import (
    cancel_ask,
    create_ask,
    get_ask,
    list_asks,
    mark_answered,
    mark_escalated,
    mark_sent,
    resolve_route,
)
from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings


@pytest.fixture
def factory():
    return get_session_factory(get_settings())


async def _seed_repo(factory, *, name: str) -> str:
    repo_id = str(_uuid.uuid4())
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO repos (id, urn, name, url) "
                "VALUES (CAST(:id AS uuid), :urn, :name, :url)"
            ),
            {
                "id": repo_id,
                "urn": f"urn:bifrost:repo:{name}",
                "name": name,
                "url": f"https://github.com/AcmeCo/{name}",
            },
        )
        await session.commit()
    return repo_id


async def _seed_ownership(
    factory,
    *,
    repo_id: str,
    squad: str,
    slack: str | None = None,
    jira: str | None = None,
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO repo_ownership "
                "(repo_id, squad, slack_channel, jira_project_key, "
                " source, last_synced_at) "
                "VALUES (CAST(:r AS uuid), :s, :sc, :jp, "
                "'test', NOW())"
            ),
            {"r": repo_id, "s": squad, "sc": slack, "jp": jira},
        )
        await session.commit()


async def _cleanup(factory, *, repo_ids: list[str]) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM collab_asks WHERE asker_email LIKE 'test-%'"),
        )
        await session.execute(
            text("DELETE FROM repo_ownership WHERE repo_id::text = ANY(:r)"),
            {"r": repo_ids},
        )
        await session.execute(
            text("DELETE FROM repos WHERE id::text = ANY(:r)"),
            {"r": repo_ids},
        )
        await session.commit()


# ── resolve_route ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_route_empty_scope_returns_unrouted(factory) -> None:
    async with factory() as session:
        s = await resolve_route(session, scope_repo_ids=[])
    assert s.target_squad is None
    assert s.candidates == []


@pytest.mark.asyncio
async def test_resolve_route_picks_dominant_squad(factory) -> None:
    repos: list[str] = []
    try:
        a = await _seed_repo(factory, name="asks-test-a")
        b = await _seed_repo(factory, name="asks-test-b")
        c = await _seed_repo(factory, name="asks-test-c")
        repos = [a, b, c]
        await _seed_ownership(
            factory,
            repo_id=a,
            squad="DLVR",
            slack="#dlvr-help",
            jira="DLVR",
        )
        await _seed_ownership(
            factory,
            repo_id=b,
            squad="DLVR",
            slack="#dlvr-help",
            jira="DLVR",
        )
        await _seed_ownership(
            factory,
            repo_id=c,
            squad="PLAT",
            slack="#plat-help",
            jira="PLAT",
        )

        async with factory() as session:
            r = await resolve_route(session, scope_repo_ids=repos)
        assert r.target_squad == "DLVR"
        assert r.target_slack_channel == "#dlvr-help"
        assert r.target_jira_project == "DLVR"
        assert "matched DLVR" in r.rationale
        assert len(r.candidates) >= 2
    finally:
        await _cleanup(factory, repo_ids=repos)


@pytest.mark.asyncio
async def test_resolve_route_tied_returns_no_decision(factory) -> None:
    """When two squads tie on top vote count, we surface the candidates
    instead of guessing — UI prompts the picker."""
    repos: list[str] = []
    try:
        a = await _seed_repo(factory, name="asks-tied-a")
        b = await _seed_repo(factory, name="asks-tied-b")
        repos = [a, b]
        await _seed_ownership(factory, repo_id=a, squad="DLVR", slack="#a", jira="DLVR")
        await _seed_ownership(factory, repo_id=b, squad="PLAT", slack="#b", jira="PLAT")
        async with factory() as session:
            r = await resolve_route(session, scope_repo_ids=repos)
        assert r.target_squad is None
        assert "tied" in r.rationale.lower()
        assert len(r.candidates) == 2
    finally:
        await _cleanup(factory, repo_ids=repos)


# ── create + lifecycle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_ask_routes_and_drafts(factory, monkeypatch) -> None:
    repos: list[str] = []

    # Stub the Haiku draft so we don't hit Claude
    async def fake_draft(
        *,
        asker_email,
        question,
        target_squad,
        target_jira_project,
        target_slack_channel,
        scope_label=None,
    ) -> str:
        return f"DRAFT to {target_squad}: {question[:30]}"

    monkeypatch.setattr(asks_router, "draft_slack_message", fake_draft)

    try:
        repo = await _seed_repo(factory, name="ask-create-test")
        repos = [repo]
        await _seed_ownership(
            factory,
            repo_id=repo,
            squad="DLVR",
            slack="#dlvr-help",
            jira="DLVR",
        )

        async with factory() as session:
            ask_id = await create_ask(
                session,
                asker_email="test-asker@acmeco.example",
                question="How does SigningKeys route carrier webhooks?",
                scope_repo_ids=[repo],
            )
            await session.commit()

        async with factory() as session:
            row = await get_ask(session, ask_id=ask_id)
        assert row is not None
        assert row.target_squad == "DLVR"
        assert row.target_slack_channel == "#dlvr-help"
        assert row.status == "routed"
        assert row.slack_draft_md is not None
        assert "DRAFT to DLVR" in row.slack_draft_md
    finally:
        await _cleanup(factory, repo_ids=repos)


@pytest.mark.asyncio
async def test_lifecycle_routed_to_sent_to_answered(
    factory,
    monkeypatch,
) -> None:
    async def fake_draft(**_kwargs) -> str:
        return "_draft_"

    monkeypatch.setattr(asks_router, "draft_slack_message", fake_draft)

    repos: list[str] = []
    try:
        repo = await _seed_repo(factory, name="ask-lifecycle-test")
        repos = [repo]
        await _seed_ownership(
            factory,
            repo_id=repo,
            squad="DLVR",
            slack="#x",
            jira="DLVR",
        )

        async with factory() as session:
            ask_id = await create_ask(
                session,
                asker_email="test-life@acmeco.example",
                question="q",
                scope_repo_ids=[repo],
            )
            await session.commit()

        async with factory() as session:
            await mark_sent(
                session,
                ask_id=ask_id,
                slack_thread_url="https://slack/x",
            )
            await session.commit()
        async with factory() as session:
            row = await get_ask(session, ask_id=ask_id)
        assert row is not None and row.status == "sent"
        assert row.slack_thread_url == "https://slack/x"

        conv_id = str(_uuid.uuid4())
        # Set up a fake conversation so the FK is satisfied
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, session_id, title, scope_repo_ids) "
                    "VALUES (CAST(:id AS uuid), 'sid', 't', '[]'::jsonb)"
                ),
                {"id": conv_id},
            )
            await session.commit()

        async with factory() as session:
            await mark_answered(
                session,
                ask_id=ask_id,
                answer_conversation_id=conv_id,
            )
            await session.commit()
        async with factory() as session:
            row = await get_ask(session, ask_id=ask_id)
        assert row is not None and row.status == "answered"
        assert row.answer_conversation_id == conv_id

        # cleanup conv
        async with factory() as session:
            await session.execute(
                text("DELETE FROM conversations WHERE id = CAST(:id AS uuid)"),
                {"id": conv_id},
            )
            await session.commit()
    finally:
        await _cleanup(factory, repo_ids=repos)


@pytest.mark.asyncio
async def test_escalate_then_cancel(factory, monkeypatch) -> None:
    async def fake_draft(**_kwargs) -> str:
        return "_d_"

    monkeypatch.setattr(asks_router, "draft_slack_message", fake_draft)

    repos: list[str] = []
    try:
        repo = await _seed_repo(factory, name="ask-escalate-test")
        repos = [repo]
        await _seed_ownership(
            factory,
            repo_id=repo,
            squad="DLVR",
            slack="#x",
            jira="DLVR",
        )

        async with factory() as session:
            ask_id = await create_ask(
                session,
                asker_email="test-esc@acmeco.example",
                question="q",
                scope_repo_ids=[repo],
            )
            await session.commit()

        async with factory() as session:
            await mark_escalated(session, ask_id=ask_id)
            await session.commit()
        async with factory() as session:
            row = await get_ask(session, ask_id=ask_id)
        assert row is not None and row.status == "escalated"
        assert row.escalated_at is not None

        async with factory() as session:
            await cancel_ask(session, ask_id=ask_id)
            await session.commit()
        async with factory() as session:
            row = await get_ask(session, ask_id=ask_id)
        assert row is not None and row.status == "cancelled"
    finally:
        await _cleanup(factory, repo_ids=repos)


# ── list filter ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_asks_open_excludes_terminal(factory, monkeypatch) -> None:
    async def fake_draft(**_kwargs) -> str:
        return "_d_"

    monkeypatch.setattr(asks_router, "draft_slack_message", fake_draft)

    repos: list[str] = []
    try:
        r = await _seed_repo(factory, name="ask-list-test")
        repos = [r]
        await _seed_ownership(factory, repo_id=r, squad="DLVR", slack="#x", jira="DLVR")
        async with factory() as session:
            id1 = await create_ask(
                session,
                asker_email="test-list1@acmeco.example",
                question="q1",
                scope_repo_ids=[r],
            )
            id2 = await create_ask(
                session,
                asker_email="test-list2@acmeco.example",
                question="q2",
                scope_repo_ids=[r],
            )
            await cancel_ask(session, ask_id=id2)
            await session.commit()
        async with factory() as session:
            open_rows = await list_asks(session, status="open")
        ids = {r.id for r in open_rows}
        assert id1 in ids
        assert id2 not in ids
    finally:
        await _cleanup(factory, repo_ids=repos)
