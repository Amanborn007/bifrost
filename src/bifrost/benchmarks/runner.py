"""Benchmark runner — execute Run A/B/C across the loaded question set.

Three runs per question set:

  * **Run A — cold cache.** Empty ``agent_results`` for the questions'
    scopes. Pure first-time cost + zero "Known facts" injection.
  * **Run B — warm cache, fresh conv.** Same questions in a brand new
    conversation. Measures cross-conversation reuse: the
    ``agent_results`` semantic cache (Tier 1) and the
    ``entity_relationships`` graph (Tier 2) are both warm.
  * **Run C — warm cache, paraphrased.** The YAML's ``paraphrase``
    field. Probes semantic robustness: do the cache + facts still hit
    when the wording shifts?

Each invocation is a real chat turn — the runner posts to the same
``run_research`` async generator the production chat path uses, drains
the events, scores the answer, and persists a ``BenchmarkRun`` row.

Idempotency: the runner does NOT clear caches between Run A→B→C. Run A
is cold by virtue of being run first AFTER an explicit cache clear (the
caller invokes ``clear_caches_for_scope`` before Run A). Subsequent
runs read the warm state. This way the harness tells the truth about
"what does it actually cost on a cold start" vs "what's the steady
state."

The runner is invocable via the CLI (``python -m bifrost.benchmarks
run --domain acme``) — see the ``main()`` entry point at the bottom.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.api._cookie_signing import sign_session_cookie
from bifrost.api.auth.csrf import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from bifrost.api.deps import get_session_factory
from bifrost.benchmarks.loader import ensure_loaded, is_sequenced
from bifrost.benchmarks.scorer import ScoreInput, score_answer
from bifrost.cli.config import BifrostSettings, get_settings, validate_startup
from bifrost.core.http import http_timeout
from bifrost.research.coordinator import ResearchDeps
from bifrost.storage.models import (
    BenchmarkQuestion,
    BenchmarkRun,
    Conversation,
    ConversationMessage,
    Repo,
)
from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)


# ── one-hop adjacency builder ─────────────────────────────────────────────────
#
# The scorer's precision component credits one-hop neighbours of expected
# entities as on-topic. We build this map per-question once at the start
# of the run, querying the live ``entity_relationships`` graph.


async def _build_one_hop_neighbours(
    *,
    session: AsyncSession,
    expected_urns: list[str],
) -> dict[str, set[str]]:
    """For each expected URN, the set of URNs it has any edge to/from.

    Direction-agnostic — both incoming and outgoing edges count as
    adjacency for the precision relaxation.
    """
    if not expected_urns:
        return {}

    out: dict[str, set[str]] = {u: set() for u in expected_urns}

    rows = (
        await session.execute(
            sa_text(
                """
                SELECT DISTINCT src.urn AS subj, tgt.urn AS obj
                FROM entity_relationships r
                JOIN entities src ON src.id = r.source_id
                JOIN entities tgt ON tgt.id = r.target_id
                WHERE src.urn = ANY(:urns) OR tgt.urn = ANY(:urns)
                """
            ),
            {"urns": expected_urns},
        )
    ).all()
    for r in rows:
        if r.subj in out:
            out[r.subj].add(r.obj)
        if r.obj in out:
            out[r.obj].add(r.subj)
    return out


# ── scope_repo_ids resolution ────────────────────────────────────────────────


async def _resolve_repo_ids(
    *,
    session: AsyncSession,
    names: list[str],
) -> list[str]:
    if not names:
        return []
    rows = (await session.execute(select(Repo.id, Repo.name).where(Repo.name.in_(names)))).all()
    by_name = {r[1]: str(r[0]) for r in rows}
    return [by_name[n] for n in names if n in by_name]


# ── conversation seeding ─────────────────────────────────────────────────────


async def _resolve_owner_session_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Resolve the session_id under which benchmark conversations
    should be created.

    Order of resolution:
      1. ``BIFROST_BENCHMARK_OWNER_SESSION_ID`` env var (explicit).
      2. Most-active non-benchmark session_id in the conversations
         table (dev convenience — one user typically dominates).
      3. Fallback to a stable ``"benchmark"`` placeholder if the table
         is empty (the conversation will be orphaned but the runner
         won't crash).

    Why: conversations are session-cookie-scoped (the API checks
    ``get_conversation_for_session`` on every read endpoint). If the
    runner uses a fresh synthetic session_id per benchmark, the
    browser cookie never matches and the user sees an empty page when
    navigating to ``/query?conv=<id>`` — no streaming, no progress,
    nothing renders. Adopting the dominant user session makes
    benchmark conversations viewable AS the running user.
    """
    import os as _os

    explicit = _os.environ.get("BIFROST_BENCHMARK_OWNER_SESSION_ID")
    if explicit and explicit.strip():
        return explicit.strip()

    async with session_factory() as session:
        row = (
            await session.execute(
                sa_text(
                    """
                    SELECT session_id
                    FROM conversations
                    WHERE deleted_at IS NULL
                      AND title NOT LIKE '[bench:%'
                      AND session_id IS NOT NULL
                    GROUP BY session_id
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                    """
                )
            )
        ).scalar_one_or_none()
    return row or "benchmark"


