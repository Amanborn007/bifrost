"""Stage 6 — GraphRAG-style community detection + summarization.

Louvain-clusters the entity graph into communities, picks
representatives per community, generates a short LLM summary, and
stores everything in `entity_communities`.

Global queries (the kind where a user asks about a whole domain rather
than a specific entity) retrieve these summaries via centroid cosine
similarity instead of per-entity vector search. That's the retrieval
shape vanilla RAG is worst at — communities are the fix.

Public entry points:
    rebuild_communities(session_factory, scope_repo_id=None)
        Cluster + persist. Safe to run repeatedly; wipes prior
        communities for the given scope before inserting new ones.

    summarize_communities(session_factory, limit=None)
        For communities whose `summary` is NULL, call Claude CLI with
        the representative entities + their edges; write the summary.

    search_communities(session, question, k=5)
        Return top-K communities by centroid-cosine to the question's
        embedding. Used by global-query routing (Stage 6c).
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import networkx as nx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient
from bifrost.storage.models import (
    Entity,
    EntityCommunity,
    EntityRelationship,
    Repo,
)

logger = logging.getLogger(__name__)


# Communities below this size are too small to be useful as a retrieval
# unit — they get dissolved back to unclustered (community_id=NULL).
_MIN_COMMUNITY_SIZE = 3
# Above this we cap representatives; the summary doesn't need all of them.
_REPS_PER_COMMUNITY = 8


@dataclass
class ClusterStats:
    scope_repo_id: str | None
    entities_scanned: int
    edges_scanned: int
    communities_created: int
    communities_dropped_too_small: int
    entities_assigned: int
    entities_orphaned: int  # no edges → no community

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_repo_id": self.scope_repo_id,
            "entities_scanned": self.entities_scanned,
            "edges_scanned": self.edges_scanned,
            "communities_created": self.communities_created,
            "communities_dropped_too_small": self.communities_dropped_too_small,
            "entities_assigned": self.entities_assigned,
            "entities_orphaned": self.entities_orphaned,
        }


# ── 1. Clustering ────────────────────────────────────────────────────


async def rebuild_communities(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope_repo_id: str | None = None,
    resolution: float = 1.0,
) -> ClusterStats:
    """Cluster the entity graph and persist communities.

    When ``scope_repo_id`` is set, only that repo's entities are
    clustered (intra-repo communities). When None, the whole graph —
    including cross-repo edges — is clustered together. Cross-repo
    clustering is the one that powers org-wide "how does X work?"
    questions.
    """
    async with session_factory() as session:
        # 1. Pull entity nodes + their edges for the scope.
        ent_stmt = select(
            Entity.id,
            Entity.urn,
            Entity.name,
            Entity.entity_type,
            Entity.scope_repo_id,
            Entity.embedding,
        )
        if scope_repo_id:
            ent_stmt = ent_stmt.where(Entity.scope_repo_id == _uuid.UUID(scope_repo_id))
        ent_rows = (await session.execute(ent_stmt)).all()
        if not ent_rows:
            return ClusterStats(
                scope_repo_id=scope_repo_id,
                entities_scanned=0,
                edges_scanned=0,
                communities_created=0,
                communities_dropped_too_small=0,
                entities_assigned=0,
                entities_orphaned=0,
            )

        ent_ids = {eid for eid, *_ in ent_rows}
        ent_by_id: dict[_uuid.UUID, dict[str, Any]] = {}
        for eid, urn, name, etype, repo_id, emb in ent_rows:
            ent_by_id[eid] = {
                "id": eid,
                "urn": urn,
                "name": name,
                "type": etype,
                "repo_id": repo_id,
                "embedding": emb,
            }

        edge_stmt = select(
            EntityRelationship.source_id,
            EntityRelationship.target_id,
        )
        if scope_repo_id:
            edge_stmt = edge_stmt.where(
                EntityRelationship.source_id.in_(ent_ids),
                EntityRelationship.target_id.in_(ent_ids),
            )
        edge_rows = (await session.execute(edge_stmt)).all()

        # 2. Build undirected graph for Louvain.
        graph = nx.Graph()
        graph.add_nodes_from(ent_ids)
        for src, tgt in edge_rows:
            if src in ent_ids and tgt in ent_ids and src != tgt:
                graph.add_edge(src, tgt)

        # 3. Run Louvain on each connected component with >= MIN_SIZE
        # nodes. Isolated nodes (no edges) stay unclustered — they're
        # useless as community members anyway.
        communities: list[set[_uuid.UUID]] = []
        for component in nx.connected_components(graph):
            if len(component) < _MIN_COMMUNITY_SIZE:
                continue
            sub = graph.subgraph(component)
            sub_communities = nx.algorithms.community.louvain_communities(
                sub,
                resolution=resolution,
                seed=42,
            )
            for c in sub_communities:
                if len(c) >= _MIN_COMMUNITY_SIZE:
                    communities.append(set(c))

        logger.info(
            "Louvain: %d entities, %d edges → %d communities",
            len(ent_ids),
            len(edge_rows),
            len(communities),
        )

        # ── Safety gate ──────────────────────────────────────────
        # If Louvain returned zero communities (transient failure or
        # extreme sparsity), DO NOT wipe existing communities. That
        # would destroy LLM summaries for no reason.
        if not communities:
            logger.warning(
                "rebuild_communities: Louvain returned 0 communities; "
                "leaving existing communities intact to preserve summaries."
            )
            return ClusterStats(
                scope_repo_id=scope_repo_id,
                entities_scanned=len(ent_ids),
                edges_scanned=len(edge_rows),
                communities_created=0,
                communities_dropped_too_small=0,
                entities_assigned=0,
                entities_orphaned=len(ent_ids),
            )

        # ── Preserve summaries across rebuilds ──────────────────
        # Load the existing communities in this scope so we can carry
        # over summaries + names onto the new Louvain output when the
        # membership overlap is strong (Jaccard ≥ 0.5). This is the
        # difference between rebuild = "graph snapshot refresh" vs.
        # "nuke $2-$5 of LLM summary work every run".
        old_scope_filter = (
            EntityCommunity.scope_repo_id == _uuid.UUID(scope_repo_id)
            if scope_repo_id
            else EntityCommunity.scope_repo_id.is_(None)
        )
        old_comms_rows = (
            await session.execute(
                select(
                    EntityCommunity.id,
                    EntityCommunity.name,
                    EntityCommunity.summary,
                    EntityCommunity.representative_entity_ids,
                ).where(old_scope_filter)
            )
        ).all()
        # Collect each old community's full member set from entities.
        old_member_sets: list[tuple[_uuid.UUID, str, str | None, set[_uuid.UUID]]] = []
        for oc_id, oc_name, oc_summary, _reps in old_comms_rows:
            member_stmt = select(Entity.id).where(Entity.community_id == oc_id)
            members_ = {row[0] for row in (await session.execute(member_stmt)).all()}
            old_member_sets.append((oc_id, oc_name, oc_summary, members_))

        def _best_match(new_members: set[_uuid.UUID]) -> tuple[str, str | None] | None:
            """Return (name, summary) of the best-matching old community
            when Jaccard overlap ≥ 0.5; else None."""
            best_jac = 0.0
            best: tuple[str, str | None] | None = None
            for _oc_id, name_, summary_, old_members in old_member_sets:
                if not old_members:
                    continue
                inter = len(new_members & old_members)
                union = len(new_members | old_members)
                if union == 0:
                    continue
                jac = inter / union
                if jac > best_jac:
                    best_jac = jac
                    best = (name_, summary_)
            return best if best_jac >= 0.5 else None

        # Wipe old communities AFTER we've read them; FK on Entity is
        # ON DELETE SET NULL so member links go to null automatically.
        await session.execute(delete(EntityCommunity).where(old_scope_filter))
        # Also explicitly null out community_id on every in-scope entity
        # in case FK wasn't cascading.
        await session.execute(
            update(Entity).where(Entity.id.in_(ent_ids)).values(community_id=None)
        )

        # 5. Insert new communities + assign entities.
        created = 0
        assigned = 0
        preserved = 0
        for members in communities:
            # Pick representatives by degree within the cluster.
            member_degrees = [(mid, graph.degree(mid)) for mid in members]
            member_degrees.sort(key=lambda p: -p[1])
            reps = [mid for mid, _ in member_degrees[:_REPS_PER_COMMUNITY]]

            # Centroid embedding: mean of member embeddings that exist.
            embs = [
                ent_by_id[mid]["embedding"]
                for mid in members
                if ent_by_id[mid]["embedding"] is not None
            ]
            centroid = _mean_embedding(embs)

            # Preserve summary+name if this cluster is substantially the
            # same as an old one.
            carry = _best_match(members)
            if carry is not None:
                name, summary = carry
                preserved += 1
            else:
                top_name = ent_by_id[reps[0]]["name"] if reps else "community"
                name = f"{top_name}-cluster ({len(members)} entities)"
                summary = None  # filled by summarize_communities

            community = EntityCommunity(
                id=_uuid.uuid4(),
                scope_repo_id=_uuid.UUID(scope_repo_id) if scope_repo_id else None,
                name=name,
                summary=summary,
                entity_count=len(members),
                representative_entity_ids=list(reps),
                centroid=centroid,
                updated_at=datetime.now(UTC),
            )
            session.add(community)
            await session.flush()  # pull the id

            # Assign community_id on each member.
            await session.execute(
                update(Entity).where(Entity.id.in_(members)).values(community_id=community.id)
            )
            created += 1
            assigned += len(members)

        logger.info(
            "rebuild_communities: %d new (%d preserved summaries, %d need fresh summaries)",
            created,
            preserved,
            created - preserved,
        )

        # 6. Orphan count: entities with no community after assignment.
        orphan = (
            await session.execute(
                select(func.count(Entity.id))
                .where(Entity.id.in_(ent_ids))
                .where(Entity.community_id.is_(None))
            )
        ).scalar_one()

        await session.commit()

        return ClusterStats(
            scope_repo_id=scope_repo_id,
            entities_scanned=len(ent_ids),
            edges_scanned=len(edge_rows),
            communities_created=created,
            communities_dropped_too_small=0,
            entities_assigned=assigned,
            entities_orphaned=orphan,
        )


def _mean_embedding(vectors: list[Any]) -> list[float] | None:
    """Average a list of embedding vectors, preserving 384 dims.
    Returns None when no vectors (so DB stores NULL centroid)."""
    if not vectors:
        return None
    import numpy as np

    arr = np.array([list(v) for v in vectors if v is not None], dtype=float)
    if arr.size == 0:
        return None
    mean = arr.mean(axis=0)
    return mean.tolist()


# ── 2. Summarization ─────────────────────────────────────────────────


async def summarize_communities(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """For communities with NULL summary, generate one via Claude CLI.

    Returns {attempted, summarized, failed}.
    """
    settings = get_settings()
    client = ClaudeLlmClient(settings)

    stats = {"attempted": 0, "summarized": 0, "failed": 0}

    async with session_factory() as session:
        stmt = (
            select(EntityCommunity)
            .where(EntityCommunity.summary.is_(None))
            .order_by(EntityCommunity.entity_count.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        communities = (await session.execute(stmt)).scalars().all()

        for c in communities:
            stats["attempted"] += 1
            try:
                # Pull representative entities.
                rep_stmt = select(
                    Entity.urn,
                    Entity.name,
                    Entity.entity_type,
                    Entity.scope_repo_id,
                    Entity.entity_metadata,
                ).where(Entity.id.in_(c.representative_entity_ids))
                reps = (await session.execute(rep_stmt)).all()
                if not reps:
                    stats["failed"] += 1
                    continue

                repo_names: dict[_uuid.UUID, str] = {}
                if reps:
                    repo_stmt = select(Repo.id, Repo.name).where(
                        Repo.id.in_({r[3] for r in reps if r[3]})
                    )
                    repo_names = {
                        rid: name for rid, name in (await session.execute(repo_stmt)).all()
                    }

                summary, name = await _llm_summarize(
                    client,
                    community_name=c.name,
                    representatives=reps,
                    repo_names=repo_names,
                )
                c.summary = summary
                if name:
                    c.name = name
                c.updated_at = datetime.now(UTC)
                session.add(c)
                stats["summarized"] += 1
            except Exception:
                logger.exception(
                    "Failed to summarize community %s",
                    c.id,
                )
                stats["failed"] += 1

        await session.commit()

    return stats


_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "summary"],
    "properties": {
        "name": {
            "type": "string",
            "description": "Short, specific community label (3-8 words).",
        },
        "summary": {
            "type": "string",
            "description": (
                "2-4 sentence description of what this cluster of entities "
                "collectively represents — what domain/concern it covers, "
                "which services dominate, what kinds of flows it enables."
            ),
        },
    },
}


_SUMMARY_SYSTEM_PROMPT = """\
You are summarizing a cluster of related code entities (a "community"
in graph terms) so the group can be retrieved as a whole when a user
asks a BROAD architectural question.

