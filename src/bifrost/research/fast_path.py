"""Fast-path answer route for simple entity/ownership lookup queries.

Bypasses the full 90-second coordinator pipeline for simple lookups:
- "what is X?" / "what does X do?" → entity graph lookup
- "who owns Y?" / "what team works on Z?" → RepoOwnership lookup

Answers in <2 seconds with one synthesis LLM call using only Postgres data.
No agents, no fan-out, no freshness checks.

Trigger conditions
------------------
A question triggers the fast path when ALL of the following hold:

1. ENTITY or OWNERSHIP signal detected:
   - "what is", "what does", "what's", "describe", "explain" → entity lookup
   - "who owns", "what team", "what squad", "who works on" → ownership lookup

2. An entity name OR repo name matching the question is found in Postgres
   (case-insensitive substring match, score ≥ 1 candidate).

3. No cross-cutting qualifier present:
   - "compare", "how do they interact", "what's the impact", "blast radius",
     "depends on", "dependencies" → these require the full pipeline.

If _try_fast_path returns None, caller falls through to run_research().
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings
from bifrost.extraction.cli_client import ClaudeLlmClient
from bifrost.storage.models import Entity, EntityCommunity, EntityRelationship, Repo, RepoOwnership

logger = logging.getLogger(__name__)

# ── Heuristic vocabulary ──────────────────────────────────────────────────────

_DEFINITION_PATTERNS = re.compile(
    r"\b(what is|what's|what are|what does|describe|explain|tell me about"
    r"|how does|what do you know about)\b",
    re.IGNORECASE,
)

_OWNERSHIP_PATTERNS = re.compile(
    r"\b(who owns|what team|what squad|who works on|who is responsible for"
    r"|which team|which squad|who maintains|who built|ownership of)\b",
    re.IGNORECASE,
)

# Any of these words in the question → fast path should not apply
_CROSS_CUTTING_PATTERNS = re.compile(
    r"\b(compare|comparison|how do they interact|what.s the impact|blast radius"
    r"|dependencies|depends on|dependency|interacts with|vs\b|versus"
    r"|across all|across repos|multiple repos|end.to.end|e2e"
    r"|flow|pipeline|architecture|diagram)\b",
    re.IGNORECASE,
)

# ── Fast-path trigger detection ───────────────────────────────────────────────


def _detect_fast_path_intent(question: str) -> str | None:
    """Return "entity", "ownership", or None.

    Returns None if the question contains cross-cutting signals that
    require the full coordinator pipeline.
    """
    if _CROSS_CUTTING_PATTERNS.search(question):
        return None

    if _OWNERSHIP_PATTERNS.search(question):
        return "ownership"

    if _DEFINITION_PATTERNS.search(question):
        return "entity"

    return None


# ── Postgres lookups ──────────────────────────────────────────────────────────


async def _find_entity_by_name(
    session: AsyncSession,
    question: str,
    scope_repo_ids: list[str] | None = None,
) -> Entity | None:
    """Case-insensitive fuzzy match against Entity.name.

    Prefers higher-confidence, higher-knowledge-tier entities.
    Respects scope_repo_ids when provided.
    """
    # Extract potential entity name tokens: words 3+ chars, skip stop words
    _STOP = {
        "what",
        "does",
        "the",
        "a",
        "an",
        "is",
        "are",
        "who",
        "which",
        "how",
        "tell",
        "me",
        "about",
        "you",
        "know",
        "explain",
        "describe",
        "for",
        "on",
        "in",
        "of",
        "and",
        "or",
        "it",
        "this",
        "that",
    }
    tokens = [
        w.strip("?.,!:;\"'()")
        for w in question.split()
        if len(w.strip("?.,!:;\"'()")) >= 3 and w.lower().strip("?.,!:;\"'()") not in _STOP
    ]
    if not tokens:
        return None

    # Build a disjunction: entity name contains any of the candidate tokens
    conditions = [func.lower(Entity.name).contains(tok.lower()) for tok in tokens]

    stmt = (
        select(Entity)
        .where(or_(*conditions))
        .order_by(
            Entity.knowledge_tier.desc(),
            Entity.confidence.desc(),
        )
        .limit(1)
    )

    if scope_repo_ids:
        try:
            uuids = [uuid.UUID(r) for r in scope_repo_ids]
            stmt = stmt.where(Entity.scope_repo_id.in_(uuids))
        except (ValueError, AttributeError):
            pass  # malformed UUIDs — ignore scope filter

    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_repo_with_ownership(
    session: AsyncSession,
    question: str,
    scope_repo_ids: list[str] | None = None,
) -> tuple[Repo, RepoOwnership] | None:
    """Find a repo + its ownership row whose name appears in the question."""
    _STOP = {
        "who",
        "owns",
        "what",
        "team",
        "squad",
        "works",
        "on",
        "is",
        "the",
        "responsible",
        "for",
        "which",
        "maintains",
        "built",
        "ownership",
    }
    tokens = [
        w.strip("?.,!:;\"'()")
        for w in question.split()
        if len(w.strip("?.,!:;\"'()")) >= 3 and w.lower().strip("?.,!:;\"'()") not in _STOP
    ]
    if not tokens:
        return None

    conditions = [func.lower(Repo.name).contains(tok.lower()) for tok in tokens]

    stmt = (
        select(Repo, RepoOwnership)
        .join(RepoOwnership, RepoOwnership.repo_id == Repo.id, isouter=True)
        .where(or_(*conditions))
        .limit(1)
    )
    if scope_repo_ids:
        try:
            uuids = [uuid.UUID(r) for r in scope_repo_ids]
            stmt = stmt.where(Repo.id.in_(uuids))
        except (ValueError, AttributeError):
            pass

    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    repo, ownership = row
    return (repo, ownership) if ownership is not None else None


async def _fetch_one_hop_relationships(
    session: AsyncSession,
    entity_id: uuid.UUID,
) -> list[EntityRelationship]:
    """Fetch 1-hop outbound + inbound relationships for an entity."""
    stmt = (
        select(EntityRelationship)
        .where(
            or_(
                EntityRelationship.source_id == entity_id,
                EntityRelationship.target_id == entity_id,
            )
        )
        .limit(20)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _fetch_community_summary(
    session: AsyncSession,
    community_id: uuid.UUID,
) -> str | None:
    """Return community summary text if available."""
    stmt = select(EntityCommunity.summary).where(EntityCommunity.id == community_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ── Synthesis prompt builders ─────────────────────────────────────────────────

_ENTITY_SYNTHESIS_TEMPLATE = """\
You are Bifrost, an engineering knowledge assistant. Answer the user's question
using ONLY the structured graph data below. Be concise and precise.