async def _create_benchmark_conversation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    domain: str,
    run_kind: str,
    question_key: str,
    scope_repo_ids: list[str],
    owner_session_id: str,
) -> _uuid.UUID:
    """One conversation per (question, run_kind). Title encodes the
    benchmark coordinates so the chat history admin UI can find them.

    ``owner_session_id`` is the dominant user session resolved by
    ``_resolve_owner_session_id`` so the conversation is visible to
    the running user's browser session.
    """
    cid = _uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Conversation(
                id=cid,
                session_id=owner_session_id,
                title=f"[bench:{domain}:{run_kind}] {question_key}",
                scope_repo_ids=scope_repo_ids,
            )
        )
        await session.commit()
    return cid


# ── cache clearing for Run A (cold) ──────────────────────────────────────────


async def clear_caches_for_scope(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repo_names: list[str],
) -> dict[str, int]:
    """Wipe the ``agent_results`` semantic cache for the given repos.

    NOT touched: ``entity_relationships`` (the knowledge graph is
    permanent — that's the whole point of Tier 1's permanence fix).
    Only the per-turn agent cache is cleared so Run A is a true
    cold-start cost reading.

    Returns ``{"agent_results_cleared": N}``.
    """
    if not repo_names:
        return {"agent_results_cleared": 0}

    async with session_factory() as session:
        repo_ids = (
            (await session.execute(select(Repo.id).where(Repo.name.in_(repo_names))))
            .scalars()
            .all()
        )
        if not repo_ids:
            return {"agent_results_cleared": 0}
        # agent_results' ``target_id`` for repo agents = repo_id; for
        # external agents (jira/conf/github/slack) ``target_id`` is also
        # the repo_id (the agent is scoped to a single repo). So
        # deleting all rows where target_id matches our repo set sweeps
        # the cache for those repos cleanly.
        result = await session.execute(
            sa_text(
                """
                DELETE FROM agent_results
                WHERE target_id = ANY(:ids)
                """
            ),
            {"ids": [str(i) for i in repo_ids]},
        )
        await session.commit()
        return {"agent_results_cleared": affected_rows(result)}


# ── runner outputs ───────────────────────────────────────────────────────────


@dataclass
class TurnResult:
    """One scored turn — what gets persisted into ``benchmark_runs``."""

    question_id: _uuid.UUID
    question_key: str
    run_kind: str
    answer_text: str
    answer_confidence: str | None
    cost_usd: float
    duration_ms: int
    agents_dispatched: int
    agents_cache_hit: int
    facts_injected: int
    edges_confirmed: int
    edges_contradicted: int
    new_edges_inserted: int
    cited_urns: list[str]
    score_row: dict[str, Any]
    conversation_id: _uuid.UUID
    research_run_id: _uuid.UUID | None
    error: str | None = None


@dataclass
class RunReport:
    """Aggregated harness report — one per (domain, run_kind)."""

    domain: str
    run_kind: str
    build_label: str | None
    n_questions: int
    n_succeeded: int
    n_errored: int
    total_cost_usd: float
    total_duration_ms: int
    turn_results: list[TurnResult] = field(default_factory=list)

    def as_summary(self) -> dict[str, Any]:
        scores = [t.score_row["score_total"] for t in self.turn_results if t.error is None]
        return {
            "domain": self.domain,
            "run_kind": self.run_kind,
            "build_label": self.build_label,
            "n_questions": self.n_questions,
            "n_succeeded": self.n_succeeded,
            "n_errored": self.n_errored,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_duration_ms": self.total_duration_ms,
            "avg_score_total": (round(sum(scores) / len(scores), 3) if scores else 0.0),
            "avg_cost_per_question_usd": (round(self.total_cost_usd / max(1, self.n_succeeded), 4)),
        }


