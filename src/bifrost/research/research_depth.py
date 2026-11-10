"""Depth helpers for chat research: blast-radius / domain candidate
gathering + sub-question decomposition.

These exist so the coordinator's reasoning-driven deepening loop chooses
from a pool that reflects what the involved code AFFECTS / is AFFECTED BY
(blast radius) and what lives in the SAME PRODUCT DOMAIN — not just
name-adjacent or cross-repo-edge neighbours. They are pure helpers: no
coordinator imports (avoids a circular dependency) and each is trivially
unit-testable with a mocked session / client.

Three pieces:

  * ``gather_blast_radius_candidates`` — walk ``expand_subgraph`` from the
    question's seed entities (direction="both", depth 2) and return the
    repos whose code is up/down-stream-impacted.
  * ``gather_domain_sibling_candidates`` — other ``Repo`` rows sharing a
    ``domain_id`` with any walked repo.
  * ``decompose_question`` — a reasoning step that breaks the user's
    question into 2-4 concrete sub-questions threaded into synthesis.

Everything is guarded: on any error the helpers return ``[]`` (candidate
gathering) or ``[]`` (decomposition) so the turn degrades to today's
behaviour and never breaks.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient
from bifrost.research.graph_retrieval import expand_subgraph
from bifrost.storage.models import Domain, Entity, Repo

logger = logging.getLogger(__name__)


async def rank_candidate_repos_by_relevance(
    session: AsyncSession,
    repo_ids: list[str],
    question_embedding: list[float] | None,
    *,
    keep: int,
) -> list[str]:
    """Rank candidate repo ids by their code's relevance to the question.

    THE relevance gate for scope widening: a repo is only worth walking if its
    code is actually about the question. We score each candidate by the BEST
    (smallest cosine distance) similarity of any of its entities to the
    question embedding, order most-relevant-first, and keep the top ``keep``.
    Repos with no relevant code (junk / unrelated domain siblings)
    sort last and fall off the cap — so they never
    reach the reasoning model or the fan-out. Repos with no embeddings keep
    their input order behind the scored ones. Falls back to input order (capped)
    when no question embedding is available, and never raises.
    """
    if not repo_ids or keep <= 0:
        return []
    if not question_embedding:
        return repo_ids[:keep]
    uuids: list[_uuid.UUID] = []
    for r in repo_ids:
        try:
            uuids.append(_uuid.UUID(r))
        except (ValueError, AttributeError):
            continue
    if not uuids:
        return repo_ids[:keep]
    try:
        stmt = (
            select(
                Entity.scope_repo_id,
                func.min(Entity.embedding.cosine_distance(question_embedding)).label("d"),
            )
            .where(Entity.scope_repo_id.in_(uuids), Entity.embedding.is_not(None))
            .group_by(Entity.scope_repo_id)
            .order_by("d")
        )
        rows = (await session.execute(stmt)).all()
        ranked = [str(rid) for (rid, _d) in rows]
        seen = set(ranked)
        tail = [r for r in repo_ids if r not in seen]  # no-embedding repos last
        return (ranked + tail)[:keep]
    except Exception:
        logger.exception("candidate relevance ranking failed; using input order")
        return repo_ids[:keep]


@dataclass
class ScopeCandidate:
    """One unwalked repo the reasoning model may choose to deepen into.

    ``why`` is a short human-readable label fed verbatim into
    ``select_repos_to_deepen``'s candidate prompt so the model knows WHY
    each repo is a candidate (blast-radius impact vs same-domain).
    """

    repo: Repo
    why: str


async def gather_blast_radius_candidates(
    session: AsyncSession,
    *,
    seed_entity_ids: list[str],
    walked_repo_ids: set[str],
    depth: int = 2,
    soft_cap: int = 80,
) -> list[ScopeCandidate]:
    """Repos whose code is up/down-stream-impacted by the seed entities.

    Walks the blast-radius graph (``expand_subgraph``, direction="both")
    from the question's seed entities — the entities in the master
    subgraph for the walked repos — and groups the reached nodes by
    ``repo_id``. Returns one :class:`ScopeCandidate` per impacted repo
    NOT already walked, ranked by how many impacted entities it carries
    (heaviest first). ``why`` names the closest seed so the reasoning
    model can read "blast-radius impact of <seed>".

    Never raises — returns ``[]`` on any error so scope gathering
    degrades to the cross-repo-edge + domain signals only.
    """
    if not seed_entity_ids:
        return []
    try:
        subgraph = await expand_subgraph(
            session,
            seed_entity_ids,
            depth=depth,
            soft_cap=soft_cap,
            direction="both",
        )
        if not subgraph.nodes:
            return []
        # A representative seed name for the ``why`` label. Seeds are the
        # distance-0 nodes; pick the first with a name.
        seed_name = ""
        for n in subgraph.nodes:
            if n.is_seed and n.name:
                seed_name = n.name
                break
        # Count impacted (non-seed) entities per repo so we can rank.
        impact_count: dict[str, int] = {}
        for n in subgraph.nodes:
            if n.is_seed or not n.repo_id:
                continue
            impact_count[n.repo_id] = impact_count.get(n.repo_id, 0) + 1
        # Drop already-walked repos.
        ranked = [
            rid
            for rid in sorted(impact_count, key=lambda r: impact_count[r], reverse=True)
            if rid not in walked_repo_ids
        ]
        if not ranked:
            return []
        repo_uuids = []
        for rid in ranked:
            try:
                repo_uuids.append(_uuid.UUID(rid))
            except (ValueError, AttributeError):
                continue
        if not repo_uuids:
            return []
        rows = (await session.execute(select(Repo).where(Repo.id.in_(repo_uuids)))).scalars().all()
        repo_by_id = {str(r.id): r for r in rows}
        why = (
            f"blast-radius impact of {seed_name}"
            if seed_name
            else "blast-radius impact of the question's seed entities"
        )
        return [
            ScopeCandidate(repo=repo_by_id[rid], why=why) for rid in ranked if rid in repo_by_id
        ]
    except Exception:
        logger.exception("blast-radius candidate gathering failed; not widening via blast radius")
        return []


async def gather_domain_sibling_candidates(
    session: AsyncSession,
    *,
    walked_repo_ids: set[str],
) -> list[ScopeCandidate]:
    """Other repos in the SAME product domain as any walked repo.

    Loads the ``domain_id`` of every walked repo, then returns the other
    ``Repo`` rows sharing one of those domains (excluding the walked set).
    ``why`` names the domain so the reasoning model reads "same domain
    (<domain name>)".

    Never raises — returns ``[]`` on any error.
    """
    if not walked_repo_ids:
        return []
    try:
        walked_uuids = []
        for rid in walked_repo_ids:
            try:
                walked_uuids.append(_uuid.UUID(rid))
            except (ValueError, AttributeError):
                continue
        if not walked_uuids:
            return []
        # domain_id of each walked repo.
        walked_rows = (
            await session.execute(select(Repo.domain_id).where(Repo.id.in_(walked_uuids)))
        ).all()
        domain_ids = {d for (d,) in walked_rows if d is not None}
        if not domain_ids:
            return []
        # Name map for the ``why`` label.
        domain_rows = (
            await session.execute(select(Domain.id, Domain.name).where(Domain.id.in_(domain_ids)))
        ).all()
        domain_name = {did: nm for (did, nm) in domain_rows}
        # Sibling repos in those domains, not already walked.
        sib_rows = (
            (
                await session.execute(
                    select(Repo)
                    .where(Repo.domain_id.in_(domain_ids))
                    .order_by(Repo.is_primary.desc(), Repo.name)
                )
            )
            .scalars()
            .all()
        )
        out: list[ScopeCandidate] = []
        for r in sib_rows:
            if str(r.id) in walked_repo_ids:
                continue
            dname = domain_name.get(r.domain_id) or "shared domain"
            out.append(ScopeCandidate(repo=r, why=f"same domain ({dname})"))
        return out
    except Exception:
        logger.exception("domain-sibling candidate gathering failed; not widening via domain")
        return []


# ── Sub-question decomposition ───────────────────────────────────────────


_DECOMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sub_questions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 4,
            "items": {"type": "string"},
            "description": "2-4 concrete, self-contained sub-questions that, "
            "answered together, fully answer the user's question. Each must "
            "be a distinct facet (a component, a hop, a decision, a "
            "failure-mode), phrased so a code-research agent can answer it "
            "independently. Empty when the question is already atomic.",
        },
    },
    "required": ["sub_questions"],
}

_DECOMPOSE_SYSTEM = """You decompose an engineering question for a \
code-research assistant into 2-4 concrete sub-questions. CODE IS THE GROUND \
TRUTH; the assistant answers each sub-question by walking source + the \
extracted entity graph (and corroborating docs/Jira/Slack).

