"""Read path: surface entity_relationships as "Known facts" to per-source agents.

The chat-research learning loop's first half. The existing
``entity_relationships`` table is the knowledge graph — not a
parallel ``claims`` store. This module
gives per-source research agents a short prompt block of edges already
known about their target so they verify-or-extend instead of
rediscovering. That's the "remember instead of re-search" win.

Two functions:

  * ``graph_facts_for_subject(...)`` — fetch the top-K active edges
    rooted at a subject URN (a repo or component), filtered by
    predicate, ranked by ``confidence DESC, updated_at DESC``.
  * ``render_facts_block(...)`` — format them into a markdown bullet
    list ready to splice into the agent's ``prior_turns`` prompt.

The block is bounded — defaults are tuned so the per-agent prompt
overhead stays small even when the graph is dense. If the graph
is empty for a subject, both helpers no-op (return ``[]`` / ``""``).

Predicate hints per agent kind live in ``KIND_PREDICATES`` so the Jira
agent doesn't get told about ``calls_service`` edges (irrelevant) and
the Slack agent doesn't get bombarded with ``contains`` edges (too
many, low signal).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


# ── predicate filters per per-source-agent kind ──────────────────────────────
#
# Each agent kind only needs to see facts in predicates relevant to its
# search surface. The repo (code) agent gets the union since its job
# spans every relation type. ``None`` => no filter (all predicates).
KIND_PREDICATES: dict[str, list[str] | None] = {
    "code": None,  # repo agent: union view
    "jira": ["has_jira_ticket", "has_status", "has_title"],
    "confluence": ["has_confluence_doc", "has_title"],
    "github": ["has_pr", "has_state"],
    "slack": ["has_slack_thread"],
    "gdocs": ["has_gdoc", "has_title"],
}


@dataclass
class FactEdge:
    """One row of the knowledge graph rendered as a fact bullet."""

    subject_urn: str
    rel_type: str
    target_name: str
    target_urn: str
    confidence: float
    confidence_level: str
    last_observed: str  # ISO-8601 date or "" if unknown


_FACTS_QUERY = text(
    """
    SELECT
      src.urn        AS subject_urn,
      r.rel_type     AS rel_type,
      tgt.name       AS target_name,
      tgt.urn        AS target_urn,
      r.confidence   AS confidence,
      tgt.confidence_level AS target_confidence_level,
      r.updated_at   AS last_observed
    FROM entity_relationships r
    JOIN entities src ON src.id = r.source_id
    JOIN entities tgt ON tgt.id = r.target_id
    WHERE src.urn = :subject_urn
      AND r.confidence >= :min_confidence
      AND (:predicates_count = 0 OR r.rel_type = ANY(:predicates))
    ORDER BY r.confidence DESC, r.updated_at DESC
    LIMIT :limit
    """
)


_REPO_FACTS_QUERY = text(
    """
    SELECT
      src.name       AS subject_name,
      src.urn        AS subject_urn,
      r.rel_type     AS rel_type,
      tgt.name       AS target_name,
      tgt.urn        AS target_urn,
      r.confidence   AS confidence,
      tgt.confidence_level AS target_confidence_level,
      r.updated_at   AS last_observed
    FROM entity_relationships r
    JOIN entities src ON src.id = r.source_id
    JOIN entities tgt ON tgt.id = r.target_id
    WHERE src.scope_repo_id = :repo_id
      AND r.confidence >= :min_confidence
      AND (:predicates_count = 0 OR r.rel_type = ANY(:predicates))
    ORDER BY r.confidence DESC, r.updated_at DESC
    LIMIT :limit
    """
)


async def graph_facts_for_subject(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    subject_urn: str,
    predicates: Iterable[str] | None = None,
    min_confidence: float = 0.5,
    limit: int = 20,
) -> list[FactEdge]:
    """Top-K active edges rooted at ``subject_urn``.

    ``predicates``: when non-empty, restricts to those rel_types. ``None``
    or empty => all predicates. Use ``KIND_PREDICATES[kind]`` to get the
    canonical filter for a per-source agent.

    ``min_confidence``: drops edges below the floor. Default 0.5 matches
    Entity's default — we don't want speculation in the agent prompt.

    ``limit``: caps the prompt overhead. 20 is the default — at 30
    chars/bullet that's ~600 chars. Edges beyond the limit are still in
    the graph; the agent just isn't told about them.

    Returns an empty list if the subject URN doesn't exist or has no
    matching edges. Callers should treat empty as "no prior knowledge,
    proceed with normal research" — not as an error.
    """
    pred_list = list(predicates) if predicates else []

    async with session_factory() as session:
        rows = (
            await session.execute(
                _FACTS_QUERY,
                {
                    "subject_urn": subject_urn,
                    "min_confidence": min_confidence,
                    "predicates": pred_list,
                    "predicates_count": len(pred_list),
                    "limit": limit,
                },
            )
        ).all()

    return [
        FactEdge(
            subject_urn=r.subject_urn,
            rel_type=r.rel_type,
            target_name=r.target_name,
            target_urn=r.target_urn,
            confidence=float(r.confidence) if r.confidence is not None else 0.0,
            confidence_level=str(r.target_confidence_level or "unknown"),
            last_observed=(r.last_observed.isoformat()[:10] if r.last_observed else ""),
        )
        for r in rows
    ]


def render_facts_block(
    edges: list[FactEdge],
    *,
    subject_label: str | None = None,
) -> str:
    """Format edges into a markdown block for the agent prompt.

    Returns ``""`` when ``edges`` is empty so callers can splice
    unconditionally without padding the prompt with a "no facts" header.

    The block leads with a directive ("DO NOT re-research these…") so
    the agent skips searches whose only output would be re-finding the
    listed edges. Confidence is shown so the agent can decide if a
    low-confidence edge is worth verifying — high-confidence ones are
    treated as ground truth.
    """
    if not edges:
        return ""

    label = subject_label or edges[0].subject_urn
    lines = [
        f"## Known facts about {label} (from prior research)",
        "",
        "DO NOT re-research these unless the question hinges on freshness.",
        "Verify, contradict, or extend — find what's NEW.",
        "",
    ]
    for e in edges:
        # one bullet per edge: "(conf=0.85, last 2026-04-30) calls_service → CarrierRateService [urn:…]"
        last_seg = f", last {e.last_observed}" if e.last_observed else ""
        lines.append(
            f"- (conf={e.confidence:.2f}{last_seg}) {e.rel_type} → {e.target_name} [{e.target_urn}]"
        )
    return "\n".join(lines)


async def graph_facts_for_repo(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repo_id,
    predicates: Iterable[str] | None = None,
    min_confidence: float = 0.5,
    limit: int = 20,
) -> list[FactEdge]:
    """Top-K active edges where the source entity is scoped to ``repo_id``.

    The agent prompt wants facts about *what the repo's components do* —
    e.g., "ExampleApp.OrderRouter calls_service LedgerService".
    Joining on ``scope_repo_id`` is the natural way to pull edges rooted
    in repo-owned entities; cross-repo edges show up because the *source*
    is in this repo even when the target lives elsewhere.
    """
    pred_list = list(predicates) if predicates else []

    async with session_factory() as session:
        rows = (
            await session.execute(
                _REPO_FACTS_QUERY,
                {
                    "repo_id": str(repo_id),
                    "min_confidence": min_confidence,
                    "predicates": pred_list,
                    "predicates_count": len(pred_list),
                    "limit": limit,
                },
            )
        ).all()

    return [
        FactEdge(
            subject_urn=f"{r.subject_name} [{r.subject_urn}]",
            rel_type=r.rel_type,
            target_name=r.target_name,
            target_urn=r.target_urn,
            confidence=float(r.confidence) if r.confidence is not None else 0.0,
            confidence_level=str(r.target_confidence_level or "unknown"),
            last_observed=(r.last_observed.isoformat()[:10] if r.last_observed else ""),
        )
        for r in rows
    ]


async def render_repo_facts_block_for_kind(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repo_id,
    repo_name: str,
    kind: str,
    limit: int = 20,
    min_confidence: float = 0.5,
) -> str:
    """Per-repo facts block, gated by ``KIND_PREDICATES``.

    Returns ``""`` when the repo has no edges yet (fresh extraction or no
    matching predicates) so callers splice unconditionally.
    """
    preds = KIND_PREDICATES.get(kind)
    edges = await graph_facts_for_repo(
        session_factory=session_factory,
        repo_id=repo_id,
        predicates=preds,
        min_confidence=min_confidence,
        limit=limit,
    )
    if not edges:
        return ""

    lines = [
        f"## Known facts about {repo_name} (from prior research)",
        "",
        "DO NOT re-research these unless the question hinges on freshness.",
        "Verify, contradict, or extend — find what's NEW.",
        "",
    ]
    for e in edges:
        last_seg = f", last {e.last_observed}" if e.last_observed else ""
        lines.append(
            f"- (conf={e.confidence:.2f}{last_seg}) "
            f"{e.subject_urn} {e.rel_type} → {e.target_name} [{e.target_urn}]"
        )
    return "\n".join(lines)


async def render_facts_block_for_kind(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    subject_urn: str,
    kind: str,
    subject_label: str | None = None,
    limit: int = 20,
    min_confidence: float = 0.5,
) -> str:
    """Convenience: fetch + render in one call, gated by ``KIND_PREDICATES``.

    ``kind``: one of ``code|jira|confluence|github|slack``. Unknown kinds
    fall through to "no predicate filter" — better to overshare than hide
    facts from a future agent kind we haven't tuned.
    """
    preds = KIND_PREDICATES.get(kind)
    edges = await graph_facts_for_subject(
        session_factory=session_factory,
        subject_urn=subject_urn,
        predicates=preds,
        min_confidence=min_confidence,
        limit=limit,
    )
    return render_facts_block(edges, subject_label=subject_label)
