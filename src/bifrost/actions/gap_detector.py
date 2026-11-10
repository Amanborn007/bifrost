"""Coverage / gap assessment for agent research.

Before the research tool runs the full synthesis pipeline, we ask:
    "Does the graph have enough knowledge in scope to answer this?"

If yes → run research directly (cheap, fast).
If no  → suggest extractions first, let the caller decide whether to
         trigger them or answer with what we have.

Signal inputs used today
------------------------
- Embedding similarity between the question and entities in scope.
  Close matches (cosine distance < 0.5) are strong evidence the graph
  already covers the topic. ``min_distance`` and ``close_match_count``
  carry most of the signal.
- Total entity count in scope. An empty scope cannot be "covered" no
  matter what the question is.
- Org catalog keyword match. When coverage is low, we suggest repos
  from ``org_repos`` whose name/description/topics mention the
  question's keywords — those are the likely-useful extractions.

Signal we deliberately DON'T use here
-------------------------------------
- Edge count / graph density. Fancy but noisy at our scale.
- Community centroids. Communities are reshuffled every rebuild;
  coupling the gap detector to them would add brittle behavior.

Caller contract
---------------
This module is pure (modulo DB reads). It doesn't trigger extractions
or mutate state — the tools module is responsible for acting on the
assessment. The returned ``GapAssessment`` is dict-serializable for
easy logging / event payloads.
"""

from __future__ import annotations

import re
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.storage.models import Entity, OrgRepo

# Distance thresholds on the HNSW cosine index. MiniLM-L6-v2 yields
# cosine distances in roughly [0, 1.2]. Empirical buckets:
#   < 0.40 → nearly the same concept (very strong match)
#   < 0.55 → related concept (strong match)
#   < 0.70 → loosely on-topic (weak match)
#   > 0.70 → probably off-topic noise
_DIST_STRONG = 0.55
_DIST_WEAK = 0.70

# Verdict thresholds on the computed coverage_score (0..1)
_V_ADEQUATE = 0.60
_V_PARTIAL = 0.30


Verdict = Literal["adequate", "partial", "insufficient"]


@dataclass
class EntityMatch:
    id: str
    name: str
    entity_type: str
    distance: float
    scope_repo_id: str | None


@dataclass
class SuggestedRepo:
    owner: str
    name: str
    url: str
    description: str | None
    already_registered: bool
    reason: str  # why we think this repo helps


@dataclass
class GapAssessment:
    verdict: Verdict
    coverage_score: float  # 0..1
    min_distance: float | None  # None = scope has no embeddings
    strong_match_count: int  # distance < _DIST_STRONG
    weak_match_count: int  # _DIST_STRONG <= distance < _DIST_WEAK
    scope_entity_count: int  # total entities in scope
    entity_matches: list[EntityMatch] = field(default_factory=list)
    keywords_used: list[str] = field(default_factory=list)
    suggested_repos_to_extract: list[SuggestedRepo] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "coverage_score": round(self.coverage_score, 3),
            "min_distance": (
                round(self.min_distance, 3) if self.min_distance is not None else None
            ),
            "strong_match_count": self.strong_match_count,
            "weak_match_count": self.weak_match_count,
            "scope_entity_count": self.scope_entity_count,
            "keywords_used": self.keywords_used,
            "entity_matches": [
                {
                    "id": m.id,
                    "name": m.name,
                    "entity_type": m.entity_type,
                    "distance": round(m.distance, 3),
                    "scope_repo_id": m.scope_repo_id,
                }
                for m in self.entity_matches
            ],
            "suggested_repos_to_extract": [
                {
                    "owner": s.owner,
                    "name": s.name,
                    "url": s.url,
                    "description": s.description,
                    "already_registered": s.already_registered,
                    "reason": s.reason,
                }
                for s in self.suggested_repos_to_extract
            ],
        }


