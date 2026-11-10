"""Tests for the chat-research write path: ``chat_audit``.

Uses the live dev DB (same pattern as ``test_chat_citations_learning.py``)
because ``confidence_deltas`` lives in the action-layer tables that are
only created by alembic, not by ``Base.metadata.create_all()``. Each
test seeds its own URN namespace and cleans up in a finally block so
multiple runs don't accumulate fixture rows.

Covers:
1. ``edge_verifications`` (verified=true) → records ``audit_confirmed``
   delta with the chat-research origin pointers (conversation_id +
   research_run_id).
2. ``edge_verifications`` (verified=false) → records
   ``audit_contradicted`` and the relationship's confidence drops
   immediately (governance: downgrades are immediate).
3. ``missed_edges`` → new ``EntityRelationship`` row inserted with
   ``code_verified`` (T1) provenance + the chat-research back-pointer
   in ``source_basis[*].method``.
4. Re-running the same audit is idempotent (no duplicate edge rows).
5. Unresolvable component names get counted as ``edges_skipped`` rather
   than crashing.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text

from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings
from bifrost.research.chat_audit import chat_research_audit
from bifrost.research.repo_agent import (
    EdgeVerification,
    MissedEdge,
    RepoReport,
)
from bifrost.storage.models import Conversation, Entity, EntityRelationship, Repo


def _factory():
    return get_session_factory(get_settings())


async def _seed_conversation() -> _uuid.UUID:
    """Insert a fixture Conversation so the FK from confidence_deltas
    to conversations is satisfied.
    """
    f = _factory()
    cid = _uuid.uuid4()
    async with f() as s:
        s.add(
            Conversation(
                id=cid,
                session_id=f"audit-test-{cid.hex[:8]}",
                title="chat_audit fixture",
                scope_repo_ids=[],
            )
        )
        await s.commit()
    return cid


async def _cleanup_conversation(cid: _uuid.UUID) -> None:
    f = _factory()
    async with f() as s:
        await s.execute(
            text("DELETE FROM confidence_deltas WHERE conversation_id = :cid"), {"cid": str(cid)}
        )
        await s.execute(text("DELETE FROM conversations WHERE id = :cid"), {"cid": str(cid)})
        await s.commit()


async def _seed_two_components_with_edge(
    *,
    rel_type: str = "calls",
    initial_confidence: float = 0.5,
) -> tuple[_uuid.UUID, str, str, _uuid.UUID, _uuid.UUID, list[str]]:
    """Seed: 1 repo, 2 components in it, 1 edge between them.

    Returns (repo_id, src_name, tgt_name, src_entity_id, edge_id, urns).
    Caller passes ``urns`` to ``_cleanup_audit_state`` in a finally block.
    """
    suffix = _uuid.uuid4().hex[:8]
    src_name = f"OrderRouter-{suffix}"
    tgt_name = f"VendorClient-{suffix}"
    repo_full = f"AcmeApp-{suffix}"
    urn_src = f"urn:bifrost:repo/{repo_full}/component/{src_name}"
    urn_tgt = f"urn:bifrost:repo/{repo_full}/component/{tgt_name}"
    urn_repo = f"urn:bifrost:repo/{repo_full}"

    f = _factory()
    async with f() as s:
        repo = Repo(
            id=_uuid.uuid4(),
            urn=urn_repo,
            name=repo_full,
            url=f"https://github.com/test/{repo_full}",
        )
        s.add(repo)
        await s.flush()
        src = Entity(
            id=_uuid.uuid4(),
            entity_type="component",
            urn=urn_src,
            name=src_name,
            scope_repo_id=repo.id,
            confidence=0.9,
            confidence_level="confirmed",
            knowledge_tier=1,
        )
        tgt = Entity(
            id=_uuid.uuid4(),
            entity_type="component",
            urn=urn_tgt,
            name=tgt_name,
            scope_repo_id=repo.id,
            confidence=0.9,
            confidence_level="confirmed",
            knowledge_tier=1,
        )
        s.add(src)
        s.add(tgt)
        await s.flush()
        rel = EntityRelationship(
            id=_uuid.uuid4(),
            source_id=src.id,
            target_id=tgt.id,
            rel_type=rel_type,
            confidence=initial_confidence,
            freshness_state="fresh",
            source_basis=[],
        )
        s.add(rel)
        await s.commit()
        return repo.id, src_name, tgt_name, src.id, rel.id, [urn_src, urn_tgt, urn_repo]


async def _cleanup_audit_state(urns: list[str]) -> None:
    """Delete any test rows we added: deltas, relationships, entities, repos."""
    if not urns:
        return
    f = _factory()
    async with f() as s:
        ent_rows = (await s.execute(select(Entity.id).where(Entity.urn.in_(urns)))).scalars().all()
        if ent_rows:
            ent_ids = [str(i) for i in ent_rows]
            # Deltas reference entities OR relationships; nuke both
            # via the relationships fk
            rel_rows = (
                (
                    await s.execute(
                        select(EntityRelationship.id).where(
                            EntityRelationship.source_id.in_(ent_rows)
                        )
                    )
                )
                .scalars()
                .all()
            )
            all_ids = ent_ids + [str(i) for i in rel_rows]
            if all_ids:
                await s.execute(
                    text("DELETE FROM confidence_deltas WHERE target_id = ANY(:ids)"),
                    {"ids": all_ids},
                )
            await s.execute(
                delete(EntityRelationship).where(EntityRelationship.source_id.in_(ent_rows))
            )
        await s.execute(delete(Entity).where(Entity.urn.in_(urns)))
        # Repos aren't strictly required to clean (cascade from entities),
        # but be tidy: anything with urn starting "urn:bifrost:repo/AcmeApp-…"
        await s.execute(
            text("DELETE FROM repos WHERE urn = ANY(:urns)"),
            {"urns": [u for u in urns if "/component/" not in u]},
        )
        await s.commit()


def _make_bundle(repo_id, repo_name, *, code_report) -> SimpleNamespace:
    """Construct a minimal RepoBundle-shaped object for the audit walker."""
    return SimpleNamespace(
        repo_id=str(repo_id),
        repo_name=repo_name,
        code=code_report,
        jira=None,
        confluence=None,
        github=None,
        slack=None,
    )


@pytest.mark.asyncio
async def test_audit_confirmed_records_delta() -> None:
    repo_id, src_name, tgt_name, _, edge_id, urns = await _seed_two_components_with_edge(
        rel_type="calls",
        initial_confidence=0.5,
    )
    conv_id = await _seed_conversation()
    try:
        code_report = RepoReport(
            repo_id=str(repo_id),
            repo_name="AcmeApp",
            clone_path="",
            relevance="high",
            role_in_answer="",
            edge_verifications=[
                EdgeVerification(
                    edge_desc=f"{src_name} --calls--> {tgt_name}",
                    verified="true",
                    evidence="order_router.py:42",
                ),
            ],
        )
        bundle = _make_bundle(repo_id, "AcmeApp", code_report=code_report)

        stats = await chat_research_audit(
            session_factory=_factory(),
            bundles=[bundle],
            conversation_id=str(conv_id),
            research_run_id=None,
        )

        assert stats.edges_confirmed == 1
        assert stats.edges_contradicted == 0
        assert stats.deltas_recorded >= 1

        # Check a confidence_deltas row was written w/ the chat-research origin
        f = _factory()
        async with f() as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT reason, applied_by, conversation_id, research_run_id
                        FROM confidence_deltas
                        WHERE target_id = :tid
                        ORDER BY applied_at DESC
                        """
                    ),
                    {"tid": str(edge_id)},
                )
            ).all()
        assert any(r[0] == "audit_confirmed" for r in rows)
        assert any(str(r[2]) == str(conv_id) for r in rows if r[2] is not None)
    finally:
        await _cleanup_audit_state(urns)
        await _cleanup_conversation(conv_id)


