"""POST /api/v1/dependencies/blast-radius — standalone blast-radius API.

S6 (dependency-impact). The graph-walk engine
(``research.graph_retrieval.expand_subgraph``) already powers the chat
"what breaks if I change X" answer, but it had no clean REST surface —
programmatic callers and the external-agent API (PRD §19.2) could only
reach it through a chat turn. This route exposes the existing engine
directly: resolve a seed entity by URN, walk the dependency graph in the
requested direction, and return nodes + edges + confidence buckets in the
standard envelope.

No new graph algorithm — this is glue + serialization over
``expand_subgraph``. Headline confidence tracks the **weakest path** on
the walk (the bottleneck ``min_path_confidence``), matching the
project rule that synthesis must never out-claim its weakest evidence.
"""

from __future__ import annotations

from collections import deque
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from bifrost.api.deps import Repo
from bifrost.api.schemas.envelope import BifrostEnvelope, EnvelopeMeta
from bifrost.research.graph_retrieval import Subgraph, SubgraphNode, expand_subgraph

router = APIRouter(prefix="/dependencies", tags=["dependencies"])

DirectionLiteral = Literal["downstream", "upstream", "both"]

# Metadata keys that, if present on an entity, carry a 1-based source line.
# Extraction pipelines vary; we probe a few common spellings and surface the
# first that resolves so the reason string can cite ``file:line``.
_LINE_KEYS = ("line", "line_start", "start_line", "line_number", "lineno")


class BlastRadiusRequest(BaseModel):
    """Body of POST /api/v1/dependencies/blast-radius."""

    urn: str = Field(..., min_length=1, description="Seed entity URN to walk from")
    direction: DirectionLiteral = Field(
        default="downstream",
        description="downstream = what this entity affects (callees); "
        "upstream = what affects this entity (callers); both = bidirectional",
    )
    depth: int = Field(default=2, ge=1, le=5, description="BFS hop depth")
    confidence_floor: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Drop edges below this confidence before expanding",
    )


class BlastNode(BaseModel):
    id: str
    urn: str
    name: str
    entity_type: str
    repo_id: str
    distance_from_seed: int
    is_seed: bool
    min_path_confidence: float
    # Code grounding — surfaced so the UI can show exactly where the
    # entity lives without a second round-trip.
    source_file: str | None = None
    line: int | None = None
    qualified_name: str | None = None


class BlastEdge(BaseModel):
    source_urn: str
    target_urn: str
    rel_type: str
    is_cross_repo: bool
    confidence: float
    direction_from_seed: str | None = None


class BlastHop(BaseModel):
    """One edge on the seed→impacted path, grounded in code.

    The ``from``/``to`` endpoints are the connecting entities; the
    ``rel_type`` is the concrete relationship (calls / imports / …);
    the file/line pair is the source location of the *target* of this
    hop (the entity that would observe the change as it propagates).
    """

    from_urn: str
    from_name: str
    to_urn: str
    to_name: str
    rel_type: str
    confidence: float
    is_cross_repo: bool
    # Where the receiving end of this hop lives — the concrete code
    # location an engineer would open to see the call that breaks.
    to_source_file: str | None = None
    to_line: int | None = None
    to_qualified_name: str | None = None


class BlastImpact(BaseModel):
    """One impacted entity, ranked by directness, with its WHY.

    This is the granular answer: not "N services @ 88%" but
    "X breaks because seed is called by X.method() at file:line".
    """

    urn: str
    name: str
    entity_type: str
    repo_id: str
    is_cross_repo: bool
    distance_from_seed: int  # 1 = direct caller/callee (what actually breaks)
    # Weakest-link confidence along this path (bottleneck), so the
    # headline never out-claims its faintest hop.
    path_confidence: float
    source_file: str | None = None
    line: int | None = None
    qualified_name: str | None = None
    # The concrete edge chain seed → … → this entity.
    path: list[BlastHop]
    # Deterministic, code-grounded reason assembled from ``path`` — no LLM.
    reason: str


class BlastRadiusResponse(BaseModel):
    seed_urn: str
    seed_name: str
    seed_source_file: str | None = None
    direction: str
    depth: int
    impacted_count: int  # non-seed nodes reached
    # Ranked, code-grounded impacts (directness first). This is the
    # primary surface — ``nodes``/``edges`` remain for graph callers.
    impacts: list[BlastImpact]
    # How many impacts were collapsed into the long tail beyond
    # ``impacts`` (which is capped to the highest-impact few).
    tail_count: int
    nodes: list[BlastNode]
    edges: list[BlastEdge]
    confidence_buckets: dict[str, list[str]]  # high/medium/low node ids
    soft_cap_exceeded: bool
    stats: dict[str, int]


# Cap on the number of fully-explained impacts in the headline. The rest
# collapse into ``tail_count`` so the engineer sees the few that actually
# break, not a 151-row wall.
_IMPACT_CAP = 12