Goal: break the question into the DISTINCT facets that must each be \
code-verified for a complete answer — e.g. a specific component's role, a \
specific hop in a flow, a specific decision/branch, a specific failure mode, \
or a specific cross-service boundary. Each sub-question must be:
  - self-contained (name the component/service/flow it's about),
  - answerable independently by walking code,
  - non-overlapping with the others,
  - genuinely load-bearing (don't pad to hit a count).

If the question is already atomic (a single direct lookup), return an empty \
list — do NOT invent sub-questions. Prefer FEWER, sharper sub-questions over \
more vague ones. Never restate the original question verbatim as a \
sub-question."""


async def decompose_question(
    question: str,
    settings: BifrostSettings | None = None,
    *,
    client: ClaudeLlmClient | None = None,
) -> list[str]:
    """Break ``question`` into 2-4 concrete sub-questions.

    One reasoning-tier ``claude -p`` call (no tools). Returns the list of
    sub-question strings, or ``[]`` when the question is atomic or on ANY
    error — the caller falls back to today's single-answer behaviour and
    the turn is never broken.
    """
    if not question or not question.strip():
        return []
    settings = settings or get_settings()
    client = client or ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_DECOMPOSE_SYSTEM,
            user_message=f"## Question\n{question}\n\nDecompose it.",
            model=settings.reasoning_model,
            json_schema=_DECOMPOSE_SCHEMA,
            max_turns=1,
            timeout_override=90,
        )
    except ClaudeCliError as exc:
        logger.warning("question decomposition failed (%s); single-answer mode", exc)
        return []
    except Exception:
        logger.exception("question decomposition crashed; single-answer mode")
        return []
    data = completion.parsed or {}
    raw = data.get("sub_questions") or []
    out: list[str] = []
    for q in raw:
        if isinstance(q, str) and q.strip():
            out.append(q.strip()[:400])
        if len(out) >= 4:
            break
    return out