@pytest.mark.asyncio
async def test_audit_contradicted_drops_confidence() -> None:
    repo_id, src_name, tgt_name, _, edge_id, urns = await _seed_two_components_with_edge(
        rel_type="calls",
        initial_confidence=0.85,
    )
    conv_id = await _seed_conversation()
    try:
        code_report = RepoReport(
            repo_id=str(repo_id),
            repo_name="AcmeApp",
            clone_path="",
            relevance="high",
            role_in_answer="",
            edge_verifications=[
                EdgeVerification(
                    edge_desc=f"{src_name} --calls--> {tgt_name}",
                    verified="false",
                    evidence="not actually called from OrderRouter",
                ),
            ],
        )
        bundle = _make_bundle(repo_id, "AcmeApp", code_report=code_report)

        stats = await chat_research_audit(
            session_factory=_factory(),
            bundles=[bundle],
            conversation_id=str(conv_id),
            research_run_id=None,
        )

        assert stats.edges_contradicted == 1

        # Downgrade is immediate per learning.py governance.
        f = _factory()
        async with f() as s:
            new_conf = (
                await s.execute(
                    select(EntityRelationship.confidence).where(EntityRelationship.id == edge_id)
                )
            ).scalar_one()
        assert float(new_conf) < 0.85
    finally:
        await _cleanup_audit_state(urns)
        await _cleanup_conversation(conv_id)


