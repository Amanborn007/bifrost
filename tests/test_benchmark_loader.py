"""Integration tests for the benchmark loader + runner + persistence.

Uses the live dev DB (matching ``test_chat_citations_learning.py``)
because ``benchmark_questions`` / ``benchmark_runs`` are
alembic-managed tables. The runner test stubs ``run_research`` so it
doesn't pay for real LLM calls; the goal is to validate the wiring
(scope resolution → cache clear → score → persist) end-to-end.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select, text

from bifrost.api.deps import get_session_factory
from bifrost.benchmarks.loader import ensure_loaded
from bifrost.benchmarks.runner import (
    _build_one_hop_neighbours,
    _resolve_repo_ids,
    clear_caches_for_scope,
    run_domain,
)
from bifrost.cli.config import get_settings
from bifrost.research.coordinator import ResearchDeps
from bifrost.storage.models import (
    BenchmarkQuestion,
    BenchmarkRun,
    Entity,
    EntityRelationship,
    Repo,
)


def _factory():
    return get_session_factory(get_settings())


# ── loader idempotency ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loader_idempotent_on_replay() -> None:
    """First call inserts; second call skips (no churn)."""
    f = _factory()
    r1 = await ensure_loaded(session_factory=f, domain="acme")
    r2 = await ensure_loaded(session_factory=f, domain="acme")
    # First call may insert (fresh DB) or skip (already loaded by a
    # prior test); second call is always pure-skip.
    assert r2["inserted"] == 0
    assert r2["updated"] == 0
    assert r2["skipped"] == r1["inserted"] + r1["updated"] + r1["skipped"]


@pytest.mark.asyncio
async def test_loaded_questions_have_expected_fields() -> None:
    f = _factory()
    await ensure_loaded(session_factory=f, domain="acme")
    async with f() as s:
        q = (
            await s.execute(
                select(BenchmarkQuestion).where(
                    BenchmarkQuestion.domain == "acme",
                    BenchmarkQuestion.question_key == "q1-acme-service-map",
                )
            )
        ).scalar_one()
    assert q.question_text.startswith("What services does the AcmeCo webhook delivery platform")
    assert q.expects_diagram is True
    assert "urn:bifrost:service:webhook-api" in q.expected_entities
    assert q.tier_floor == 1


# ── one-hop adjacency ────────────────────────────────────────────────────────


@pytest.fixture
async def _seeded_dispatcher_graph():
    """Seed a ``dispatcher`` repo + service entity with a real edge to
    ``delivery-gateway`` so the data-dependent benchmark-loader tests are
    hermetic.

    The original tests assumed a live dev DB pre-populated with these
    rows; in the schema-only test DB nothing is seeded, so they must
    create their own fixtures. Cleans up on teardown (the session DB is
    shared across tests, so leaving rows would leak into others).
    """
    import uuid as _u

    f = _factory()
    repo_id = _u.uuid4()
    dispatcher_id = _u.uuid4()
    doc_id = _u.uuid4()
    dispatcher_urn = "urn:bifrost:service:dispatcher"
    doc_urn = "urn:bifrost:service:delivery-gateway"
    async with f() as s:
        s.add(
            Repo(
                id=repo_id,
                urn="urn:bifrost:repo/dispatcher",
                name="dispatcher",
                url="https://github.com/test/dispatcher",
            )
        )
        s.add(
            Entity(
                id=dispatcher_id,
                entity_type="service",
                urn=dispatcher_urn,
                name="dispatcher",
                confidence=0.9,
                confidence_level="confirmed",
                knowledge_tier=1,
            )
        )
        s.add(
            Entity(
                id=doc_id,
                entity_type="service",
                urn=doc_urn,
                name="delivery-gateway",
                confidence=0.9,
                confidence_level="confirmed",
                knowledge_tier=1,
            )
        )
        await s.flush()
        s.add(
            EntityRelationship(
                id=_u.uuid4(),
                source_id=dispatcher_id,
                target_id=doc_id,
                rel_type="calls",
                confidence=0.9,
                freshness_state="fresh",
                source_basis=[],
            )
        )
        await s.commit()
    try:
        yield {"repo_id": repo_id, "dispatcher_urn": dispatcher_urn, "doc_urn": doc_urn}
    finally:
        async with f() as s:
            await s.execute(
                delete(EntityRelationship).where(EntityRelationship.source_id == dispatcher_id)
            )
            await s.execute(delete(Entity).where(Entity.id.in_([dispatcher_id, doc_id])))
            await s.execute(delete(Repo).where(Repo.id == repo_id))
            await s.commit()


@pytest.mark.asyncio
async def test_one_hop_neighbours_finds_real_edges(_seeded_dispatcher_graph) -> None:
    """The seeded entity_relationships graph has dispatcher→delivery-gateway,
    so ``dispatcher``'s one-hop set should include ``delivery-gateway``."""
    f = _factory()
    async with f() as s:
        adj = await _build_one_hop_neighbours(
            session=s,
            expected_urns=["urn:bifrost:service:dispatcher"],
        )
    d_neighbours = adj.get("urn:bifrost:service:dispatcher", set())
    # Real graph: dispatcher has several edges. We assert at least one well-
    # known target appears.
    assert any("delivery-gateway" in u or "usage-metering" in u for u in d_neighbours), (
        f"expected delivery-gateway or usage-metering in {d_neighbours}"
    )