# ── single-question runner ───────────────────────────────────────────────────


def _build_http_client(*, settings: BifrostSettings, cookie_value: str) -> httpx.AsyncClient:
    # The session cookie makes these POSTs look like a browser to the CSRF middleware.
    return httpx.AsyncClient(
        base_url=settings.api_base_url,
        cookies={"bifrost_session": cookie_value},
        headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
        timeout=http_timeout(settings),
    )


async def _run_one_question(
    *,
    deps: ResearchDeps,
    question: BenchmarkQuestion,
    run_kind: str,
    use_paraphrase: bool,
    shared_conversation_id: _uuid.UUID | None = None,
) -> TurnResult:
    """Execute one chat turn against ``run_research`` and score it.

    ``shared_conversation_id`` reuses an existing conversation across
    multiple turns (sequenced domains). When ``None``, a fresh
    conversation is created — the default for non-sequenced domains.
    """
    started = time.monotonic()
    question_text = (
        question.paraphrase if (use_paraphrase and question.paraphrase) else question.question_text
    )
    async with deps.session_factory() as session:
        scope_repo_ids = await _resolve_repo_ids(
            session=session,
            names=question.scope_repo_names,
        )
    owner_session_id_for_cookie = await _resolve_owner_session_id(
        deps.session_factory,
    )
    if shared_conversation_id is not None:
        conv_id = shared_conversation_id
    else:
        conv_id = await _create_benchmark_conversation(
            session_factory=deps.session_factory,
            domain=question.domain,
            run_kind=run_kind,
            question_key=question.question_key,
            scope_repo_ids=scope_repo_ids,
            owner_session_id=owner_session_id_for_cookie,
        )

    # Drive the turn over HTTP through the LIVE API server. This is
    # critical: the runner is a separate Python process from the API
    # server (uvicorn), and ``event_bus`` is in-memory per-process. If
    # the runner called ``run_query_turn`` directly, events would
    # publish to the runner's own event_bus, NOT the one the API
    # server's SSE endpoint reads from — the FE would see nothing
    # except a "Thinking..." placeholder. POSTing to
    # ``/api/v1/conversations/{id}/messages`` makes the API server
    # itself run the turn, so events flow through the SAME event_bus
    # the FE is subscribed to → live progress widget renders
    # correctly for every turn (sequenced or not).
    cookie_value = sign_session_cookie(owner_session_id_for_cookie)
    # Polling deadline: 30 min for sequenced turns (prior_turns
    # context makes synthesis substantially longer than turn 1) and
    # 15 min for non-sequenced. Both well above the bundle's soft
    # 180s external-agent deadline.
    poll_deadline_s = 1800.0 if shared_conversation_id is not None else 900.0

    async def _poll_until_terminal(http: httpx.AsyncClient) -> str | None:
        """Poll /research-status until the most recent run reaches a
        terminal state. Returns ``None`` on success, an error string
        on timeout / non-done terminal status.
        """
        deadline = time.monotonic() + poll_deadline_s
        while time.monotonic() < deadline:
            await asyncio.sleep(5.0)
            status_resp = await http.get(
                f"/api/v1/conversations/{conv_id}/research-status",
            )
            if status_resp.status_code != 200:
                continue
            data = (status_resp.json() or {}).get("data") or {}
            in_flight = data.get("in_flight")
            if in_flight is None:
                latest = data.get("latest") or {}
                latest_status = latest.get("status")
                if latest_status in ("done", "failed", "cancelled"):
                    if latest_status != "done":
                        return f"run terminal: {latest_status}: {latest.get('error_message')}"
                    return None
        return f"polling timed out after {poll_deadline_s / 60:.0f} minutes"

    error: str | None = None
    async with _build_http_client(settings=deps.settings, cookie_value=cookie_value) as http:
        try:
            resp = await http.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={"content": question_text},
            )
            if resp.status_code == 409:
                # query_in_flight — a previous turn's run is still
                # going. Reattach: poll until terminal, then submit
                # this question. Critical for sequenced runs where
                # the bundle deadline can fire late and cascade into
                # the next turn's POST.
                logger.warning(
                    "409 on submit — previous run still in flight. "
                    "Polling until terminal, then resubmitting.",
                )
                wait_err = await _poll_until_terminal(http)
                if wait_err:
                    error = f"reattach wait: {wait_err}"
                    logger.error(error)
                else:
                    # Resubmit now that the prior run is terminal.
                    resp = await http.post(
                        f"/api/v1/conversations/{conv_id}/messages",
                        json={"content": question_text},
                    )
                    if resp.status_code >= 400:
                        error = f"resubmit failed: HTTP {resp.status_code}: {resp.text[:200]}"
                        logger.error(error)
                    else:
                        error = await _poll_until_terminal(http)
                        if error:
                            logger.warning(error)
            elif resp.status_code >= 400:
                error = f"submit failed: HTTP {resp.status_code}: {resp.text[:200]}"
                logger.error(error)
            else:
                error = await _poll_until_terminal(http)
                if error:
                    logger.warning(error)
        except Exception as exc:  # pragma: no cover
            error = f"runner crash: {exc}"
            logger.exception("benchmark turn crashed: %s", question.question_key)

    duration_ms = int((time.monotonic() - started) * 1000)

    # Pull the assistant message that run_query_turn just wrote — its
    # content/confidence/sources/cost are the authoritative scoring
    # inputs.
    answer_text = ""
    confidence: str | None = None
    cited_urns: list[str] = []
    cost = 0.0
    audit_summary = {
        "edges_confirmed": 0,
        "edges_contradicted": 0,
        "new_edges_inserted": 0,
    }
    agents_dispatched = 0
    agents_cache_hit = 0
    facts_injected = 0
    research_run_id: _uuid.UUID | None = None
    async with deps.session_factory() as s_read:
        msg_row = (
            await s_read.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == conv_id,
                    ConversationMessage.role == "assistant",
                )
                .order_by(ConversationMessage.turn_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if msg_row is not None:
            answer_text = msg_row.content or ""
            confidence = msg_row.confidence
            cited_urns = [
                s.get("urn") if isinstance(s, dict) and s.get("urn") else str(s)
                for s in (msg_row.sources or [])
                if (isinstance(s, dict) and s.get("urn"))
                or (isinstance(s, str) and s.startswith("urn:"))
            ]
            cost = float(msg_row.cost_usd or 0.0)
        if answer_text == "" and error is None:
            error = "no assistant message persisted"

    # Build adjacency map for precision scoring
    async with deps.session_factory() as s:
        one_hop = await _build_one_hop_neighbours(
            session=s,
            expected_urns=list(question.expected_entities or []),
        )

    # Compute bundle_has_gap from the actual run state. The done-event
    # payload doesn't carry per-source error details, but the per-source
    # report objects on each bundle do — we just don't have direct
    # access to them from the runner. As a reasonable proxy, scan the
    # synthesized answer for per-source-failure phrasing the
    # synthesizer uses ("Slack: search failed", etc.) — same needles
    # the evidence_consistency scorer uses on the answer body.
    # Setting bundle_has_gap=True here is REDUNDANT with the answer-
    # body scan in the scorer, but the explicit signal helps when the
    # synthesizer scrubs failures from its body (which shouldn't
    # happen post-fix, but we want defense-in-depth).
    from bifrost.benchmarks.scorer import _GAP_NEEDLES

    answer_lower = (answer_text or "").lower()
    bundle_has_gap = any(needle in answer_lower for needle in _GAP_NEEDLES)

    score_input = ScoreInput(
        answer_text=answer_text,
        answer_confidence=confidence,
        cited_urns=cited_urns,
        expected_entities=list(question.expected_entities or []),
        expected_jira_refs=list(question.expected_jira_refs or []),
        expected_pr_refs=list(question.expected_pr_refs or []),
        expected_key_facts=list(question.expected_key_facts or []),
        tier_floor=int(question.tier_floor or 3),
        expects_diagram=bool(question.expects_diagram),
        expected_format=question.expected_format,
        max_words=question.max_words,
        bundle_has_gap=bundle_has_gap,
        one_hop_neighbours=one_hop,
    )
    score = score_answer(score_input)

    # Count "Known facts" bullets actually rendered to agents this turn:
    # we don't have a direct event hook for that yet, so we approximate
    # via audit_summary's edges_confirmed (a confirmed edge implies the
    # agent saw it as a known fact and verified it).
    facts_injected = audit_summary["edges_confirmed"] + audit_summary["edges_contradicted"]

    return TurnResult(
        question_id=question.id,
        question_key=question.question_key,
        run_kind=run_kind,
        answer_text=answer_text,
        answer_confidence=confidence,
        cost_usd=cost,
        duration_ms=duration_ms,
        agents_dispatched=agents_dispatched,
        agents_cache_hit=agents_cache_hit,
        facts_injected=facts_injected,
        edges_confirmed=int(audit_summary.get("edges_confirmed") or 0),
        edges_contradicted=int(audit_summary.get("edges_contradicted") or 0),
        new_edges_inserted=int(audit_summary.get("new_edges_inserted") or 0),
        cited_urns=score.cited_entity_urns,
        score_row=score.as_db_row(),
        conversation_id=conv_id,
        research_run_id=research_run_id,
        error=error,
    )


# ── persist scored turn ──────────────────────────────────────────────────────


async def _persist_run(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    turn: TurnResult,
    build_label: str | None,
) -> _uuid.UUID:
    row_id = _uuid.uuid4()
    async with session_factory() as session:
        session.add(
            BenchmarkRun(
                id=row_id,
                question_id=turn.question_id,
                run_kind=turn.run_kind,
                build_label=build_label,
                conversation_id=turn.conversation_id,
                research_run_id=turn.research_run_id,
                answer_text=turn.answer_text or "",
                answer_confidence=turn.answer_confidence,
                cost_usd=turn.cost_usd,
                duration_ms=turn.duration_ms,
                agents_dispatched=turn.agents_dispatched,
                agents_cache_hit=turn.agents_cache_hit,
                facts_injected=turn.facts_injected,
                edges_confirmed=turn.edges_confirmed,
                edges_contradicted=turn.edges_contradicted,
                new_edges_inserted=turn.new_edges_inserted,
                **turn.score_row,
            )
        )
        await session.commit()
    return row_id


# ── domain-level orchestration ───────────────────────────────────────────────


async def run_domain(
    *,
    deps: ResearchDeps,
    domain: str,
    run_kinds: tuple[str, ...] = ("A", "B", "C"),
    build_label: str | None = None,
    only_keys: list[str] | None = None,
) -> dict[str, RunReport]:
    """Top-level entry: load YAML, run A/B/C, score, persist.

    Returns ``{run_kind: RunReport}`` so the CLI can print a comparison
    table at the end.

    Cache discipline:
      * Run A: ``clear_caches_for_scope(repos)`` runs first, so it sees
        a true cold cache. The graph (``entity_relationships``) is
        intentionally NOT cleared — it's permanent.
      * Run B: no clearing. Hits whatever Run A populated.
      * Run C: no clearing. Same as B but with paraphrased questions
        (the YAML's ``paraphrase`` field). Skipped per-question if the
        question has no paraphrase.
    """
    await ensure_loaded(session_factory=deps.session_factory, domain=domain)

    async with deps.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(BenchmarkQuestion).where(
                        BenchmarkQuestion.domain == domain,
                    )
                )
            )
            .scalars()
            .all()
        )
    # Natural-numeric sort by the leading digit run in question_key.
    # Postgres' default lexicographic ORDER BY puts q10 before q2
    # because '1' < '2' at position 1 of the literal string — meaning
    # the sequenced harness ran q10 → q11 → q1 → q2 → … instead of
    # q1 → q2 → … → q10. For a sequenced fixture (one conversation,
    # follow-up turns building on prior_turns context), this scrambles
    # the narrative the questions were authored to tell. Extract the
    # leading numeric prefix (after the "q") so q1 < q2 < … < q10 < q11.
    import re as _re

    def _natural_key(qk: str) -> tuple[int, str]:
        m = _re.match(r"q(\d+)", qk)
        return (int(m.group(1)) if m else 0, qk)

    rows = sorted(rows, key=lambda q: _natural_key(q.question_key))
    questions = [q for q in rows if (only_keys is None or q.question_key in only_keys)]
    if not questions:
        raise RuntimeError(f"no benchmark questions loaded for {domain!r}")

    # Pre-cache clear for Run A: sweep agent_results for the union of
    # all repos referenced by any question in scope.
    all_repo_names = sorted({n for q in questions for n in (q.scope_repo_names or [])})

    # Sequenced fixtures (``sequenced: true``) reproduce a multi-turn conversation:
    # all questions for a run_kind share ONE conversation so prior_turns accumulate.
    sequenced = is_sequenced(domain)

    out: dict[str, RunReport] = {}
    for kind in run_kinds:
        if kind == "A":
            cleared = await clear_caches_for_scope(
                session_factory=deps.session_factory,
                repo_names=all_repo_names,
            )
            logger.info("Run A pre-clear: %s", cleared)

        report = RunReport(
            domain=domain,
            run_kind=kind,
            build_label=build_label,
            n_questions=len(questions),
            n_succeeded=0,
            n_errored=0,
            total_cost_usd=0.0,
            total_duration_ms=0,
        )

        # For sequenced domains, mint one shared conversation per run_kind.
        shared_conv_id: _uuid.UUID | None = None
        if sequenced:
            async with deps.session_factory() as session:
                seq_scope_ids = await _resolve_repo_ids(
                    session=session,
                    names=all_repo_names,
                )
            seq_owner_sid = await _resolve_owner_session_id(deps.session_factory)
            shared_conv_id = await _create_benchmark_conversation(
                session_factory=deps.session_factory,
                domain=domain,
                run_kind=kind,
                question_key="_sequenced_",
                scope_repo_ids=seq_scope_ids,
                owner_session_id=seq_owner_sid,
            )
            logger.info(
                "Run %s sequenced conversation: %s",
                kind,
                shared_conv_id,
            )

        for q in questions:
            if kind == "C" and not q.paraphrase:
                # Skip Run C for questions without a paraphrase variant
                logger.info(
                    "Run C: skipping %s (no paraphrase)",
                    q.question_key,
                )
                continue
            logger.info(
                "[%s] %s %s",
                kind,
                q.question_key,
                q.paraphrase if (kind == "C" and q.paraphrase) else q.question_text[:80],
            )
            t = await _run_one_question(
                deps=deps,
                question=q,
                run_kind=kind,
                use_paraphrase=(kind == "C"),
                shared_conversation_id=shared_conv_id,
            )
            await _persist_run(
                session_factory=deps.session_factory,
                turn=t,
                build_label=build_label,
            )
            report.turn_results.append(t)
            if t.error is None:
                report.n_succeeded += 1
            else:
                report.n_errored += 1
            report.total_cost_usd += t.cost_usd
            report.total_duration_ms += t.duration_ms
        out[kind] = report
    return out


