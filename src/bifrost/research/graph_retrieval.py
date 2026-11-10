"""Graph-first retrieval for Bifrost research.

Today's retrieval is vector-top-K with an optional 1-hop edge attachment
bolt-on. This module flips it: vectors find SEEDS, then a bounded BFS
through the pre-extracted `EntityRelationship` graph yields the
**query subgraph** — the actual structural context the synthesizer needs
to reason over.

Core abstractions:
    Subgraph         — nodes + edges + seed set + stats for one query.
    SubgraphNode     — entity with repo, centrality, distance-from-seed.
    SubgraphEdge     — directed edge with resolved partner names/URNs.

Main entry points:
    find_seeds(session, question, repo_ids, k)     — vector/keyword hybrid
    expand_subgraph(session, seed_ids, depth, cap) — BFS, rel-type filtered
    build_query_subgraph(session, question, ...)   — one-shot convenience

Design rules:
  - Everything is bounded. No unbounded BFS, no unbounded N+1 queries.
  - Cross-repo edges are first-class: an edge whose source/target live in
    different repos is flagged and stats-tracked (the value prop).
  - The output is serializable and back-compat: `Subgraph.to_snippets()`
    returns `list[EntitySnippet]` so existing code agents don't break.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.research.repo_agent import EntitySnippet, GraphEdge
from bifrost.storage.models import Entity, EntityRelationship

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class SubgraphNode:
    """One entity in the query subgraph, enriched with graph-ranking info."""

    id: str
    urn: str
    name: str
    entity_type: str
    purpose: str
    source_file: str | None
    metadata: dict[str, object]
    repo_id: str
    # Ranking:
    distance_from_seed: int  # 0 = seed, 1/2/… = hop count
    degree: int  # inbound + outbound edge count (local to subgraph)
    is_seed: bool
    # Bottleneck confidence on the BFS path from
    # seed to this node — min(parent.min_path_confidence, edge.confidence).
    # Seeds are 1.0 by definition. Drives Stage 4 tier bucketing in
    # the synthesizer.
    min_path_confidence: float = 1.0


@dataclass
class SubgraphEdge:
    """One directed relationship within the subgraph, with resolved partners."""

    source_id: str
    target_id: str
    rel_type: str
    # Convenience fields so the synthesizer doesn't re-lookup:
    source_urn: str
    source_name: str
    source_repo_id: str
    target_urn: str
    target_name: str
    target_repo_id: str
    is_cross_repo: bool
    # Provenance — carried through from EntityRelationship. Surfaced in
    # the evidence block so synthesis can phrase claims per tier.
    confidence: float = 0.5
    source_type: str = "unknown"
    tier: int = 5
    # Direction relative to the BFS seed that
    # discovered this edge. ``downstream`` = source is in the frontier
    # (seed → callee chain); ``upstream`` = target is in the frontier
    # (caller → seed chain). ``None`` when expand_subgraph is run
    # bidirectionally without seed-relative tagging (legacy callers).
    direction_from_seed: str | None = None  # "upstream" | "downstream" | None


@dataclass
class Subgraph:
    """The connected structure pulled for one user question."""

    nodes: list[SubgraphNode] = field(default_factory=list)
    edges: list[SubgraphEdge] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)  # node ids
    stats: dict[str, int] = field(default_factory=dict)
    # True when ``expand_subgraph`` exceeded
    # ``soft_cap`` for this turn. The synthesizer surfaces it to the
    # user so they know their walk was wider than the soft cap (no
    # truncation — every node still made it back; the warning just
    # invites them to tighten settings if they want sharper results).
    soft_cap_exceeded: bool = False

    # ── Confidence-tier buckets ────────────────────────────────
    @property
    def confidence_buckets(self) -> dict[str, list[str]]:
        """Group node ids by their ``min_path_confidence``.

        ``high``    >= 0.85 (T1+T2 — code-verified or config-declared
                   all the way down the path from a seed)
        ``medium``  >= 0.55 (T3 — at least one metadata-inferred link)
        ``low``     <  0.55 (T4+T5 — docs / fuzzy-matched anywhere on
                   the path — the synthesizer must hedge claims drawn
                   only from these)

        Computed from ``nodes``; pure projection so no caching needed.
        """
        out: dict[str, list[str]] = {"high": [], "medium": [], "low": []}
        for n in self.nodes:
            if n.min_path_confidence >= 0.85:
                out["high"].append(n.id)
            elif n.min_path_confidence >= 0.55:
                out["medium"].append(n.id)
            else:
                out["low"].append(n.id)
        return out

    # ── Back-compat: old code agent expects EntitySnippet[] ─────────
    def to_snippets(self) -> list[EntitySnippet]:
        """Return nodes as EntitySnippets with edges attached as GraphEdges.

        Preserves the shape the repo_agent already consumes so this
        module is drop-in on the retrieval side.
        """
        return _nodes_to_snippets(self.nodes, self.edges)

    def nodes_in_repo(self, repo_id: str) -> list[SubgraphNode]:
        return [n for n in self.nodes if n.repo_id == repo_id]

    def cross_repo_edges(self) -> list[SubgraphEdge]:
        return [e for e in self.edges if e.is_cross_repo]


@dataclass
class RepoSlice:
    """One repo's view of the master subgraph.

    Produced by `extract_repo_slice` so each per-repo code agent gets
    (a) the subgraph nodes that live IN its repo,
    (b) boundary nodes — entities in OTHER repos connected by an edge,
    (c) the edges touching this repo (intra + cross).

    This is what makes agents cross-repo-aware: the StockService
    agent learns that `OrderActionProcessor --calls--> EventReporter`
    where `EventReporter` is in PriceCatalog. It can now verify that
    claim via Grep, or flag it if the class-is-missing.
    """

    repo_id: str
    nodes: list[SubgraphNode] = field(default_factory=list)
    boundary: list[SubgraphNode] = field(default_factory=list)
    edges: list[SubgraphEdge] = field(default_factory=list)

    def to_snippets(self) -> list[EntitySnippet]:
        """In-repo nodes + boundary nodes as EntitySnippets.

        The agent sees all of them as its working set, but the graph
        edges still encode which are "yours" (in-repo) and which are
        "reachable" (boundary) via the GraphEdge.other_urn field.
        """
        return _nodes_to_snippets(self.nodes + self.boundary, self.edges)

    def stats(self) -> dict[str, int]:
        return {
            "in_repo_nodes": len(self.nodes),
            "boundary_nodes": len(self.boundary),
            "total_edges": len(self.edges),
            "cross_repo_edges": sum(1 for e in self.edges if e.is_cross_repo),
        }


def extract_repo_slice(subgraph: Subgraph, repo_id: str) -> RepoSlice:
    """Project the master subgraph down to one repo's view."""
    in_repo = [n for n in subgraph.nodes if n.repo_id == repo_id]
    in_repo_ids = {n.id for n in in_repo}
    if not in_repo_ids:
        return RepoSlice(repo_id=repo_id)

    # Collect edges touching this repo.
    relevant_edges: list[SubgraphEdge] = []
    boundary_ids: set[str] = set()
    for e in subgraph.edges:
        touches = e.source_id in in_repo_ids or e.target_id in in_repo_ids
        if not touches:
            continue
        relevant_edges.append(e)
        if e.source_id not in in_repo_ids:
            boundary_ids.add(e.source_id)
        if e.target_id not in in_repo_ids:
            boundary_ids.add(e.target_id)

    boundary = [n for n in subgraph.nodes if n.id in boundary_ids]
    return RepoSlice(
        repo_id=repo_id,
        nodes=in_repo,
        boundary=boundary,
        edges=relevant_edges,
    )