_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "is",
    "are",
    "was",
    "were",
    "be",
    "being",
    "been",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "what",
    "how",
    "why",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "with",
    "by",
    "from",
    "up",
    "down",
    "into",
    "over",
    "under",
    "then",
    "than",
    "if",
    "else",
    "can",
    "could",
    "should",
    "would",
    "we",
    "our",
    "us",
    "as",
    "at",
    "but",
    "not",
    "no",
    "yes",
    "my",
    "me",
    "you",
    "your",
}


def _extract_keywords(question: str, *, max_kw: int = 8) -> list[str]:
    """Pull out candidate keywords — lowercase, non-stopword, ≥ 3 chars.

    We also keep CamelCase / snake_case fragments intact because those
    are usually repo / component names. Example:
        "how does OrderService handle retries"
        → ["order", "service", "orderservice", "handle", "retries"]
    """
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", question)
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw:
        lower = tok.lower()
        if lower in _STOPWORDS:
            continue
        if lower not in seen:
            seen.add(lower)
            out.append(lower)
        # split CamelCase/snake — but only add sub-tokens ≥ 4 chars
        for sub in re.split(r"(?<=[a-z])(?=[A-Z])|_", tok):
            sl = sub.lower()
            if len(sl) >= 4 and sl not in seen and sl not in _STOPWORDS:
                seen.add(sl)
                out.append(sl)
        if len(out) >= max_kw:
            break
    return out[:max_kw]


async def _count_scope_entities(
    session: AsyncSession,
    repo_ids: list[str] | None,
) -> int:
    stmt = select(func.count(Entity.id))
    if repo_ids:
        stmt = stmt.where(Entity.scope_repo_id.in_([_uuid.UUID(r) for r in repo_ids]))
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _find_matches(
    session: AsyncSession,
    *,
    question: str,
    repo_ids: list[str] | None,
    top_k: int = 20,
) -> tuple[list[EntityMatch], float | None]:
    """Top-K entities by cosine distance. Also returns min distance."""
    try:
        from bifrost.research.embeddings import embed_one

        q_vec = embed_one(question)
    except Exception:
        return [], None

    dist_expr = Entity.embedding.cosine_distance(q_vec)
    stmt = (
        select(
            Entity.id,
            Entity.name,
            Entity.entity_type,
            Entity.scope_repo_id,
            dist_expr.label("distance"),
        )
        .where(Entity.embedding.isnot(None))
        .order_by(dist_expr)
        .limit(top_k)
    )
    if repo_ids:
        stmt = stmt.where(Entity.scope_repo_id.in_([_uuid.UUID(r) for r in repo_ids]))
    rows = (await session.execute(stmt)).all()
    if not rows:
        return [], None

    matches = [
        EntityMatch(
            id=str(r.id),
            name=r.name,
            entity_type=r.entity_type,
            distance=float(r.distance),
            scope_repo_id=str(r.scope_repo_id) if r.scope_repo_id else None,
        )
        for r in rows
    ]
    return matches, matches[0].distance


def _compute_coverage_score(
    *,
    min_dist: float | None,
    strong: int,
    weak: int,
    scope_size: int,
) -> float:
    """Squash all signals into a single 0..1 score.

    Rules of thumb:
      - If scope has no entities: score = 0 regardless.
      - If no match within weak threshold: score ≤ 0.2 (caller likely
        needs extraction).
      - 1 strong match alone lifts score above 0.5 (graph clearly
        covers the topic).
      - Each extra strong match adds small diminishing-returns weight.
    """
    if scope_size == 0 or min_dist is None:
        return 0.0
    if min_dist > _DIST_WEAK:
        # best match is noise — tiny coverage
        return max(0.0, 0.20 - 0.05 * min(min_dist - _DIST_WEAK, 1.0))

    # Strong-match contribution: diminishing returns after ~3.
    strong_component = min(0.5, 0.25 * strong + 0.10 * max(0, strong - 1))
    # Weak-match contribution: smaller weight.
    weak_component = min(0.25, 0.05 * weak)
    # Min-distance contribution: closer = better.
    dist_component = max(0.0, (_DIST_WEAK - min_dist) / _DIST_WEAK) * 0.40

    raw = strong_component + weak_component + dist_component
    return max(0.0, min(1.0, raw))


