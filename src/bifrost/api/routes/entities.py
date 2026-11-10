"""Entity search/filter/get route handlers."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from bifrost.api.deps import DB, Repo
from bifrost.api.schemas.entities import EntityDetail, EntitySearchResult, EntitySummary
from bifrost.api.schemas.envelope import BifrostEnvelope, EnvelopeMeta
from bifrost.storage.models import Entity, EntityRelationship
from bifrost.storage.models import Repo as RepoModel

router = APIRouter(prefix="/entities", tags=["entities"])


class RelationshipEdge(BaseModel):
    """One relationship with the partner entity's display data."""

    id: str
    rel_type: str
    direction: str  # "outbound" | "inbound"
    source_urn: str
    source_name: str
    source_entity_type: str | None
    target_urn: str
    target_name: str
    target_entity_type: str | None
    confidence: str | None = None


class EntityRelationshipsResponse(BaseModel):
    outbound: list[RelationshipEdge]
    inbound: list[RelationshipEdge]


class EntitySourceResponse(BaseModel):
    file_path: str
    content: str
    truncated: bool = False


def _rel_confidence_label(score: float | None) -> str | None:
    """Map a relationship's float ``confidence`` to a level label.

    ``EntityRelationship`` stores only a float ``confidence`` (no
    ``confidence_level`` column — unlike ``Entity``). Use the canonical
    thresholds (graph_retrieval.py: >=0.85 confirmed, >=0.55 inferred,
    else uncertain) so the relationship edge surfaces a level consistent
    with the rest of the system.
    """
    if score is None:
        return None
    if score >= 0.85:
        return "confirmed"
    if score >= 0.55:
        return "inferred"
    return "uncertain"


def _entity_summary(e: object) -> EntitySummary:
    """Convert an ORM Entity to an EntitySummary schema."""
    return EntitySummary(
        id=str(e.id),  # type: ignore[attr-defined]
        urn=e.urn,  # type: ignore[attr-defined]
        name=e.name,  # type: ignore[attr-defined]
        entity_type=e.entity_type,  # type: ignore[attr-defined]
        confidence_level=e.confidence_level,  # type: ignore[attr-defined]
        freshness_state=e.freshness_state,  # type: ignore[attr-defined]
    )


@router.get("/search", response_model=BifrostEnvelope[list[EntitySearchResult]])
async def search_entities(
    repo: Repo,
    q: str = Query(..., description="Search query for pgvector similarity"),
    entity_type: str | None = Query(None, description="Filter by entity type"),
    limit: int = Query(10, ge=1, le=100),
) -> BifrostEnvelope[list[EntitySearchResult]]:
    """Search entities using pgvector cosine similarity.

    V1: Uses a zero-vector placeholder since embedding generation is not yet wired.
    When the embedding pipeline is connected, this will accept real query embeddings.
    """
    # V1 placeholder: return empty results since we can't generate embeddings yet.
    # The pgvector search infra is in EntityRepository.search_entities_by_embedding.
    # Once an embedding provider is wired, replace this with actual vector search.
    # 384 dims to match sentence-transformers/all-MiniLM-L6-v2 (the
    # current entity embedding model — see the baseline migration).
    placeholder_embedding = [0.0] * 384
    results = await repo.search_entities_by_embedding(
        embedding=placeholder_embedding,
        limit=limit,
        entity_type=entity_type,
    )
    return BifrostEnvelope(
        data=[
            EntitySearchResult(entity=_entity_summary(entity), score=1.0 - distance)
            for entity, distance in results
        ],
    )