Do NOT list every entity. Do NOT describe each one separately. Write a
single paragraph that captures what these entities COLLECTIVELY do.
Mention the dominant repo(s), the domain/concern (e.g., "order
fulfillment", "stock reservation", "tracking number
allocation"), and how the cluster fits into the bigger picture.

Name this cluster with a short, specific label — not "cluster 42",
something like "Order fulfillment orchestration" or "Shipment event
reporting".

Output ONLY the JSON matching the schema.
"""


async def _llm_summarize(
    client: ClaudeLlmClient,
    *,
    community_name: str,
    representatives: list,
    repo_names: dict,
) -> tuple[str, str | None]:
    """Call Claude CLI to produce (summary, new_name). May raise."""
    lines = [
        f"## Community (current label: {community_name})",
        "",
        "Representative entities (the most-connected members):",
        "",
    ]
    for urn, name, etype, repo_id, meta in representatives[:_REPS_PER_COMMUNITY]:
        repo = repo_names.get(repo_id, "?")
        purpose = ""
        if isinstance(meta, dict):
            purpose = str(meta.get("purpose", ""))[:150]
        lines.append(f"- [{etype}] `{urn}` {name} (repo: {repo}) — {purpose}")
    user_msg = "\n".join(lines) + ("\n\nProduce the JSON {name, summary} matching the schema.")

    try:
        completion = await client.call_with_json_schema(
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            user_message=user_msg,
            model=get_settings().agent_model,
            json_schema=_SUMMARY_SCHEMA,
            max_turns=1,
            timeout_override=60,
        )
    except ClaudeCliError as exc:
        raise RuntimeError(f"Summary LLM failed: {exc}") from exc

    data = completion.parsed or {}
    summary = str(data.get("summary", "")).strip()
    new_name = str(data.get("name", "")).strip() or None
    return summary, new_name


# ── 3. Retrieval (Stage 6c hook) ─────────────────────────────────────


async def search_communities(
    session: AsyncSession,
    question: str,
    *,
    repo_ids: list[str] | None = None,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Return top-K communities by centroid-cosine similarity.

    Used for global queries where the user asks about a whole domain;
    yields {id, name, summary, entity_count, representative_urns}.
    """
    try:
        from bifrost.research.embeddings import embed_one

        q_vec = embed_one(question)
    except Exception:
        logger.exception("community search: embedding failed")
        return []

    stmt = (
        select(EntityCommunity)
        .where(EntityCommunity.centroid.isnot(None))
        .where(EntityCommunity.summary.isnot(None))
        .order_by(EntityCommunity.centroid.cosine_distance(q_vec))
        .limit(k)
    )
    if repo_ids:
        # Include global (scope=null) + per-repo matches when repo_ids given
        scope_uuids = [_uuid.UUID(r) for r in repo_ids]
        stmt = stmt.where(
            or_(
                EntityCommunity.scope_repo_id.is_(None),
                EntityCommunity.scope_repo_id.in_(scope_uuids),
            )
        )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[dict[str, Any]] = []
    for c in rows:
        # Resolve representative URNs for display.
        rep_urns: list[str] = []
        if c.representative_entity_ids:
            urn_rows = (
                await session.execute(
                    select(Entity.urn).where(Entity.id.in_(c.representative_entity_ids))
                )
            ).all()
            rep_urns = [u for (u,) in urn_rows]
        out.append(
            {
                "id": str(c.id),
                "name": c.name,
                "summary": c.summary,
                "entity_count": c.entity_count,
                "representative_urns": rep_urns,
                "scope_repo_id": str(c.scope_repo_id) if c.scope_repo_id else None,
            }
        )
    return out