def _verdict_from_score(score: float) -> Verdict:
    if score >= _V_ADEQUATE:
        return "adequate"
    if score >= _V_PARTIAL:
        return "partial"
    return "insufficient"


async def _suggest_repos_to_extract(
    session: AsyncSession,
    *,
    keywords: list[str],
    max_suggestions: int = 5,
) -> list[SuggestedRepo]:
    """Keyword-match against org_repos. Prefer unextracted over extracted.

    The scoring is intentionally simple: rank by whether any keyword
    hits name/description/topics. We prefer unregistered repos because
    the caller's goal is usually "find repos we don't know about yet."
    """
    if not keywords:
        return []

    # Build an OR clause across name + description + topics JSON
    ilike_patterns = [f"%{kw}%" for kw in keywords]
    name_clauses = [func.lower(OrgRepo.name).like(p) for p in ilike_patterns]
    desc_clauses = [func.lower(OrgRepo.description).like(p) for p in ilike_patterns]
    # JSONB topics match via text search — cheaper than ->>
    topic_clauses = [
        text(":kw = ANY (SELECT jsonb_array_elements_text(topics))").bindparams(kw=kw)
        for kw in keywords
    ]

    stmt = (
        select(OrgRepo)
        .where(
            or_(
                *name_clauses,
                *desc_clauses,
                *topic_clauses,
            )
        )
        .where(OrgRepo.archived == False)  # noqa: E712
        .order_by(
            # prefer unregistered
            OrgRepo.registered_repo_id.is_(None).desc(),
            # then larger / more active
            OrgRepo.pushed_at.desc().nullslast(),
        )
        .limit(max_suggestions * 3)
    )
    rows = (await session.execute(stmt)).scalars().all()

    suggestions: list[SuggestedRepo] = []
    for r in rows:
        reason_bits = []
        lname = (r.name or "").lower()
        ldesc = (r.description or "").lower()
        for kw in keywords:
            if kw in lname:
                reason_bits.append(f"name matches '{kw}'")
                break
            if kw in ldesc:
                reason_bits.append(f"description mentions '{kw}'")
                break
        if not reason_bits:
            reason_bits.append("topic tag matches one of the keywords")
        suggestions.append(
            SuggestedRepo(
                owner=r.owner,
                name=r.name,
                url=r.url,
                description=r.description,
                already_registered=r.registered_repo_id is not None,
                reason="; ".join(reason_bits),
            )
        )
        if len(suggestions) >= max_suggestions:
            break
    return suggestions


async def assess_coverage(
    session: AsyncSession,
    *,
    question: str,
    scope_repo_ids: list[str] | None = None,
    top_k: int = 20,
    suggest_extractions: bool = True,
    max_suggestions: int = 5,
) -> GapAssessment:
    """Return a GapAssessment for (question, scope).

    ``scope_repo_ids`` constrains the entity search; None = all repos.
    """
    keywords = _extract_keywords(question)

    scope_size = await _count_scope_entities(session, scope_repo_ids)
    matches, min_dist = await _find_matches(
        session,
        question=question,
        repo_ids=scope_repo_ids,
        top_k=top_k,
    )

    strong = sum(1 for m in matches if m.distance < _DIST_STRONG)
    weak = sum(1 for m in matches if _DIST_STRONG <= m.distance < _DIST_WEAK)

    score = _compute_coverage_score(
        min_dist=min_dist,
        strong=strong,
        weak=weak,
        scope_size=scope_size,
    )
    verdict = _verdict_from_score(score)

    suggestions: list[SuggestedRepo] = []
    if suggest_extractions and verdict != "adequate":
        suggestions = await _suggest_repos_to_extract(
            session,
            keywords=keywords,
            max_suggestions=max_suggestions,
        )

    return GapAssessment(
        verdict=verdict,
        coverage_score=score,
        min_distance=min_dist,
        strong_match_count=strong,
        weak_match_count=weak,
        scope_entity_count=scope_size,
        entity_matches=matches[:top_k],
        keywords_used=keywords,
        suggested_repos_to_extract=suggestions,
    )