@router.get("", response_model=BifrostEnvelope[list[EntitySummary]])
async def list_entities(
    repo: Repo,
    entity_type: str | None = Query(None),
    repo_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> BifrostEnvelope[list[EntitySummary]]:
    """List/filter entities by type, repo, or domain."""
    entities = await repo.list_entities(
        entity_type=entity_type,
        repo_id=uuid.UUID(repo_id) if repo_id else None,
        domain_id=uuid.UUID(domain_id) if domain_id else None,
        limit=limit,
        offset=offset,
    )
    return BifrostEnvelope(data=[_entity_summary(e) for e in entities])


@router.get(
    "/by-id/{entity_id}/relationships",
    response_model=BifrostEnvelope[EntityRelationshipsResponse],
)
async def entity_relationships(
    entity_id: str,
    repo: Repo,
) -> BifrostEnvelope[EntityRelationshipsResponse]:
    """Inbound + outbound relationships for an entity, joined with partner names."""
    try:
        eid = uuid.UUID(entity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity id")

    # Use the repository's session (same pattern as /dependencies/blast-radius)
    # so the handler runs on a single session rather than two independent ones.
    session = repo._session  # noqa: SLF001 -- intentional shared session

    # Join: for each relationship, we need the OTHER side's entity row
    # Outbound: source_id == eid → get target info
    out_stmt = (
        select(EntityRelationship, Entity)
        .join(Entity, Entity.id == EntityRelationship.target_id)
        .where(EntityRelationship.source_id == eid)
    )
    out_rows = (await session.execute(out_stmt)).all()

    in_stmt = (
        select(EntityRelationship, Entity)
        .join(Entity, Entity.id == EntityRelationship.source_id)
        .where(EntityRelationship.target_id == eid)
    )
    in_rows = (await session.execute(in_stmt)).all()

    # Fetch self for the source_name/target_name of the opposite side
    self_entity = await session.get(Entity, eid)
    if self_entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    def _edge_outbound(rel: EntityRelationship, target: Entity) -> RelationshipEdge:
        return RelationshipEdge(
            id=str(rel.id),
            rel_type=rel.rel_type,
            direction="outbound",
            source_urn=self_entity.urn,
            source_name=self_entity.name,
            source_entity_type=self_entity.entity_type,
            target_urn=target.urn,
            target_name=target.name,
            target_entity_type=target.entity_type,
            confidence=_rel_confidence_label(rel.confidence),
        )

    def _edge_inbound(rel: EntityRelationship, source: Entity) -> RelationshipEdge:
        return RelationshipEdge(
            id=str(rel.id),
            rel_type=rel.rel_type,
            direction="inbound",
            source_urn=source.urn,
            source_name=source.name,
            source_entity_type=source.entity_type,
            target_urn=self_entity.urn,
            target_name=self_entity.name,
            target_entity_type=self_entity.entity_type,
            confidence=_rel_confidence_label(rel.confidence),
        )

    return BifrostEnvelope(
        data=EntityRelationshipsResponse(
            outbound=[_edge_outbound(r, t) for r, t in out_rows],
            inbound=[_edge_inbound(r, s) for r, s in in_rows],
        ),
    )


@router.get(
    "/by-id/{entity_id}/source",
    response_model=BifrostEnvelope[EntitySourceResponse],
)
async def entity_source(
    entity_id: str,
    session: DB,
    max_bytes: int = Query(200_000, ge=1, le=2_000_000),
) -> BifrostEnvelope[EntitySourceResponse]:
    """Return the source file referenced by the entity metadata, if the repo clone exists."""
    try:
        eid = uuid.UUID(entity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity id")

    entity = await session.get(Entity, eid)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    meta: dict[str, Any] = entity.entity_metadata or {}
    source_file = str(meta.get("source_file") or "").lstrip("/")
    if not source_file:
        raise HTTPException(
            status_code=404,
            detail="Entity has no source_file in metadata",
        )

    if entity.scope_repo_id is None:
        raise HTTPException(status_code=404, detail="Entity not scoped to a repo")

    repo_row = await session.get(RepoModel, entity.scope_repo_id)
    if repo_row is None or not repo_row.local_clone_path:
        raise HTTPException(
            status_code=404,
            detail="Repo clone not available on server. Run an extraction to populate it.",
        )

    from pathlib import Path

    full_path = Path(repo_row.local_clone_path) / source_file
    try:
        resolved = full_path.resolve()
        clone_resolved = Path(repo_row.local_clone_path).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="Could not resolve path")

    # Security: ensure the resolved path is within the clone dir
    if not str(resolved).startswith(str(clone_resolved)):
        raise HTTPException(status_code=400, detail="Path escapes clone root")

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {source_file}")

    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Read error: {exc}") from exc

    truncated = len(raw) > max_bytes
    content = raw[:max_bytes].decode("utf-8", errors="replace")
    return BifrostEnvelope(
        data=EntitySourceResponse(
            file_path=source_file,
            content=content,
            truncated=truncated,
        ),
    )


@router.get("/{entity_urn:path}", response_model=BifrostEnvelope[EntityDetail])
async def get_entity(entity_urn: str, repo: Repo) -> BifrostEnvelope[EntityDetail]:
    """Get an entity by URN."""
    entity = await repo.get_entity_by_urn(entity_urn)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")

    return BifrostEnvelope(
        data=EntityDetail(
            id=str(entity.id),
            urn=entity.urn,
            name=entity.name,
            qualified_name=entity.qualified_name,
            entity_type=entity.entity_type,
            knowledge_tier=entity.knowledge_tier,
            metadata=entity.entity_metadata,
            confidence=entity.confidence,
            confidence_level=entity.confidence_level,
            freshness_state=entity.freshness_state,
            source_basis=entity.source_basis,
            repo_id=str(entity.scope_repo_id) if entity.scope_repo_id else None,
            domain_id=str(entity.scope_domain_id) if entity.scope_domain_id else None,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        ),
        # Trust contract (S10): mirror this entity's confidence + freshness
        # into the uniform `meta` location alongside the data body.
        meta=EnvelopeMeta(
            confidence=entity.confidence_level,
            freshness=entity.freshness_state,
            data_source="extraction",
        ),
    )
