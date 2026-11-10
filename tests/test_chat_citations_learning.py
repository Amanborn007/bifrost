"""Tests for the chat-citations → confidence_deltas wire-up.

Regression: the Learning admin tab was effectively
empty because nothing in the chat path ever called
``learning.record_delta``. This wire-up writes one
``audit_confirmed`` delta per entity URN the synthesizer walked,
so the dashboard fills with real signal and the operator can see
what's being cited where.

Tests use the live dev DB with a unique URN namespace
(``urn:bifrost:repo/test-chat-citations-{run_id}/...``) and a
finally-block sweep so multiple runs don't accumulate fixture rows.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import delete, select, text

from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings
from bifrost.research.coordinator import _record_chat_citations
from bifrost.storage.models import Entity, Repo


def _factory():
    return get_session_factory(get_settings())


async def _seed_entities(repo_id: _uuid.UUID, repo_name: str, n: int) -> list[str]:
    """Insert ``n`` test entity rows; return their URNs in order."""
    f = _factory()
    urns: list[str] = []
    async with f() as s:
        for i in range(n):
            urn = f"urn:bifrost:repo/{repo_name}/component/T{i:03d}"
            urns.append(urn)
            s.add(
                Entity(
                    id=_uuid.uuid4(),
                    urn=urn,
                    name=f"T{i:03d}",
                    entity_type="component",
                    scope_repo_id=repo_id,
                    confidence=0.5,
                    confidence_level="inferred",
                )
            )
        await s.commit()
    return urns


async def _cleanup_entities_and_deltas(urns: list[str]) -> None:
    f = _factory()
    async with f() as s:
        ent_rows = (await s.execute(select(Entity.id).where(Entity.urn.in_(urns)))).scalars().all()
        if ent_rows:
            await s.execute(
                text("DELETE FROM confidence_deltas WHERE target_id = ANY(:ids)"),
                {"ids": [str(i) for i in ent_rows]},
            )
        await s.execute(delete(Entity).where(Entity.urn.in_(urns)))
        await s.commit()


# ── Trigger conditions ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_urn_list_is_noop() -> None:
    """No URNs → no DB activity, no error."""
    f = _factory()
    conv_id = _uuid.uuid4()
    # Just confirm it doesn't raise + doesn't write anything.
    async with f() as s:
        before = (await s.execute(text("SELECT count(*) FROM confidence_deltas"))).scalar()
    await _record_chat_citations(
        session_factory=f,
        conversation_id=conv_id,
        walked_entity_urns=[],
    )
    async with f() as s:
        after = (await s.execute(text("SELECT count(*) FROM confidence_deltas"))).scalar()
    assert after == before


@pytest.mark.asyncio
async def test_records_delta_per_resolved_urn() -> None:
    """Each cited URN that resolves to an entity gets one
    ``confidence_deltas`` row with reason ``audit_confirmed``."""
    f = _factory()
    test_id = _uuid.uuid4().hex[:8]
    repo_name = f"test-chat-citations-{test_id}"
    repo_id = _uuid.uuid4()
    conv_id = _uuid.uuid4()
    async with f() as s:
        s.add(
            Repo(
                id=repo_id,
                urn=f"urn:bifrost:repo/{repo_name}",
                name=repo_name,
                url=f"https://example.invalid/{repo_name}",
                adapter_config={},
                is_primary=False,
            )
        )
        await s.commit()

    urns = await _seed_entities(repo_id, repo_name, 3)
    try:
        await _record_chat_citations(
            session_factory=f,
            conversation_id=conv_id,
            walked_entity_urns=urns,
        )
        # 3 deltas, all reason=audit_confirmed, all for our entities.
        async with f() as s:
            rows = (
                await s.execute(
                    text(
                        """
                SELECT cd.reason, cd.applied_by, e.urn
                FROM confidence_deltas cd
                JOIN entities e ON e.id = cd.target_id
                WHERE e.urn = ANY(:urns)
                ORDER BY e.urn
                """,
                    ),
                    {"urns": urns},
                )
            ).all()
        assert len(rows) == 3
        for r in rows:
            d = dict(r._mapping)
            assert d["reason"] == "audit_confirmed"
            # actor encoded into applied_by (noise:chat:<conv_id>).
            assert f"chat:{conv_id}" in d["applied_by"]
    finally:
        async with f() as s:
            await s.execute(delete(Repo).where(Repo.id == repo_id))
            await s.commit()
        await _cleanup_entities_and_deltas(urns)


@pytest.mark.asyncio
async def test_unresolved_urns_skip_cleanly() -> None:
    """URNs that don't match any entity are skipped without erroring."""
    f = _factory()
    conv_id = _uuid.uuid4()
    fake_urns = [
        "urn:bifrost:repo/never-existed/component/X",
        "urn:bifrost:repo/never-existed/component/Y",
    ]
    async with f() as s:
        before = (await s.execute(text("SELECT count(*) FROM confidence_deltas"))).scalar()
    await _record_chat_citations(
        session_factory=f,
        conversation_id=conv_id,
        walked_entity_urns=fake_urns,
    )
    async with f() as s:
        after = (await s.execute(text("SELECT count(*) FROM confidence_deltas"))).scalar()
    # 0 new deltas because no URNs resolved.
    assert after == before