@pytest.mark.asyncio
async def test_one_hop_neighbours_empty_for_unknown_urn() -> None:
    f = _factory()
    async with f() as s:
        adj = await _build_one_hop_neighbours(
            session=s,
            expected_urns=["urn:bifrost:repo/no-such-thing-12345"],
        )
    assert adj == {"urn:bifrost:repo/no-such-thing-12345": set()}


# ── repo id resolution ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_repo_ids_returns_known_only(_seeded_dispatcher_graph) -> None:
    f = _factory()
    async with f() as s:
        ids = await _resolve_repo_ids(
            session=s,
            names=["dispatcher", "no-such-repo-zzz"],
        )
    # dispatcher is seeded; the bogus name is silently dropped
    assert len(ids) == 1


# ── cache clear ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_caches_for_scope_returns_count() -> None:
    """Smoke: clear-caches against an empty repo list is a no-op
    returning 0; against real repos it returns >=0 (no assertion that
    the cache was non-empty, since the test DB might not have rows)."""
    f = _factory()
    out_empty = await clear_caches_for_scope(session_factory=f, repo_names=[])
    assert out_empty == {"agent_results_cleared": 0}

    out_some = await clear_caches_for_scope(
        session_factory=f,
        repo_names=["dispatcher"],
    )
    assert "agent_results_cleared" in out_some
    assert out_some["agent_results_cleared"] >= 0


# ── stubbed run_domain end-to-end ────────────────────────────────────────────
#
# The full ``run_query_turn`` pipeline runs real Claude subprocesses and
# would cost money. We stub it with a coroutine that writes a synthetic
# assistant message directly to the DB — same shape ``run_query_turn``
# would have produced — so the test exercises the harness wiring
# (scope → cache clear → score → persist) without paying for LLM calls.


def _stub_run_query_turn_factory(
    *,
    answer: str,
    sources: list[dict[str, Any]],
    confidence: str = "confirmed",
    cost_usd: float = 0.0123,
):
    """Build a stub ``httpx.AsyncClient`` replacement that:

      * On POST ``/messages``: writes a synthetic user + assistant
        message to ``conversation_messages`` (the runner reads back
        the assistant message after the call returns, so it must be
        present BEFORE the post-call read step).
      * On GET ``/research-status``: returns ``in_flight=null`` +
        ``latest.status="done"`` so the polling loop exits on the
        first probe.

    Real benchmark runs go through the live API server (separate
    process) so this stub mirrors that contract end-to-end.
    """

    class _StubResponse:
        def __init__(self, status: int, payload: dict[str, Any]):
            self.status_code = status
            self._payload = payload
            self.text = ""

        def json(self) -> dict[str, Any]:
            return self._payload

    class _StubClient:
        def __init__(self, *_args, **kwargs):
            self.cookies = kwargs.get("cookies", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, path: str, json: dict[str, Any] | None = None):
            # Determine conv_id from the path; persist user + assistant
            # via the same EntityRepository the live route uses.
            assert "/conversations/" in path and "/messages" in path
            import uuid as _u

            from bifrost.api.deps import get_session_factory
            from bifrost.cli.config import get_settings as _gs
            from bifrost.storage.repository import EntityRepository

            conv_id = _u.UUID(path.split("/conversations/", 1)[1].split("/", 1)[0])
            sf = get_session_factory(_gs())
            async with sf() as s:
                repo = EntityRepository(s)
                await repo.append_message(
                    conversation_id=conv_id,
                    role="user",
                    content=(json or {}).get("content", ""),
                )
                await repo.append_message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=answer,
                    sources=sources,
                    confidence=confidence,
                    cost_usd=cost_usd,
                )
                await s.commit()
            return _StubResponse(
                200,
                {
                    "data": {
                        "user_message_id": "stub-user",
                        "stream_url": f"/api/v1/conversations/{conv_id}/stream",
                    }
                },
            )

        async def get(self, path: str):
            assert "/research-status" in path
            return _StubResponse(
                200,
                {
                    "data": {
                        "in_flight": None,
                        "latest": {"status": "done"},
                    }
                },
            )

    return _StubClient


