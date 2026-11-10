"""Hydrate the prior research bundle's URN list into ``EntitySnippet``s.

When a chat follow-up turn fires the EXTEND_BUNDLE narrow-fan-out path,
the synthesizer sees fresh per-repo Evidence blocks for the new fan-out
repos but only URN strings for the prior repos. That's enough to refer
to prior entities by name, but not enough for cross-repo flow reasoning
("does the new repo's retry path share state with the prior repo's
RetryQueue?") because the synthesizer can't see the prior entities'
purpose, source file, or relationships.

This module closes that gap by re-fetching entity rows + 1-hop edges
for the prior bundle's URNs straight from Postgres, packaged as the
same ``EntitySnippet`` shape the live fan-out produces. The synthesis
prompt builder (``coordinator._build_synthesis_user_message``) renders
them in a "## Prior research evidence" section, distinct from the
live fan-out so the model knows which entities are which.

Bounded two ways:
  * ``max_entities_per_repo`` (default 25) — same per-repo cap as the
    live fan-out, so a chat turn never blows the prompt budget.
  * ``max_repos`` (default 5) — never hydrate more prior repos than
    the live cap.

Hydration runs ONE round-trip per repo's edges (via the existing
``_attach_graph_edges`` helper) plus one entity SELECT, so the wall
cost is comparable to a single repo agent's setup time. We surface
the counts via the ``prior_subgraph_hydrated`` SSE event so the UI's
bundle-reuse banner can display them.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.research.repo_agent import EntitySnippet
from bifrost.storage.models import Entity, Repo

logger = logging.getLogger(__name__)


_DEFAULT_MAX_ENTITIES_PER_REPO = 25
_DEFAULT_MAX_REPOS = 5


@dataclass
class HydratedRepoSubgraph:
    """One prior repo's hydrated entities + their 1-hop edges.

    Shape mirrors what each live ``RepoBundle.entities`` carries so
    the synthesis renderer can treat both uniformly.
    """

    repo_id: str
    repo_name: str
    entities: list[EntitySnippet]

    @property
    def edge_count(self) -> int:
        # outbound edges only — inbound is the symmetric view of the
        # same physical relationship row, so counting both would
        # double-count.
        return sum(len(e.outbound) for e in self.entities)


@dataclass
class HydratedSubgraph:
    """The full hydration result: one entry per prior repo.

    Empty (``is_empty()`` true) when the prior bundle's URNs have all
    been deleted, or when no prior repo IDs were resolvable. Callers
    treat empty as "fall back to URN-string-only continuity" rather
    than as an error.
    """

    repos: list[HydratedRepoSubgraph] = field(default_factory=list)
    total_entities: int = 0
    total_edges: int = 0
    missing_urns: list[str] = field(default_factory=list)  # URNs that no longer exist

    def is_empty(self) -> bool:
        return self.total_entities == 0

    def repo_names(self) -> list[str]:
        return [r.repo_name for r in self.repos]


async def hydrate_prior_subgraph(
    *,
    urns: list[str],
    session_factory: async_sessionmaker[AsyncSession],
    max_entities_per_repo: int = _DEFAULT_MAX_ENTITIES_PER_REPO,
    max_repos: int = _DEFAULT_MAX_REPOS,
) -> HydratedSubgraph:
    """Re-fetch the prior bundle's entities + edges from Postgres.

    Pipeline:
      1. SELECT the entity rows whose ``urn`` is in the input list.
         URNs that don't match (entity deleted upstream) are recorded
         in ``missing_urns`` and skipped.
      2. SELECT the corresponding repo names so the result can be
         labeled by repo.
      3. Convert to ``EntitySnippet`` via the same helper the live
         path uses (``coordinator._rows_to_snippets``).
      4. Attach 1-hop relationships via ``coordinator._attach_graph_edges``.
      5. Group by repo, cap to ``max_entities_per_repo`` per repo and
         ``max_repos`` total, sort larger repos first so synthesis
         sees the most-connected repos first.

    The helpers are imported lazily to avoid a circular import with
    coordinator.py (which now imports from this module). A future
    refactor can move them into their own shared module.
    """
    out = HydratedSubgraph()
    if not urns:
        return out

    # Cap the input — guards against a pathologically huge prior bundle.
    capped_urns = list(dict.fromkeys(urns))[:200]  # de-dup + truncate

    async with session_factory() as session:
        ent_rows = list(
            (await session.execute(select(Entity).where(Entity.urn.in_(capped_urns))))
            .scalars()
            .all()
        )

        if not ent_rows:
            out.missing_urns = list(capped_urns)
            return out

        found_urns = {e.urn for e in ent_rows}
        out.missing_urns = [u for u in capped_urns if u not in found_urns]

        repo_ids: set[_uuid.UUID] = {
            e.scope_repo_id for e in ent_rows if e.scope_repo_id is not None
        }
        repo_name_by_id: dict[str, str] = {}
        if repo_ids:
            repo_rows = (
                await session.execute(select(Repo.id, Repo.name).where(Repo.id.in_(repo_ids)))
            ).all()
            for rid, rname in repo_rows:
                repo_name_by_id[str(rid)] = rname

        from bifrost.research.coordinator import (
            _attach_graph_edges,
            _rows_to_snippets,
        )

        snips = _rows_to_snippets(ent_rows)
        snippet_repo_id: dict[str, str | None] = {
            s.id: (str(e.scope_repo_id) if e.scope_repo_id else None)
            for s, e in zip(snips, ent_rows, strict=False)
        }
        snips = await _attach_graph_edges(session, snips)

    per_repo: dict[str, list[EntitySnippet]] = {}
    for s in snips:
        rid = snippet_repo_id.get(s.id) or "_orphan"
        per_repo.setdefault(rid, []).append(s)

    # Largest repos first — most connected = most useful evidence.
    ordered = sorted(
        per_repo.items(),
        key=lambda kv: -len(kv[1]),
    )

    for rid, ent_list in ordered[:max_repos]:
        capped = ent_list[:max_entities_per_repo]
        rname = repo_name_by_id.get(rid, "(unknown repo)") if rid != "_orphan" else "(orphaned)"
        sub = HydratedRepoSubgraph(repo_id=rid, repo_name=rname, entities=capped)
        out.repos.append(sub)
        out.total_entities += len(capped)
        out.total_edges += sub.edge_count

    if out.missing_urns:
        logger.debug(
            "subgraph_hydrate: %d/%d prior URNs no longer exist",
            len(out.missing_urns),
            len(capped_urns),
        )

    return out


def hydrated_summary(subgraph: HydratedSubgraph) -> dict[str, Any]:
    """Compact summary for the ``prior_subgraph_hydrated`` SSE event."""
    return {
        "total_entities": subgraph.total_entities,
        "total_edges": subgraph.total_edges,
        "repo_count": len(subgraph.repos),
        "repo_names": subgraph.repo_names(),
        "missing_urns": len(subgraph.missing_urns),
    }