def _nodes_to_snippets(
    nodes: list[SubgraphNode],
    edges: list[SubgraphEdge],
) -> list[EntitySnippet]:
    """Shared helper: build EntitySnippet[] from nodes + edges."""
    by_id: dict[str, SubgraphNode] = {n.id: n for n in nodes}
    out_edges: dict[str, list[GraphEdge]] = {n.id: [] for n in nodes}
    in_edges: dict[str, list[GraphEdge]] = {n.id: [] for n in nodes}
    for e in edges:
        if e.source_id in out_edges:
            out_edges[e.source_id].append(
                GraphEdge(
                    rel_type=e.rel_type,
                    other_name=e.target_name,
                    other_urn=e.target_urn,
                    other_type=_node_type(by_id.get(e.target_id)),
                    confidence=e.confidence,
                    source_type=e.source_type,
                    tier=e.tier,
                )
            )
        if e.target_id in in_edges:
            in_edges[e.target_id].append(
                GraphEdge(
                    rel_type=e.rel_type,
                    other_name=e.source_name,
                    other_urn=e.source_urn,
                    other_type=_node_type(by_id.get(e.source_id)),
                    confidence=e.confidence,
                    source_type=e.source_type,
                    tier=e.tier,
                )
            )
    snippets: list[EntitySnippet] = []
    for n in nodes:
        snippets.append(
            EntitySnippet(
                id=n.id,
                urn=n.urn,
                name=n.name,
                entity_type=n.entity_type,
                purpose=n.purpose,
                source_file=n.source_file,
                metadata=n.metadata,
                outbound=out_edges.get(n.id, []),
                inbound=in_edges.get(n.id, []),
            )
        )
    return snippets