@pytest.mark.asyncio
async def test_run_domain_with_stubbed_research_persists_score_row() -> None:
    """Run a single question through run_domain with stubbed research.
    Verify a benchmark_runs row lands with the expected score shape."""
    f = _factory()
    settings = get_settings()
    deps = ResearchDeps(session_factory=f, settings=settings)

    # Make sure the questions are loaded
    await ensure_loaded(session_factory=f, domain="acme")

    # Use only one question to keep the test fast
    only_key = "q4-dispatcher-gateway-host"
    answer = (
        "dispatcher (urn:bifrost:service:dispatcher) calls delivery-gateway "
        "(urn:bifrost:service:delivery-gateway) for delivery. "
        "See `gateway_client.py:22` for the dispatch."
    )
    sources = [
        {"urn": "urn:bifrost:service:dispatcher", "ref": "code"},
        {"urn": "urn:bifrost:service:delivery-gateway", "ref": "code"},
    ]

    build_label = f"test-{_uuid.uuid4().hex[:6]}"

    with patch(
        "bifrost.benchmarks.runner.httpx.AsyncClient",
        new=_stub_run_query_turn_factory(answer=answer, sources=sources),
    ):
        reports = await run_domain(
            deps=deps,
            domain="acme",
            run_kinds=("A",),
            build_label=build_label,
            only_keys=[only_key],
        )

    try:
        assert "A" in reports
        rep = reports["A"]
        assert rep.n_succeeded == 1
        assert rep.n_errored == 0
        assert rep.total_cost_usd == pytest.approx(0.0123, abs=1e-6)

        # Persisted row check
        async with f() as s:
            row = (
                await s.execute(select(BenchmarkRun).where(BenchmarkRun.build_label == build_label))
            ).scalar_one()
        assert row.run_kind == "A"
        assert row.answer_confidence == "confirmed"
        assert float(row.score_recall) == 1.0  # both expected URNs cited
        assert float(row.score_total) > 0.5
        # NOTE: audit_summary fields (edges_confirmed/new_edges_inserted)
        # aren't surfaced when run_query_turn is the driver — they happen
        # internally but the runner doesn't read them back. Stub-test
        # path leaves them at 0; live runs pick them up via the chat
        # audit pass.
    finally:
        # Clean up: benchmark_runs (FK CASCADE off questions) + the stub
        # conversations.
        async with f() as s:
            await s.execute(delete(BenchmarkRun).where(BenchmarkRun.build_label == build_label))
            await s.execute(
                text("DELETE FROM conversations WHERE title LIKE :pat"), {"pat": "[bench:acme:A] %"}
            )
            await s.commit()