@pytest.mark.asyncio
async def test_missed_edge_inserts_new_relationship() -> None:
    repo_id, src_name, tgt_name, _, _, urns = await _seed_two_components_with_edge(rel_type="calls")
    conv_id = await _seed_conversation()
    try:
        code_report = RepoReport(
            repo_id=str(repo_id),
            repo_name="AcmeApp",
            clone_path="",
            relevance="high",
            role_in_answer="",
            missed_edges=[
                MissedEdge(
                    source_name=src_name,
                    rel_type="logs_to",  # new rel_type, didn't exist
                    target_name=tgt_name,
                    evidence="order_router.py:99 logger.info(...)",
                ),
            ],
        )
        bundle = _make_bundle(repo_id, "AcmeApp", code_report=code_report)

        stats = await chat_research_audit(
            session_factory=_factory(),
            bundles=[bundle],
            conversation_id=str(conv_id),
            research_run_id=None,
        )

        assert stats.new_edges_inserted == 1

        f = _factory()
        async with f() as s:
            new_rel = (
                await s.execute(
                    select(EntityRelationship)
                    .where(EntityRelationship.rel_type == "logs_to")
                    .order_by(EntityRelationship.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        assert new_rel is not None
        assert new_rel.confidence >= 0.9
        assert any(b.get("source_type") == "code_verified" for b in (new_rel.source_basis or []))
        assert any(b.get("method") == "chat_research_audit" for b in (new_rel.source_basis or []))
    finally:
        await _cleanup_audit_state(urns)
        await _cleanup_conversation(conv_id)


@pytest.mark.asyncio
async def test_audit_idempotent() -> None:
    """Running the same audit twice doesn't double-count new edges or
    create the same relationship row twice. Confidence deltas may stack
    (each turn is an attestation), but the unique constraint on
    (source_id, target_id, rel_type) prevents duplicate edges."""
    repo_id, src_name, tgt_name, _, _, urns = await _seed_two_components_with_edge()
    conv_id = await _seed_conversation()
    try:
        code_report = RepoReport(
            repo_id=str(repo_id),
            repo_name="AcmeApp",
            clone_path="",
            relevance="high",
            role_in_answer="",
            missed_edges=[
                MissedEdge(
                    source_name=src_name,
                    rel_type="logs_to",
                    target_name=tgt_name,
                    evidence="evidence-1",
                ),
            ],
        )
        bundle = _make_bundle(repo_id, "AcmeApp", code_report=code_report)

        s1 = await chat_research_audit(
            session_factory=_factory(),
            bundles=[bundle],
            conversation_id=str(conv_id),
            research_run_id=None,
        )
        s2 = await chat_research_audit(
            session_factory=_factory(),
            bundles=[bundle],
            conversation_id=str(conv_id),
            research_run_id=None,
        )

        assert s1.new_edges_inserted == 1
        # Second pass: edge already exists; treated as soft-confirm,
        # not a new insert.
        assert s2.new_edges_inserted == 0

        # Only one EntityRelationship row for that triple
        f = _factory()
        async with f() as s:
            n = (
                await s.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM entity_relationships er
                        JOIN entities e ON e.id = er.source_id
                        WHERE er.rel_type = 'logs_to'
                          AND e.urn = ANY(:urns)
                        """
                    ),
                    {"urns": urns},
                )
            ).scalar_one()
        assert n == 1
    finally:
        await _cleanup_audit_state(urns)
        await _cleanup_conversation(conv_id)


@pytest.mark.asyncio
async def test_unresolvable_names_skip_quietly() -> None:
    """Names the audit can't map to entity rows are counted as skipped
    rather than crashing."""
    repo_id, _, _, _, _, urns = await _seed_two_components_with_edge()
    conv_id = await _seed_conversation()
    try:
        code_report = RepoReport(
            repo_id=str(repo_id),
            repo_name="AcmeApp",
            clone_path="",
            relevance="high",
            role_in_answer="",
            edge_verifications=[
                EdgeVerification(
                    edge_desc="ImaginaryComponent --uses--> AnotherFakeOne",
                    verified="true",
                    evidence="this won't resolve",
                ),
            ],
            missed_edges=[
                MissedEdge(
                    source_name="GhostA-totally-not-here",
                    rel_type="ghosts",
                    target_name="GhostB-totally-not-here",
                    evidence="",
                ),
            ],
        )
        bundle = _make_bundle(repo_id, "AcmeApp", code_report=code_report)

        stats = await chat_research_audit(
            session_factory=_factory(),
            bundles=[bundle],
            conversation_id=str(conv_id),
            research_run_id=None,
        )

        assert stats.edges_skipped >= 2
        assert stats.edges_confirmed == 0
        assert stats.new_edges_inserted == 0
    finally:
        await _cleanup_audit_state(urns)
        await _cleanup_conversation(conv_id)