def _node_type(n: SubgraphNode | None) -> str:
    return n.entity_type if n else ""


# ── Seed finding ─────────────────────────────────────────────────────


async def find_seeds(
    session: AsyncSession,
    question: str,
    *,
    repo_ids: list[str] | None = None,
    k: int = 8,
) -> list[str]:
    """Pick top-K entity ids to seed the BFS.

    Hybrid: vector cosine (if embeddings exist) ∪ keyword-in-name match
    ∪ high-degree fallback. De-duplicated, capped at k.

    The high-degree fallback exists because the graph is still sparse
    (~0.12 edges/entity today): purely vector-seeded BFS often lands on
    edge-less nodes and terminates immediately. Mixing in the most-
    connected entities per repo guarantees the walk has something to
    follow even when vector similarity misses them.
    """
    if not question.strip():
        return []

    # Three-tier seed budget. Each tier gets a GUARANTEED minimum so
    # graph-blind seeds can't crowd out connectivity-aware seeds.
    # Targets: 60% vector, 10% keyword, 30% high-degree (minimum 2).
    vector_target = max(1, k * 6 // 10)
    keyword_target = max(1, k * 1 // 10)
    degree_target = max(2, k - vector_target - keyword_target)

    seed_ids: list[str] = []
    seen: set[str] = set()

    def _add(ids: Iterable[str], budget: int) -> int:
        added = 0
        for sid in ids:
            if sid in seen:
                continue
            if added >= budget:
                break
            seen.add(sid)
            seed_ids.append(sid)
            added += 1
        return added

    # ── 1. Vector path ──────────────────────────────────────────
    try:
        from bifrost.research.embeddings import embed_one

        q_vec = embed_one(question)
        stmt = (
            select(Entity.id)
            .where(Entity.embedding.isnot(None))
            .order_by(Entity.embedding.cosine_distance(q_vec))
            .limit(vector_target * 3)
        )
        if repo_ids:
            stmt = stmt.where(Entity.scope_repo_id.in_([_uuid.UUID(r) for r in repo_ids]))
        rows = (await session.execute(stmt)).all()
        _add((str(eid) for (eid,) in rows), vector_target)
    except Exception:
        logger.exception("vector seed finding failed; falling back to keywords")

    # ── 2. Keyword path ─────────────────────────────────────────
    keywords = _extract_keywords(question)
    if keywords:
        clauses = [
            func.lower(Entity.name).like(f"%{kw.lower()}%") for kw in keywords if len(kw) >= 3
        ]
        if clauses:
            kstmt = (
                select(Entity.id)
                .where(or_(*clauses))
                .order_by(Entity.updated_at.desc())
                .limit(keyword_target * 4)
            )
            if repo_ids:
                kstmt = kstmt.where(Entity.scope_repo_id.in_([_uuid.UUID(r) for r in repo_ids]))
            rows = (await session.execute(kstmt)).all()
            _add((str(eid) for (eid,) in rows), keyword_target)

    # ── 3. Graph-degree tier ────────────────────────────────────
    # Top-connected entities in scope. Guarantees BFS expansion even
    # when vector/keyword seeds landed on edge-less nodes — critical
    # while the graph is still sparse (~0.1 edges/entity today).
    try:
        high_degree = await _find_high_degree_seeds(
            session,
            repo_ids=repo_ids,
            k=degree_target * 2,  # fetch extra to survive exclusion
            exclude=seen,
        )
        _add(iter(high_degree), degree_target)
    except Exception:
        logger.exception("high-degree seed path failed")

    return seed_ids[:k]


async def _find_high_degree_seeds(
    session: AsyncSession,
    *,
    repo_ids: list[str] | None,
    k: int,
    exclude: set[str],
) -> list[str]:
    """Top-K most-connected entities in scope by (inbound + outbound)."""
    if k <= 0:
        return []
    # Count edges per entity via two UNION queries then sum. For a small
    # DB this is fine; at scale replace with a materialized view.
    src_subq = select(
        EntityRelationship.source_id.label("id"),
        func.count().label("c"),
    ).group_by(EntityRelationship.source_id)
    tgt_subq = select(
        EntityRelationship.target_id.label("id"),
        func.count().label("c"),
    ).group_by(EntityRelationship.target_id)
    union = src_subq.union_all(tgt_subq).subquery()
    degree_stmt = (
        select(union.c.id, func.sum(union.c.c).label("deg"))
        .group_by(union.c.id)
        .order_by(func.sum(union.c.c).desc())
        .limit(k * 3)
    )
    rows = (await session.execute(degree_stmt)).all()
    if not rows:
        return []

    # Filter by repo scope + exclude already-picked seeds.
    candidate_ids = [rid for rid, _ in rows if str(rid) not in exclude]
    if not candidate_ids:
        return []
    if repo_ids:
        scope_uuids = [_uuid.UUID(r) for r in repo_ids]
        filter_stmt = select(Entity.id).where(
            Entity.id.in_(candidate_ids),
            Entity.scope_repo_id.in_(scope_uuids),
        )
        in_scope = {str(rid) for (rid,) in (await session.execute(filter_stmt)).all()}
    else:
        in_scope = {str(rid) for rid in candidate_ids}

    # Preserve descending-degree order.
    ordered: list[str] = []
    for rid, _ in rows:
        sid = str(rid)
        if sid in in_scope and sid not in exclude:
            ordered.append(sid)
            if len(ordered) >= k:
                break
    return ordered


def _extract_keywords(question: str) -> list[str]:
    """Cheap keyword extraction (identical policy to coordinator._extract_keywords)."""
    import re

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]+", question)
    stop = {
        "the",
        "and",
        "for",
        "with",
        "how",
        "what",
        "does",
        "this",
        "that",
        "are",
        "from",
        "into",
        "when",
        "where",
        "which",
        "why",
        "use",
        "used",
        "show",
        "tell",
        "can",
        "should",
        "will",
        "have",
        "has",
        "was",
        "were",
        "you",
        "your",
        "our",
        "its",
        "his",
        "her",
        "them",
        "their",
        "they",
        "there",
        "these",
        "those",
        "some",
        "any",
        "all",
        "one",
        "two",
    }
    return [t for t in tokens if len(t) > 2 and t.lower() not in stop]


# ── Subgraph expansion ──────────────────────────────────────────────


async def expand_subgraph(
    session: AsyncSession,
    seed_ids: list[str],
    *,
    depth: int = 2,
    max_nodes: int | None = None,  # legacy alias for soft_cap
    soft_cap: int | None = 200,
    rel_types: Iterable[str] | None = None,
    follow_cross_repo: bool = True,
    confidence_floor: float = 0.0,
    direction: str = "both",  # "upstream" | "downstream" | "both"
) -> Subgraph:
    """BFS outward from seeds.

    The formerly-hard ``max_nodes=80`` cap is now
    a SOFT cap. When the walk would exceed it, the walk continues
    and returns ALL nodes — but ``Subgraph.soft_cap_exceeded`` is set
    so the synthesizer / UI can warn the user. Pass ``soft_cap=None``
    to disable the warning entirely. Pass the legacy ``max_nodes``
    kwarg to map onto ``soft_cap`` for back-compat callers.

    New parameters:
      * ``confidence_floor`` — drop edges with
        ``confidence < confidence_floor`` BEFORE adding partner nodes.
        Default 0.0 keeps prior behaviour.
      * ``direction`` — ``"downstream"`` walks `source_id IN frontier`
        (seed → callee chain), ``"upstream"`` walks
        `target_id IN frontier` (caller → seed chain), ``"both"``
        walks bidirectionally (legacy default). Each new edge gets
        its `direction_from_seed` field set so the synthesizer can
        cite "upstream" vs "downstream" precisely.

    One DB query per hop. Worst-case query count is O(depth).
    """
    # Back-compat: if a caller passed ``max_nodes``, treat it as
    # ``soft_cap`` so existing call sites don't break mid-rollout.
    if max_nodes is not None:
        soft_cap = max_nodes

    if not seed_ids:
        return Subgraph(stats={"total_nodes": 0, "total_edges": 0})

    # Seed hydration: fetch seed entity rows first.
    seed_uuids = [_uuid.UUID(s) for s in seed_ids]
    seed_stmt = select(Entity).where(Entity.id.in_(seed_uuids))
    seed_rows = (await session.execute(seed_stmt)).scalars().all()

    nodes_by_id: dict[str, SubgraphNode] = {}
    for e in seed_rows:
        node = _entity_to_node(e, distance=0, is_seed=True)
        node.min_path_confidence = 1.0
        nodes_by_id[str(e.id)] = node

    edges: list[SubgraphEdge] = []
    edge_keys: set[tuple[str, str, str]] = set()  # (src, tgt, rel) dedupe

    frontier: set[str] = set(nodes_by_id.keys())
    rel_set = set(rel_types) if rel_types else None
    soft_cap_exceeded = False

    for hop in range(1, depth + 1):
        if not frontier:
            break

        frontier_uuids = [_uuid.UUID(fid) for fid in frontier]

        # Pull every edge touching the current frontier in one query.
        # ``direction`` slices the OR — downstream-only walks need
        # source_id IN frontier; upstream-only need target_id IN.
        if direction == "downstream":
            edge_filter = EntityRelationship.source_id.in_(frontier_uuids)
        elif direction == "upstream":
            edge_filter = EntityRelationship.target_id.in_(frontier_uuids)
        else:
            edge_filter = or_(
                EntityRelationship.source_id.in_(frontier_uuids),
                EntityRelationship.target_id.in_(frontier_uuids),
            )

        edge_stmt = select(EntityRelationship).where(edge_filter)
        rel_rows = (await session.execute(edge_stmt)).scalars().all()

        if rel_set:
            rel_rows = [r for r in rel_rows if r.rel_type in rel_set]
        # Drop edges below the user's confidence floor BEFORE
        # adding partner nodes. The walk shouldn't follow weak edges
        # at all if the user has set a strict floor.
        if confidence_floor > 0.0:
            rel_rows = [r for r in rel_rows if float(r.confidence or 0.0) >= confidence_floor]
        if not rel_rows:
            break

        # Collect partner ids we haven't hydrated yet, AND record
        # which frontier edge brought each partner in (so we can
        # set the partner's min_path_confidence + direction tag).
        new_ids: set[str] = set()
        # Map partner id → (best edge confidence, direction_from_seed)
        # so the partner inherits the strongest path's confidence
        # and a deterministic direction tag.
        partner_info: dict[str, tuple[float, str]] = {}
        for r in rel_rows:
            s = str(r.source_id)
            t = str(r.target_id)
            edge_conf = float(r.confidence or 0.0)
            # Determine which endpoint is the new partner relative
            # to the frontier — and use that to set direction.
            if s in frontier and t not in nodes_by_id:
                new_ids.add(t)
                # frontier --calls--> partner ⇒ partner is downstream
                cur = partner_info.get(t)
                if cur is None or edge_conf > cur[0]:
                    partner_info[t] = (edge_conf, "downstream")
            if t in frontier and s not in nodes_by_id:
                new_ids.add(s)
                # partner --calls--> frontier ⇒ partner is upstream
                cur = partner_info.get(s)
                if cur is None or edge_conf > cur[0]:
                    partner_info[s] = (edge_conf, "upstream")

        # Soft cap: if we'd exceed it, set the warning flag but DO
        # NOT truncate. The user gets every result; the UI surfaces
        # the warning so they can tighten settings if they want.
        if soft_cap is not None and len(nodes_by_id) + len(new_ids) > soft_cap:
            soft_cap_exceeded = True

        # Hydrate new partners.
        if new_ids:
            new_uuids = [_uuid.UUID(i) for i in new_ids]
            new_rows = (
                (await session.execute(select(Entity).where(Entity.id.in_(new_uuids))))
                .scalars()
                .all()
            )
            for e in new_rows:
                node = _entity_to_node(e, distance=hop, is_seed=False)
                # min_path_confidence inherits the bottleneck
                # confidence on the path from a seed.
                pid = str(e.id)
                edge_conf, _direction = partner_info.get(pid, (1.0, "downstream"))
                # Walk back to figure out the parent in nodes_by_id —
                # any frontier node that touches this partner via the
                # surviving rel_rows works. Cheapest correct
                # approximation: bottleneck = min(parent_min, edge_conf).
                # If multiple frontier parents exist, take the BEST
                # path (highest min) so we don't pessimise needlessly.
                best_min = 0.0
                for r in rel_rows:
                    s = str(r.source_id)
                    t = str(r.target_id)
                    other = None
                    if s == pid and t in nodes_by_id:
                        other = nodes_by_id[t]
                    elif t == pid and s in nodes_by_id:
                        other = nodes_by_id[s]
                    if other is not None:
                        cand = min(other.min_path_confidence, float(r.confidence or 0.0))
                        if cand > best_min:
                            best_min = cand
                node.min_path_confidence = best_min if best_min > 0.0 else edge_conf
                nodes_by_id[pid] = node

        # Record edges — only those where BOTH endpoints are now in the
        # subgraph (otherwise we're pointing into a void).
        for r in rel_rows:
            s = str(r.source_id)
            t = str(r.target_id)
            if s not in nodes_by_id or t not in nodes_by_id:
                continue
            key = (s, t, r.rel_type)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            ns, nt = nodes_by_id[s], nodes_by_id[t]
            is_cross = ns.repo_id != nt.repo_id
            if not follow_cross_repo and is_cross:
                continue
            # Pull provenance from the relationship row — the first
            # entry in source_basis is the canonical one (others are
            # corroborating sources).
            sb = r.source_basis or []
            first = sb[0] if isinstance(sb, list) and sb else {}
            edge_source_type = (
                str(first.get("source_type", "unknown")) if isinstance(first, dict) else "unknown"
            )
            edge_tier = (
                int(first.get("tier", 5))
                if isinstance(first, dict) and first.get("tier") is not None
                else 5
            )
            # Tag direction from seed. ``s`` was in the frontier
            # ⇒ this is a downstream edge (frontier --calls--> partner);
            # ``t`` was in the frontier ⇒ upstream
            # (partner --calls--> frontier). For "both" walks an edge
            # could legitimately be tagged either way — prefer
            # downstream (matches the rel_type's natural reading).
            if s in frontier:
                edge_dir = "downstream"
            elif t in frontier:
                edge_dir = "upstream"
            else:
                edge_dir = None  # neither endpoint in current frontier
            edges.append(
                SubgraphEdge(
                    source_id=s,
                    target_id=t,
                    rel_type=r.rel_type,
                    source_urn=ns.urn,
                    source_name=ns.name,
                    source_repo_id=ns.repo_id,
                    target_urn=nt.urn,
                    target_name=nt.name,
                    target_repo_id=nt.repo_id,
                    is_cross_repo=is_cross,
                    confidence=float(r.confidence or 0.5),
                    source_type=edge_source_type,
                    tier=edge_tier,
                    direction_from_seed=edge_dir,
                )
            )

        # Next frontier = nodes added this hop.
        frontier = {nid for nid, n in nodes_by_id.items() if n.distance_from_seed == hop}

    # Compute degree per node (local to subgraph).
    degree: dict[str, int] = {nid: 0 for nid in nodes_by_id}
    for e in edges:
        degree[e.source_id] = degree.get(e.source_id, 0) + 1
        degree[e.target_id] = degree.get(e.target_id, 0) + 1
    for nid, n in nodes_by_id.items():
        n.degree = degree.get(nid, 0)

    # Order nodes: seeds first, then by degree desc (most-connected first).
    nodes_sorted = sorted(
        nodes_by_id.values(),
        key=lambda n: (not n.is_seed, -n.degree, n.distance_from_seed),
    )

    cross_repo_count = sum(1 for e in edges if e.is_cross_repo)
    stats = {
        "total_nodes": len(nodes_sorted),
        "total_edges": len(edges),
        "cross_repo_edges": cross_repo_count,
        "seeds": len(seed_ids),
        "max_depth_reached": max((n.distance_from_seed for n in nodes_sorted), default=0),
    }
    return Subgraph(
        nodes=nodes_sorted,
        edges=edges,
        seeds=[nid for nid, n in nodes_by_id.items() if n.is_seed],
        stats=stats,
        soft_cap_exceeded=soft_cap_exceeded,
    )


def _entity_to_node(e: Entity, *, distance: int, is_seed: bool) -> SubgraphNode:
    meta = dict(e.entity_metadata or {})
    return SubgraphNode(
        id=str(e.id),
        urn=e.urn,
        name=e.name,
        entity_type=e.entity_type,
        purpose=str(meta.get("purpose", "") or "")[:300],
        source_file=meta.get("source_file"),
        metadata=meta,
        repo_id=str(e.scope_repo_id) if e.scope_repo_id else "",
        distance_from_seed=distance,
        degree=0,
        is_seed=is_seed,
    )


# ── One-shot convenience ─────────────────────────────────────────────


@dataclass
class FlowPath:
    """A directed chain of SubgraphNodes representing a likely flow.

    Produced by `find_flow_paths` via DFS from seed nodes along outbound
    edges. Used by synthesis to write the "End-to-end flow" section:
    each hop in a path becomes a numbered step with a URN citation.
    """

    nodes: list[SubgraphNode]  # ordered: nodes[0] is the seed
    edges: list[SubgraphEdge]  # len == len(nodes) - 1
    cross_repo_hops: int  # edges[i].is_cross_repo count
    span_repos: int  # distinct repo_ids in nodes

    def describe(self) -> str:
        """One-line arrow chain: `A (repoA) → B (repoB) → C (repoB)`."""
        parts: list[str] = []
        for i, n in enumerate(self.nodes):
            if i > 0:
                parts.append("→")
            parts.append(f"`{n.urn}`")
        return " ".join(parts)


def find_flow_paths(
    subgraph: Subgraph,
    *,
    max_paths: int = 5,
    max_depth: int = 4,
) -> list[FlowPath]:
    """DFS outward from each seed along outbound edges to find likely flows.

    Ranked by (cross_repo_hops DESC, span_repos DESC, length DESC) so
    the most architecturally-interesting paths win — those spanning
    service boundaries are the ones the synthesizer is most likely to
    need for the End-to-end flow narrative.
    """
    if not subgraph.seeds or not subgraph.nodes:
        return []

    # Build out-adjacency from the subgraph edges only (we stay inside
    # the retrieved subgraph — no fresh DB hits during synthesis).
    nodes_by_id = {n.id: n for n in subgraph.nodes}
    out_adj: dict[str, list[SubgraphEdge]] = {n.id: [] for n in subgraph.nodes}
    for e in subgraph.edges:
        if e.source_id in out_adj and e.target_id in nodes_by_id:
            out_adj[e.source_id].append(e)

    paths: list[FlowPath] = []

    def _dfs(current_id: str, node_path: list[str], edge_path: list[SubgraphEdge]) -> None:
        if len(node_path) - 1 >= max_depth:
            _emit(node_path, edge_path)
            return
        nexts = out_adj.get(current_id, [])
        if not nexts:
            # Sink — emit what we have if it's at least 2 nodes
            if len(node_path) >= 2:
                _emit(node_path, edge_path)
            return
        for e in nexts:
            if e.target_id in node_path:
                continue  # avoid cycles
            _dfs(
                e.target_id,
                node_path + [e.target_id],
                edge_path + [e],
            )
        # Also emit the current prefix as a path — lets us keep shorter
        # branches around (a 2-hop path through cross-repo edges may
        # beat a 4-hop intra-repo one).
        if len(node_path) >= 2:
            _emit(node_path, edge_path)

    def _emit(node_ids: list[str], edges: list[SubgraphEdge]) -> None:
        nodes = [nodes_by_id[nid] for nid in node_ids]
        xhops = sum(1 for e in edges if e.is_cross_repo)
        repos = len({n.repo_id for n in nodes if n.repo_id})
        paths.append(
            FlowPath(
                nodes=nodes,
                edges=edges,
                cross_repo_hops=xhops,
                span_repos=repos,
            )
        )

    # Prefer starting DFS from seeds IF they have outbound edges;
    # otherwise any node with outbound edges is fair game. Sparse graphs
    # often make the semantic seeds pure sinks (lots of inbound, no
    # outbound) which kills DFS silently.
    seed_set = {sid for sid in subgraph.seeds if out_adj.get(sid)}
    starters = (
        sorted(seed_set, key=lambda nid: -len(out_adj.get(nid, [])))
        if seed_set
        else sorted(
            (nid for nid, outs in out_adj.items() if outs),
            key=lambda nid: -len(out_adj[nid]),
        )[:12]  # cap to keep DFS bounded on dense graphs
    )
    for sid in starters:
        _dfs(sid, [sid], [])

    # Dedupe by exact node sequence, then rank by interestingness.
    seen_seqs: set[tuple[str, ...]] = set()
    uniq: list[FlowPath] = []
    for p in paths:
        seq = tuple(n.id for n in p.nodes)
        if seq in seen_seqs:
            continue
        seen_seqs.add(seq)
        uniq.append(p)

    uniq.sort(
        key=lambda p: (
            -p.cross_repo_hops,
            -p.span_repos,
            -len(p.nodes),
        )
    )
    return uniq[:max_paths]


async def build_query_subgraph(
    session: AsyncSession,
    question: str,
    *,
    repo_ids: list[str] | None = None,
    depth: int = 2,
    max_nodes: int | None = None,  # legacy alias for soft_cap
    soft_cap: int | None = 200,
    seeds: int = 8,
    confidence_floor: float = 0.0,
    direction: str = "both",
    rel_types: list[str] | None = None,
) -> Subgraph:
    """Seeds → expand → return. The default entry point for retrieval.

    Forwards the new ``confidence_floor``,
    ``direction``, and ``rel_types`` knobs into ``expand_subgraph``.
    The legacy ``max_nodes`` kwarg still works (mapped to
    ``soft_cap``) for back-compat callers; new callers should pass
    ``soft_cap`` explicitly. Defaults preserve prior behaviour
    (no confidence filter, both directions).
    """
    if max_nodes is not None:
        soft_cap = max_nodes
    seed_ids = await find_seeds(
        session,
        question,
        repo_ids=repo_ids,
        k=seeds,
    )
    if not seed_ids:
        return Subgraph(stats={"total_nodes": 0, "total_edges": 0})
    return await expand_subgraph(
        session,
        seed_ids,
        depth=depth,
        soft_cap=soft_cap,
        confidence_floor=confidence_floor,
        direction=direction,
        rel_types=rel_types,
    )
