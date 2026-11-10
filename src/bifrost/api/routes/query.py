"""Knowledge query submission handler.

V1: keyword search against extracted entities (no LLM needed).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from bifrost.api.deps import Repo
from bifrost.api.schemas.envelope import BifrostEnvelope, EnvelopeMeta
from bifrost.api.schemas.query import QueryAnswer, QueryRequest, SourceCitation
from bifrost.core.enums import ConfidenceLevel

router = APIRouter(prefix="/query", tags=["query"])


def _build_answer(entities: list[tuple]) -> str:
    """Build a human-readable answer from matched entities."""
    if not entities:
        return "No matching entities found in the knowledge base. Try running an extraction first, or rephrase your question."

    lines: list[str] = []
    lines.append(f"Found {len(entities)} matching entities:\n")

    for entity, _score in entities:
        meta = entity.entity_metadata or {}
        purpose = meta.get("purpose", "")
        etype = entity.entity_type or "unknown"
        line = f"- **{entity.name}** ({etype})"
        if purpose:
            line += f": {purpose}"
        lines.append(line)

    return "\n".join(lines)


def _assess_confidence(entities: list[tuple]) -> str:
    """Determine confidence level from keyword-match score.

    Returns canonical ConfidenceLevel values (per S3 spec) so this endpoint
    stays vocabulary-consistent with the rest of the API.

    Score is the keyword-match count from search_entities_by_keyword; it is
    a match-quality proxy, not a freshness signal, so STALE is never returned
    here (freshness staleness is computed on the entity, not on the query).
    """
    if not entities:
        return ConfidenceLevel.UNKNOWN.value
    top_score = entities[0][1]
    if top_score >= 3:
        return ConfidenceLevel.CONFIRMED.value
    if top_score >= 2:
        return ConfidenceLevel.INFERRED.value
    return ConfidenceLevel.UNCERTAIN.value


@router.post(
    "",
    response_model=BifrostEnvelope[QueryAnswer],
    status_code=status.HTTP_200_OK,
)
async def submit_query(
    body: QueryRequest,
    repo: Repo,
) -> BifrostEnvelope[QueryAnswer]:
    """Submit a natural-language question. Returns structured answer with confidence + sources.

    V1: Keyword search against extracted entities. No LLM needed.
    """
    # Create task for audit trail
    task = await repo.create_task(
        task_type="question",
        query=body.question,
        scope_json={
            "repo_ids": body.repo_ids or [],
            "domain_id": body.domain_id,
        },
        budget_json={},
    )

    # Search entities by keyword
    repo_id = None
    if body.repo_ids and len(body.repo_ids) == 1:
        repo_id = uuid.UUID(body.repo_ids[0])

    results = await repo.search_entities_by_keyword(
        query=body.question,
        repo_id=repo_id,
        limit=20,
    )

    # Fallback: if no keyword matches but a repo is selected, show all entities from that repo
    if not results and repo_id is not None:
        all_entities = await repo.list_entities(repo_id=repo_id, limit=20)
        results = [(e, 0) for e in all_entities]

    # Build structured answer
    answer_text = _build_answer(results)
    confidence = _assess_confidence(results)

    sources = [
        SourceCitation(
            urn=entity.urn,
            name=entity.name,
            entity_type=entity.entity_type,
            relevance=float(score),
        )
        for entity, score in results
    ]

    # Identify gaps
    gaps: list[str] = []
    if not results:
        gaps.append("No entities match your query. The knowledge base may need an extraction run.")
    elif len(results) < 3:
        gaps.append(
            "Limited matches found. Consider running a fresh extraction for more complete results."
        )

    return BifrostEnvelope(
        data=QueryAnswer(
            answer=answer_text,
            confidence=confidence,
            sources=sources,
            gaps=gaps,
            conflicts=[],
            task_id=str(task.id),
        ),
        # Trust contract (S10): surface confidence / evidence / gaps in the
        # uniform `meta` location, not only in the data body, so a
        # spec-following client reads the same fields on every answer route.
        meta=EnvelopeMeta(
            confidence=confidence,
            gaps=gaps or None,
            task_id=str(task.id),
            data_source="extraction",
            cache_status="live",
            evidence_summary=f"{len(results)} keyword match(es)",
        ),
    )