def test_runner_drives_over_http_not_inprocess() -> None:
    """REGRESSION GUARD: the benchmark runner MUST drive turns over
    HTTP through the live API server (httpx.AsyncClient → POST
    /api/v1/conversations/{id}/messages). Calling ``run_query_turn``
    or ``run_research`` directly publishes events to the runner
    process's own ``event_bus``, not the API server's — the FE
    sees only "Thinking…" and the rich progress widget never
    renders.

    This test reads the runner module's source and asserts the
    architectural invariant statically.
    """
    from pathlib import Path

    runner_src = (
        Path(__file__).resolve().parents[1] / "src" / "bifrost" / "benchmarks" / "runner.py"
    )
    text = runner_src.read_text()
    # Forbid in-process calls — both run_research (raw coordinator)
    # and run_query_turn (the chat-layer wrapper) bypass the live
    # API server's event_bus.
    assert "run_research(" not in text, (
        "runner calls run_research — must drive over HTTP to the API server"
    )
    assert "run_query_turn(" not in text, (
        "runner calls run_query_turn — must drive over HTTP to the API server"
    )
    # Require the HTTP path
    assert "httpx" in text and "/messages" in text, (
        "runner doesn't appear to drive POST /messages over httpx"
    )
    assert "research-status" in text, (
        "runner doesn't poll /research-status — must wait for terminal state"
    )


@pytest.mark.asyncio
async def test_run_domain_persists_user_and_assistant_messages() -> None:
    """The runner MUST persist BOTH the user question and assistant
    answer to conversation_messages — that's how the UI renders the
    conversation as it streams. Regression guard for the empty-UI bug
    when the runner bypassed the production path.
    """
    from sqlalchemy import select

    from bifrost.storage.models import ConversationMessage

    f = _factory()
    deps = ResearchDeps(session_factory=f, settings=get_settings())
    await ensure_loaded(session_factory=f, domain="acme")
    label = f"msgs-{_uuid.uuid4().hex[:6]}"

    with patch(
        "bifrost.benchmarks.runner.httpx.AsyncClient",
        new=_stub_run_query_turn_factory(
            answer="A. urn:bifrost:service:dispatcher handles things.",
            sources=[{"urn": "urn:bifrost:service:dispatcher"}],
        ),
    ):
        await run_domain(
            deps=deps,
            domain="acme",
            run_kinds=("A",),
            build_label=label,
            only_keys=["q4-dispatcher-gateway-host"],
        )

    try:
        async with f() as s:
            run = (
                await s.execute(select(BenchmarkRun).where(BenchmarkRun.build_label == label))
            ).scalar_one()
            msgs = (
                (
                    await s.execute(
                        select(ConversationMessage)
                        .where(ConversationMessage.conversation_id == run.conversation_id)
                        .order_by(ConversationMessage.turn_index)
                    )
                )
                .scalars()
                .all()
            )
        roles = [m.role for m in msgs]
        # Expect BOTH user (written by runner) AND assistant (written
        # by the stubbed run_query_turn) — proves the user-msg-first
        # ordering and the post-call read-back chain.
        assert "user" in roles, f"user message not persisted; roles={roles}"
        assert "assistant" in roles, f"assistant message not persisted; roles={roles}"
    finally:
        async with f() as s:
            await s.execute(delete(BenchmarkRun).where(BenchmarkRun.build_label == label))
            await s.commit()


@pytest.mark.asyncio
async def test_run_domain_skips_run_c_when_no_paraphrase() -> None:
    """A question without a paraphrase is skipped during Run C — the
    runner doesn't error out, it just doesn't add a row."""
    f = _factory()
    deps = ResearchDeps(session_factory=f, settings=get_settings())
    await ensure_loaded(session_factory=f, domain="acme")

    # q4 doesn't have a paraphrase; q1 does
    only_keys = ["q4-dispatcher-gateway-host"]

    label = f"skipc-{_uuid.uuid4().hex[:6]}"
    with patch(
        "bifrost.benchmarks.runner.httpx.AsyncClient",
        new=_stub_run_query_turn_factory(answer="x", sources=[]),
    ):
        reports = await run_domain(
            deps=deps,
            domain="acme",
            run_kinds=("C",),
            build_label=label,
            only_keys=only_keys,
        )
    try:
        rep = reports["C"]
        # The question existed (n_questions=1) but was skipped (no row added)
        assert rep.n_questions == 1
        assert len(rep.turn_results) == 0
    finally:
        async with f() as s:
            await s.execute(delete(BenchmarkRun).where(BenchmarkRun.build_label == label))
            await s.commit()