def _line_of(node: SubgraphNode | None) -> int | None:
    """Best-effort 1-based source line from entity metadata, if present."""
    if node is None:
        return None
    meta = node.metadata or {}
    for key in _LINE_KEYS:
        val = meta.get(key)
        if isinstance(val, int) and val > 0:
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return None


def _qualified(node: SubgraphNode | None) -> str | None:
    if node is None:
        return None
    qn = (node.metadata or {}).get("qualified_name")
    return str(qn) if qn else None


def _reconstruct_paths(subgraph: Subgraph, direction: str) -> dict[str, list]:
    """For each non-seed node, find the shortest seed→node edge chain.

    We BFS over the subgraph's own edges (no DB hits), following them in
    the direction the walk discovered them so the chain reads naturally:
      * downstream → follow source→target (seed calls X calls Y)
      * upstream   → follow target→source (Y calls X calls seed),
        but we emit the chain seed-first so the reason reads
        "seed is used by X which is used by Y".

    Returns ``{node_id: [SubgraphEdge, …]}`` — the edge list IS the path
    (len == hop count). Shortest path wins; ties broken by the
    higher-confidence (stronger-evidence) chain.
    """
    nodes_by_id = {n.id: n for n in subgraph.nodes}
    seeds = set(subgraph.seeds)
    if not seeds:
        return {}

    # Build adjacency keyed by "the next node as you walk away from a seed".
    # For downstream walks the seed is the source; for upstream the seed is
    # the target. ``both`` follows whichever endpoint advances the frontier.
    adj: dict[str, list] = {n.id: [] for n in subgraph.nodes}
    for e in subgraph.edges:
        if direction == "downstream":
            adj[e.source_id].append((e.target_id, e))
        elif direction == "upstream":
            # Walk caller→seed graph outward from the seed: from the seed's
            # perspective the neighbour is the source (the caller).
            adj[e.target_id].append((e.source_id, e))
        else:  # both — follow the edge's own direction tag when set
            if e.direction_from_seed == "upstream":
                adj[e.target_id].append((e.source_id, e))
            else:
                adj[e.source_id].append((e.target_id, e))

    # BFS from all seeds at once; first time we reach a node is its
    # shortest path. Keep the path's bottleneck confidence to break ties.
    best_path: dict[str, list] = {}
    best_conf: dict[str, float] = {}
    queue: deque[tuple[str, list, float]] = deque(
        (sid, [], 1.0) for sid in seeds if sid in nodes_by_id
    )
    visited_at_depth: dict[str, int] = {sid: 0 for sid in seeds}
    while queue:
        cur, path, conf = queue.popleft()
        for nxt, edge in adj.get(cur, []):
            if nxt in seeds:
                continue
            new_path = path + [edge]
            new_conf = min(conf, float(edge.confidence))
            depth = len(new_path)
            prev_depth = visited_at_depth.get(nxt)
            if prev_depth is None:
                visited_at_depth[nxt] = depth
                best_path[nxt] = new_path
                best_conf[nxt] = new_conf
                queue.append((nxt, new_path, new_conf))
            elif depth == prev_depth and new_conf > best_conf.get(nxt, 0.0):
                # Same length, stronger evidence — prefer it.
                best_path[nxt] = new_path
                best_conf[nxt] = new_conf
    return best_path


_REL_PHRASE = {
    "calls": "is called by",
    "imports": "is imported by",
    "uses": "is used by",
    "depends_on": "is depended on by",
    "publishes_to": "publishes to",
    "subscribes_to": "subscribes to",
    "reads_from": "reads from",
    "writes_to": "writes to",
}


def _build_impact(
    node: SubgraphNode,
    path_edges: list,
    nodes_by_id: dict[str, SubgraphNode],
) -> BlastImpact:
    """Assemble a code-grounded BlastImpact from a node + its edge path."""
    hops: list[BlastHop] = []
    bottleneck = 1.0
    cross = False
    for e in path_edges:
        tgt = nodes_by_id.get(e.target_id)
        hops.append(
            BlastHop(
                from_urn=e.source_urn,
                from_name=e.source_name,
                to_urn=e.target_urn,
                to_name=e.target_name,
                rel_type=e.rel_type,
                confidence=float(e.confidence),
                is_cross_repo=e.is_cross_repo,
                to_source_file=tgt.source_file if tgt else None,
                to_line=_line_of(tgt),
                to_qualified_name=_qualified(tgt),
            )
        )
        bottleneck = min(bottleneck, float(e.confidence))
        cross = cross or e.is_cross_repo

    reason = _phrase_reason(node, path_edges)
    return BlastImpact(
        urn=node.urn,
        name=node.name,
        entity_type=node.entity_type,
        repo_id=node.repo_id,
        is_cross_repo=cross,
        distance_from_seed=node.distance_from_seed,
        path_confidence=bottleneck if path_edges else node.min_path_confidence,
        source_file=node.source_file,
        line=_line_of(node),
        qualified_name=_qualified(node),
        path=hops,
        reason=reason,
    )