# ── CLI entry point ──────────────────────────────────────────────────────────


def _print_table(reports: dict[str, RunReport]) -> None:
    print()
    print(f"{'Run':<6}{'OK':>4}{'Err':>5}{'Cost($)':>10}{'AvgScore':>11}{'Cost/Q($)':>11}")
    print("-" * 47)
    for kind in ("A", "B", "C"):
        r = reports.get(kind)
        if not r:
            continue
        s = r.as_summary()
        print(
            f"{kind:<6}{s['n_succeeded']:>4}{s['n_errored']:>5}"
            f"{s['total_cost_usd']:>10.2f}{s['avg_score_total']:>11.3f}"
            f"{s['avg_cost_per_question_usd']:>11.4f}"
        )
    print()


async def _async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    deps = ResearchDeps(
        session_factory=get_session_factory(settings),
        settings=settings,
    )
    kinds = tuple(args.run_kinds.split(","))
    only = args.only.split(",") if args.only else None
    reports = await run_domain(
        deps=deps,
        domain=args.domain,
        run_kinds=kinds,
        build_label=args.label,
        only_keys=only,
    )
    _print_table(reports)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bifrost-bench",
        description="Bifrost benchmark harness — Tier 3 proving ground.",
    )
    parser.add_argument(
        "--domain",
        default="acme",
        help="benchmark domain (filename in docs/benchmarks/, no extension)",
    )
    parser.add_argument(
        "--run-kinds",
        dest="run_kinds",
        default="A,B,C",
        help="comma-separated run kinds to execute (any subset of A,B,C)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="build_label tag stored on each row for cross-build comparison",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated question_key list to restrict the run to",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="DEBUG|INFO|WARNING",
    )
    args = parser.parse_args(argv)
    validate_startup()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    return asyncio.run(_async_main(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
