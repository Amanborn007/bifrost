"""Repo status classifier — `deep_indexed` vs `manifest_only` vs `not_cloned`.

Factored out of ``coordinator.py`` (uncapped discovery + status
check). It classifies the FULL discovered set (not just the router's
top-K) so the
UI can show a live status chip per repo before agents fan out.

Status values:

    deep_indexed     — at least one component / flow / dependency in
                       the entity table; safe to fan-out a code agent.
    manifest_only    — registered + cloneable, but no extracted
                       entities yet; lives only as a service entity in
                       the master subgraph. Auto-extract is the
                       intended remediation.
    not_cloned       — registered, but the most recent freshness check
                       failed (network, auth, deleted upstream). The
                       coordinator drops these from `usable` already;
                       included here so the UI can surface why.
    auto_registered  — created mid-turn from the org catalog (a manifest
                       row that wasn't yet a `Repo` row). These are
                       always also manifest_only since extraction hasn't
                       run; tagged separately so the UI can show a
                       distinct chip.

The classifier is intentionally synchronous-data-only: it takes the
already-loaded freshness map + entity counts. The coordinator owns the
DB queries (and so do the tests).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from bifrost.research.freshness import RepoFreshness as RepoFreshState
from bifrost.storage.models import Repo

RepoStatus = Literal[
    "deep_indexed",
    "manifest_only",
    "not_cloned",
    "auto_registered",
]


@dataclass(frozen=True)
class RepoStatusInfo:
    """Per-repo classification + the signal that produced it."""

    repo_id: str
    repo_name: str
    status: RepoStatus
    code_entity_count: int
    clone_error: str | None = None
    auto_registered: bool = False


RepoStatusByRepo = dict[str, RepoStatusInfo]


def classify_repo_status(
    repos: Iterable[Repo],
    *,
    code_entity_counts: dict[str, int],
    fresh_by_id: dict[str, RepoFreshState] | None = None,
    auto_registered_ids: set[str] | None = None,
) -> RepoStatusByRepo:
    """Classify each repo as deep_indexed / manifest_only / not_cloned /
    auto_registered.

    Args:
        repos: Repo ORM rows discovered for the question.
        code_entity_counts: ``{str(repo_id): count_of_components_flows_deps}``.
            Repos absent from this map count as zero.
        fresh_by_id: ``{str(repo_id): RepoFreshness}`` — the freshness
            check result. Repos with ``error`` set are tagged
            ``not_cloned`` regardless of entity count. Pass ``None`` to
            skip clone-state classification (callers running before
            freshness is computed).
        auto_registered_ids: repo ids that were created mid-turn from
            the org catalog. These are tagged ``auto_registered``,
            taking precedence over manifest_only since the user wants to
            see "we just discovered this one".

    Returns:
        ``{str(repo_id): RepoStatusInfo}`` covering every repo in the
        input. Order matches the input iteration; iterate the returned
        dict if order matters (Python 3.7+ insertion order).
    """
    fresh_by_id = fresh_by_id or {}
    auto_registered_ids = auto_registered_ids or set()

    out: RepoStatusByRepo = {}
    for r in repos:
        rid = str(r.id)
        ec = int(code_entity_counts.get(rid, 0))
        fresh = fresh_by_id.get(rid)
        if fresh is not None and fresh.error:
            out[rid] = RepoStatusInfo(
                repo_id=rid,
                repo_name=r.name,
                status="not_cloned",
                code_entity_count=ec,
                clone_error=fresh.error,
                auto_registered=rid in auto_registered_ids,
            )
            continue
        if rid in auto_registered_ids:
            out[rid] = RepoStatusInfo(
                repo_id=rid,
                repo_name=r.name,
                status="auto_registered",
                code_entity_count=ec,
                auto_registered=True,
            )
            continue
        if ec > 0:
            out[rid] = RepoStatusInfo(
                repo_id=rid,
                repo_name=r.name,
                status="deep_indexed",
                code_entity_count=ec,
            )
        else:
            out[rid] = RepoStatusInfo(
                repo_id=rid,
                repo_name=r.name,
                status="manifest_only",
                code_entity_count=0,
            )
    return out


def status_summary(
    by_id: RepoStatusByRepo,
) -> dict[str, int]:
    """Count repos per status — handy for SSE summary events."""
    counts = {"deep_indexed": 0, "manifest_only": 0, "not_cloned": 0, "auto_registered": 0}
    for info in by_id.values():
        counts[info.status] = counts.get(info.status, 0) + 1
    return counts


def manifest_only_ids(by_id: RepoStatusByRepo) -> list[str]:
    """Repo ids tagged ``manifest_only`` or ``auto_registered`` — the
    targets for auto-extraction."""
    return [
        info.repo_id
        for info in by_id.values()
        if info.status in ("manifest_only", "auto_registered")
    ]


def deep_indexed_ids(by_id: RepoStatusByRepo) -> list[str]:
    return [info.repo_id for info in by_id.values() if info.status == "deep_indexed"]