def _phrase_reason(node: SubgraphNode, path_edges: list) -> str:
    """Deterministic English from the edge chain — no LLM, fully grounded.

    1 hop  → "`X` is called by `seed` (calls) at app/x.py:42"
    n hops → "`seed` → `A` (calls) → `X` (uses); breaks at app/x.py:42"
    """
    if not path_edges:
        return (
            f"`{node.name}` is reachable from the seed but the connecting "
            f"path could not be reconstructed."
        )

    def _loc(n: SubgraphNode | None) -> str:
        if n is None or not n.source_file:
            return ""
        line = _line_of(n)
        return f"{n.source_file}:{line}" if line else n.source_file

    if len(path_edges) == 1:
        e = path_edges[0]
        verb = _REL_PHRASE.get(e.rel_type, e.rel_type)
        loc = _loc(node)
        at = f" at {loc}" if loc else ""
        return f"`{e.source_name}` {verb} `{e.target_name}` ({e.rel_type}){at}"

    # Multi-hop: render the arrow chain seed → … → node.
    parts = [f"`{path_edges[0].source_name}`"]
    for e in path_edges:
        parts.append(f"--{e.rel_type}--> `{e.target_name}`")
    chain = " ".join(parts)
    loc = _loc(node)
    tail = f"; surfaces at {loc}" if loc else ""
    return f"{chain}{tail}"


@router.post(
    "/blast-radius",
    response_model=BifrostEnvelope[BlastRadiusResponse],
    status_code=status.HTTP_200_OK,
)
async def blast_radius(
    body: BlastRadiusRequest,
    repo: Repo,
) -> BifrostEnvelope[BlastRadiusResponse]:
    """Walk the dependency graph from a seed entity and return the blast radius."""
    seed = await repo.get_entity_by_urn(body.urn)
    if seed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity not found: {body.urn}",
        )

    # Shared session (same pattern as /ask): EntityRepository wraps it.
    session = repo._session  # noqa: SLF001 -- intentional shared session

    subgraph = await expand_subgraph(
        session,
        [str(seed.id)],
        depth=body.depth,
        direction=body.direction,
        confidence_floor=body.confidence_floor,
    )

    nodes = [
        BlastNode(
            id=n.id,
            urn=n.urn,
            name=n.name,
            entity_type=n.entity_type,
            repo_id=n.repo_id,
            distance_from_seed=n.distance_from_seed,
            is_seed=n.is_seed,
            min_path_confidence=n.min_path_confidence,
            source_file=n.source_file,
            line=_line_of(n),
            qualified_name=_qualified(n),
        )
        for n in subgraph.nodes
    ]
    edges = [
        BlastEdge(
            source_urn=e.source_urn,
            target_urn=e.target_urn,
            rel_type=e.rel_type,
            is_cross_repo=e.is_cross_repo,
            confidence=e.confidence,
            direction_from_seed=e.direction_from_seed,
        )
        for e in subgraph.edges
    ]
    impacted = sum(1 for n in subgraph.nodes if not n.is_seed)

    # ── Granular reasoning: reconstruct the seed→node edge path for each
    # impacted entity, assemble a code-grounded WHY, and rank by
    # DIRECTNESS (1-hop callers/callees first — those are what actually
    # break), then by weakest-link confidence within a tier.
    nodes_by_id = {n.id: n for n in subgraph.nodes}
    paths = _reconstruct_paths(subgraph, body.direction)
    all_impacts: list[BlastImpact] = []
    for n in subgraph.nodes:
        if n.is_seed:
            continue
        all_impacts.append(_build_impact(n, paths.get(n.id, []), nodes_by_id))
    all_impacts.sort(key=lambda im: (im.distance_from_seed, -im.path_confidence, im.name))
    impacts = all_impacts[:_IMPACT_CAP]
    tail_count = max(0, len(all_impacts) - len(impacts))

    # Weakest-path confidence over the impacted set (bottleneck). Drives the
    # honest headline: a blast radius is only as trustworthy as its faintest link.
    weakest = min(
        (n.min_path_confidence for n in subgraph.nodes if not n.is_seed),
        default=1.0,
    )
    direct = sum(1 for im in all_impacts if im.distance_from_seed == 1)

    return BifrostEnvelope(
        data=BlastRadiusResponse(
            seed_urn=seed.urn,
            seed_name=seed.name,
            seed_source_file=(seed.entity_metadata or {}).get("source_file"),
            direction=body.direction,
            depth=body.depth,
            impacted_count=impacted,
            impacts=impacts,
            tail_count=tail_count,
            nodes=nodes,
            edges=edges,
            confidence_buckets=subgraph.confidence_buckets,
            soft_cap_exceeded=subgraph.soft_cap_exceeded,
            stats=subgraph.stats,
        ),
        meta=EnvelopeMeta(
            data_source="extraction",
            cache_status="live",
            evidence_summary=(
                f"{direct} direct + {impacted - direct} indirect impact(s) "
                f"via {body.direction} walk (depth {body.depth}); "
                f"weakest path confidence {weakest:.2f}"
            ),
        ),
    )