## Entity
- Name: {name}
- Type: {entity_type}
- Qualified name: {qualified_name}
- URN: {urn}
- Confidence: {confidence} ({confidence_level})
- Repo: {repo_name}

{metadata_block}

## Relationships (1-hop)
{relationships_block}

{community_block}

## User question
{question}

Answer in 2-5 sentences. Cite the entity URN inline. If data is insufficient,
say so — do not guess.
"""

_OWNERSHIP_SYNTHESIS_TEMPLATE = """\
You are Bifrost, an engineering knowledge assistant. Answer the user's question
about team/squad ownership using ONLY the structured data below.

## Repository
- Name: {repo_name}
- URN: {repo_urn}
- URL: {repo_url}
- Description: {repo_description}

## Ownership (from the ownership manifest)
- Squad: {squad}
- Service tier: {service_tier}
- Jira project: {jira_project}
- Confluence space: {confluence_space}
- Slack channel: {slack_channel}
- Data classification: {data_classification}
- Compliance tags: {compliance_tags}

## User question
{question}

Answer in 2-4 sentences. Be direct about ownership details.
"""


def _build_entity_prompt(
    entity: Entity,
    relationships: list[EntityRelationship],
    community_summary: str | None,
    question: str,
) -> str:
    # Metadata block
    meta = entity.entity_metadata
    meta_lines = []
    for k, v in (meta or {}).items():
        if v and k not in ("id", "urn"):
            meta_lines.append(f"- {k}: {v}")
    metadata_block = "## Metadata\n" + "\n".join(meta_lines) if meta_lines else ""

    # Relationships
    rel_lines = []
    for rel in relationships:
        if rel.source_id == entity.id:
            rel_lines.append(f"- OUTBOUND {rel.rel_type} → (target entity id: {rel.target_id})")
        else:
            rel_lines.append(f"- INBOUND {rel.rel_type} ← (source entity id: {rel.source_id})")
    relationships_block = "\n".join(rel_lines) if rel_lines else "(none)"

    # Community
    community_block = ""
    if community_summary:
        community_block = f"## Community summary\n{community_summary}"

    repo_name = entity.scope_repo.name if entity.scope_repo else "(unknown)"

    return _ENTITY_SYNTHESIS_TEMPLATE.format(
        name=entity.name,
        entity_type=entity.entity_type,
        qualified_name=entity.qualified_name or "(none)",
        urn=entity.urn,
        confidence=entity.confidence,
        confidence_level=entity.confidence_level,
        repo_name=repo_name,
        metadata_block=metadata_block,
        relationships_block=relationships_block,
        community_block=community_block,
        question=question,
    )


def _build_ownership_prompt(
    repo: Repo,
    ownership: RepoOwnership,
    question: str,
) -> str:
    return _OWNERSHIP_SYNTHESIS_TEMPLATE.format(
        repo_name=repo.name,
        repo_urn=repo.urn,
        repo_url=repo.url,
        repo_description=repo.description or "(none)",
        squad=ownership.squad or "(not set)",
        service_tier=ownership.service_tier or "(not set)",
        jira_project=ownership.jira_project_key or "(not set)",
        confluence_space=ownership.confluence_space_key or "(not set)",
        slack_channel=ownership.slack_channel or "(not set)",
        data_classification=ownership.data_classification or "(not set)",
        compliance_tags=", ".join(ownership.compliance_tags) or "none",
        question=question,
    )


# ── Public entry point ────────────────────────────────────────────────────────


async def _try_fast_path(
    question: str,
    conversation_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: BifrostSettings,
    scope_repo_ids: list[str] | None = None,
) -> AsyncGenerator[dict[str, Any], None] | None:
    """Attempt the fast path for simple entity/ownership lookups.

    Returns an async generator that yields SSE-compatible event dicts,
    OR returns None if the fast path does not apply (caller should invoke
    the full run_research() pipeline).

    Event types yielded on the fast path:
      {"type": "scope", "fast": True, "route": <repo_name>}
      {"type": "synthesis_delta", "text": <str>}
      {"type": "done", "answer": <str>, "sources": [...],
               "confidence": "graph-grounded", "fast_path": True}
    """
    intent = _detect_fast_path_intent(question)
    if intent is None:
        logger.debug("fast_path: cross-cutting question detected, falling through")
        return None

    # We need at least a candidate match in Postgres before committing
    async with session_factory() as session:
        entity: Entity | None = None
        repo_ownership: tuple[Repo, RepoOwnership] | None = None

        if intent == "entity":
            entity = await _find_entity_by_name(session, question, scope_repo_ids)
        elif intent == "ownership":
            repo_ownership = await _find_repo_with_ownership(session, question, scope_repo_ids)
            # Also try entity lookup as fallback
            if repo_ownership is None:
                entity = await _find_entity_by_name(session, question, scope_repo_ids)
                # BUG FIX (fast-path-intent-mismatch): when ownership lookup
                # fails but entity fallback succeeds, update intent so the UI
                # renders the correct card type instead of the stale "ownership".
                if entity is not None:
                    intent = "entity"

        if entity is None and repo_ownership is None:
            logger.debug("fast_path: no Postgres candidate found for question=%r", question[:80])
            return None

        # Fetch related data while session is open
        relationships: list[EntityRelationship] = []
        community_summary: str | None = None
        synthesis_prompt: str = ""
        route_name: str = "(unknown)"

        if entity is not None:
            # Eagerly load scope_repo so it's available outside session
            await session.refresh(entity, ["scope_repo"])
            relationships = await _fetch_one_hop_relationships(session, entity.id)
            if entity.community_id is not None:
                community_summary = await _fetch_community_summary(session, entity.community_id)
            synthesis_prompt = _build_entity_prompt(
                entity, relationships, community_summary, question
            )
            route_name = entity.scope_repo.name if entity.scope_repo else "(unknown)"
        elif repo_ownership is not None:
            repo, ownership = repo_ownership
            synthesis_prompt = _build_ownership_prompt(repo, ownership, question)
            route_name = repo.name

    logger.info(
        "fast_path: matched intent=%s route=%r for question=%r",
        intent,
        route_name,
        question[:80],
    )

    # Return the generator — actual streaming happens when caller iterates
    return _run_fast_path_synthesis(
        synthesis_prompt=synthesis_prompt,
        route_name=route_name,
        question=question,
        settings=settings,
        entity=entity,
        repo_ownership=repo_ownership,
        intent=intent,
    )


async def _run_fast_path_synthesis(
    *,
    synthesis_prompt: str,
    route_name: str,
    question: str,
    settings: BifrostSettings,
    entity: Entity | None,
    repo_ownership: tuple[Repo, RepoOwnership] | None,
    intent: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """Async generator: streams synthesis events for the fast path."""
    yield {"type": "scope", "fast": True, "route": route_name, "candidates": [{"name": route_name}]}

    client = ClaudeLlmClient(settings)
    full_answer_parts: list[str] = []

    system_prompt = (
        "You are Bifrost, an engineering knowledge assistant. "
        "Answer concisely using only the provided graph data. "
        "Do not speculate beyond what the data says."
    )

    try:
        async for chunk in client.stream_text(
            system_prompt=system_prompt,
            user_message=synthesis_prompt,
            model=settings.triage_model,
            max_turns=1,
            allowed_tools=None,
        ):
            chunk_type = chunk.get("type")
            if chunk_type == "delta":
                text = chunk.get("text", "")
                if text:
                    full_answer_parts.append(text)
                    yield {"type": "synthesis_delta", "text": text}
            elif chunk_type == "done":
                break
    except Exception as exc:
        logger.warning("fast_path: synthesis failed (%s); falling through", exc)
        # Signal to caller that we should fall back to full research
        yield {"type": "_fast_path_synthesis_error", "error": str(exc)}
        return

    final_answer = "".join(full_answer_parts)
    if not final_answer.strip():
        yield {"type": "_fast_path_synthesis_error", "error": "empty synthesis"}
        return

    # Build sources list
    sources: list[dict[str, Any]] = []
    if entity is not None:
        sources.append(
            {
                "kind": "entity",
                "urn": entity.urn,
                "name": entity.name,
                "entity_type": entity.entity_type,
            }
        )
    elif repo_ownership is not None:
        repo, ownership = repo_ownership
        sources.append(
            {
                "kind": "repo_ownership",
                "urn": repo.urn,
                "name": repo.name,
                "squad": ownership.squad,
            }
        )

    yield {
        "type": "done",
        "answer": final_answer,
        "sources": sources,
        "confidence": "graph-grounded",
        "fast_path": True,
        "fast_path_intent": intent,
        "route": route_name,
        "usage": {"total_cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 1},
    }


# ── Already-built detector ────────────────────────────────────────────────────

_BUILD_REQUEST_PATTERNS = re.compile(
    r"\b(need to build|want to build|how do I build|how would I build"
    r"|how do I implement|how would I implement"
    r"|build .{0,40} from scratch|implement .{0,40} ourselves"
    r"|what would it take to build|we should build|planning to build"
    r"|thinking of building|looking to build|going to build)\b",
    re.IGNORECASE,
)

# Stop words to skip when extracting noun tokens from the build question
_BUILD_STOP_WORDS = {
    "need",
    "want",
    "how",
    "do",
    "build",
    "implement",
    "would",
    "should",
    "what",
    "take",
    "from",
    "scratch",
    "ourselves",
    "planning",
    "thinking",
    "looking",
    "going",
    "could",
    "can",
    "i",
    "we",
    "to",
    "the",
    "a",
    "an",
    "is",
    "are",
    "it",
    "this",
    "that",
    "for",
    "on",
    "in",
    "of",
    "and",
    "or",
    "will",
    "be",
    "get",
    "let",
    "with",
    "new",
    "own",
    "our",
    "my",
    "your",
    "like",
    "just",
    "also",
    "into",
    "out",
    "up",
    "down",
    "all",
    "any",
    "some",
    "about",
    "there",
    "their",
    "which",
    "who",
}


async def check_already_built(
    question: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: BifrostSettings,
) -> AsyncIterator[dict[str, Any]]:
    """Pre-check: does the question ask us to build something that already exists?

    If the question looks like a build request AND we find matching entities in
    the graph with confidence > 0.5, yield an "already_built" event so the UI
    can surface a banner. The main pipeline continues regardless — this is an
    advisory signal only.

    Yields nothing when the question is not a build request or no matches found.
    """
    if not _BUILD_REQUEST_PATTERNS.search(question):
        return

    # Extract candidate noun tokens (3+ chars, not stop words)
    tokens = [
        w.strip("?.,!:;\"'()")
        for w in question.split()
        if len(w.strip("?.,!:;\"'()")) >= 3
        and w.lower().strip("?.,!:;\"'()") not in _BUILD_STOP_WORDS
    ]
    if not tokens:
        return

    try:
        async with session_factory() as session:
            conditions = [func.lower(Entity.name).contains(tok.lower()) for tok in tokens]
            stmt = (
                select(
                    Entity.name,
                    Entity.urn,
                    Entity.entity_type,
                    Entity.scope_repo_id,
                    Entity.confidence,
                )
                .where(or_(*conditions))
                .where(Entity.confidence > 0.5)
                .order_by(Entity.confidence.desc())
                .limit(5)
            )
            result = await session.execute(stmt)
            rows = result.all()

            if not rows:
                return

            # Fetch repo names for the matched entities
            repo_ids = [r.scope_repo_id for r in rows if r.scope_repo_id is not None]
            repo_name_map: dict[uuid.UUID, str] = {}
            if repo_ids:
                repo_stmt = select(Repo.id, Repo.name).where(Repo.id.in_(repo_ids))
                repo_result = await session.execute(repo_stmt)
                repo_name_map = {r.id: r.name for r in repo_result.all()}

            matches = [
                {
                    "name": r.name,
                    "urn": r.urn,
                    "entity_type": r.entity_type,
                    "repo_name": repo_name_map.get(r.scope_repo_id, "(unknown)")
                    if r.scope_repo_id
                    else "(unknown)",
                }
                for r in rows
            ]

        count = len(matches)
        logger.info(
            "already_built: found %d match(es) for build question %r",
            count,
            question[:80],
        )
        yield {
            "type": "already_built",
            "matches": matches,
            "message": (
                f"Found {count} existing implementation{'s' if count != 1 else ''}"
                " that may be what you need:"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        # DB failures must be silent — the main pipeline continues regardless.
        # Log a warning for operator visibility but do not emit an event,
        # so callers see the same empty-result behaviour as "no match found".
        logger.warning("already_built check failed: %s", exc)