@pytest.mark.asyncio
async def test_cap_limits_inserts_to_max_urns() -> None:
    """A 200-URN walk caps at ``max_urns`` deltas — bounds the
    per-turn DB cost."""
    f = _factory()
    test_id = _uuid.uuid4().hex[:8]
    repo_name = f"test-chat-citations-cap-{test_id}"
    repo_id = _uuid.uuid4()
    conv_id = _uuid.uuid4()
    async with f() as s:
        s.add(
            Repo(
                id=repo_id,
                urn=f"urn:bifrost:repo/{repo_name}",
                name=repo_name,
                url=f"https://example.invalid/{repo_name}",
                adapter_config={},
                is_primary=False,
            )
        )
        await s.commit()

    urns = await _seed_entities(repo_id, repo_name, 10)
    try:
        await _record_chat_citations(
            session_factory=f,
            conversation_id=conv_id,
            walked_entity_urns=urns,
            max_urns=4,
        )
        async with f() as s:
            count = (
                await s.execute(
                    text(
                        """
                SELECT count(*) FROM confidence_deltas cd
                JOIN entities e ON e.id = cd.target_id
                WHERE e.urn = ANY(:urns)
                """,
                    ),
                    {"urns": urns},
                )
            ).scalar()
        assert count == 4
    finally:
        async with f() as s:
            await s.execute(delete(Repo).where(Repo.id == repo_id))
            await s.commit()
        await _cleanup_entities_and_deltas(urns)


@pytest.mark.asyncio
async def test_chat_citations_dont_shift_confidence() -> None:
    """Critical contract: chat citations record audit trail but
    DO NOT move ``entities.confidence``. Confidence shifts require
    a verified task confirmation, not a chat turn."""
    f = _factory()
    test_id = _uuid.uuid4().hex[:8]
    repo_name = f"test-chat-citations-no-shift-{test_id}"
    repo_id = _uuid.uuid4()
    _uuid.uuid4()
    async with f() as s:
        s.add(
            Repo(
                id=repo_id,
                urn=f"urn:bifrost:repo/{repo_name}",
                name=repo_name,
                url=f"https://example.invalid/{repo_name}",
                adapter_config={},
                is_primary=False,
            )
        )
        await s.commit()

    urns = await _seed_entities(repo_id, repo_name, 1)
    try:
        # Capture confidence before.
        async with f() as s:
            before = (
                await s.execute(select(Entity.confidence).where(Entity.urn == urns[0]))
            ).scalar()

        # Cite the URN 5 times across 5 different "conversations".
        for _ in range(5):
            await _record_chat_citations(
                session_factory=f,
                conversation_id=_uuid.uuid4(),  # distinct conv each time
                walked_entity_urns=urns,
            )

        async with f() as s:
            after = (
                await s.execute(select(Entity.confidence).where(Entity.urn == urns[0]))
            ).scalar()
            delta_count = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM confidence_deltas cd "
                        "JOIN entities e ON e.id = cd.target_id "
                        "WHERE e.urn = :urn"
                    ),
                    {"urn": urns[0]},
                )
            ).scalar()
        # Audit rows landed (5 of them).
        assert delta_count == 5
        # But entity confidence unchanged — chat-only deltas don't
        # move the underlying number.
        assert after == before
    finally:
        async with f() as s:
            await s.execute(delete(Repo).where(Repo.id == repo_id))
            await s.commit()
        await _cleanup_entities_and_deltas(urns)
