"""Pre-flight cost estimates for handover generation.

We expose four estimators — one per scope kind — that mirror the
branching in :mod:`bifrost.collaboration.handover_generator`:

* Single repo => exactly one Sonnet call.
* Domain => one composer call + N parallel children when the repo
  count crosses ``_TREE_THRESHOLD``; otherwise one pooled call.
* Team => same as domain, joined through ``OrgRepo.squad``.
* Region => same as domain, joined through ``OrgRepo.region``.

The model is intentionally coarse — a linear function of entity count
with a couple of bands. We'll refine once we have
real telemetry from cost-tracker rows in ``handover_docs``. Until then,
*over*-estimating is fine: the goal is to catch a 30-call tree run that
would burn $3 before we commit, not to predict the cost to the cent.

The cost ceiling itself lives in
:attr:`bifrost.cli.config.BifrostSettings.handover_max_cost_usd`
(``BIFROST_HANDOVER_MAX_COST_USD`` env var, default $10). The route
layer enforces the ceiling; this module only *predicts*.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.collaboration.handover_generator import _TREE_THRESHOLD
from bifrost.storage.models import Domain, Entity, OrgRepo, Repo
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


# ── Heuristic constants ────────────────────────────────────────────
#
# A single Sonnet call for a handover is dominated by:
#   - System prompt (~1.5K tokens, fixed)
#   - Entity slice (capped at _TOTAL_ENTITY_CAP = 200 lines, ~3-5K tokens)
#   - Output (~3-6K tokens of structured JSON)
#
# At Sonnet 4.6 prices (~$3/$15 per MTok in/out), one call lands in the
# $0.04 to $0.10 range depending on entity count. We bucket by count to
# stay deliberately conservative — over-estimate is fine, under-estimate
# would silently let a costly run through.

_SONNET_LOW_COST_USD = 0.05  # ≤ 50 entities — small repo / pooled scope.
_SONNET_MID_COST_USD = 0.07  # 51-150 entities — typical mid-size repo.
_SONNET_HIGH_COST_USD = 0.10  # > 150 entities — entity cap kicks in, max prompt.

# The composer call that follows a tree decomposition reads N child
# bodies (truncated to _CHILD_BODY_CHAR_CAP each). With 50 children
# that's ~150KB of input — comparable to a high-entity repo prompt, so
# we charge it the high-band cost.
_COMPOSER_COST_USD = 0.10


@dataclass
class CostEstimate:
    """Result of an estimator. ``reasoning`` is operator-readable."""

    estimated_usd: float
    reasoning: str
    repos_to_generate: int
    will_use_tree: bool


# ── Helpers ────────────────────────────────────────────────────────


def _per_call_cost(entity_count: int) -> float:
    """Map a per-scope entity count to a single-Sonnet-call cost band.

    Conservative: rounds up. The real call cost varies with the actual
    prompt + output size; we approximate by entity volume because the
    entity slice is the only input that scales with scope size."""
    if entity_count > 150:
        return _SONNET_HIGH_COST_USD
    if entity_count > 50:
        return _SONNET_MID_COST_USD
    return _SONNET_LOW_COST_USD


async def _count_entities_for_repos(
    repo: EntityRepository,
    repo_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Return ``{repo_id: entity_count}`` for the given repo ids.

    A single grouped query — far cheaper than N round-trips when a
    domain has many repos. Repos with zero indexed entities are
    omitted from the result; callers should treat a missing key as 0."""
    if not repo_ids:
        return {}
    stmt = (
        select(Entity.scope_repo_id, func.count(Entity.id))
        .where(Entity.scope_repo_id.in_(repo_ids))
        .group_by(Entity.scope_repo_id)
    )
    rows = (await repo._session.execute(stmt)).all()
    return {rid: count for rid, count in rows if rid is not None}


# ── Repo scope ─────────────────────────────────────────────────────


async def estimate_repo_handover_cost(
    *,
    repo_id: uuid.UUID,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> CostEstimate:
    """Estimate the cost of generating a handover for a single repo.

    The repo path is always one Sonnet call, regardless of entity
    count — the prompt's bounded by ``_TOTAL_ENTITY_CAP``."""
    settings = settings or get_settings()  # noqa: F841 — kept for future use

    repo_row = await repo._session.get(Repo, repo_id)
    if repo_row is None:
        raise ValueError(f"repo {repo_id} not found")

    counts = await _count_entities_for_repos(repo, [repo_id])
    entity_count = counts.get(repo_id, 0)
    cost = _per_call_cost(entity_count)
    reasoning = (
        f"1 Sonnet call for repo `{repo_row.name}` "
        f"({entity_count} indexed entities) at ${cost:.2f}."
    )
    return CostEstimate(
        estimated_usd=round(cost, 4),
        reasoning=reasoning,
        repos_to_generate=1,
        will_use_tree=False,
    )


# ── Domain scope ───────────────────────────────────────────────────


async def estimate_domain_handover_cost(
    *,
    domain_id: uuid.UUID,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> CostEstimate:
    """Estimate the cost of generating a handover for a domain.

    Mirrors :func:`generate_domain_handover` branching: tree above
    ``_TREE_THRESHOLD`` repos, pooled single call otherwise."""
    settings = settings or get_settings()  # noqa: F841

    domain_row = await repo._session.get(Domain, domain_id)
    if domain_row is None:
        raise ValueError(f"domain {domain_id} not found")

    repo_stmt = select(Repo.id, Repo.name).where(Repo.domain_id == domain_id).order_by(Repo.name)
    rows = list((await repo._session.execute(repo_stmt)).all())
    repo_ids = [r[0] for r in rows]
    n = len(repo_ids)

    if n == 0:
        return CostEstimate(
            estimated_usd=0.0,
            reasoning=(
                f"Domain `{domain_row.name}` has no linked repos; "
                "generate writes a stub doc with no LLM call."
            ),
            repos_to_generate=0,
            will_use_tree=False,
        )

    counts = await _count_entities_for_repos(repo, repo_ids)
    will_use_tree = n > _TREE_THRESHOLD

    if will_use_tree:
        # N child Sonnet calls + 1 composer call.
        child_total = sum(_per_call_cost(counts.get(rid, 0)) for rid in repo_ids)
        cost = child_total + _COMPOSER_COST_USD
        reasoning = (
            f"Tree decomposition: {n} child Sonnet calls "
            f"(~${child_total:.2f}) + 1 composer call "
            f"(${_COMPOSER_COST_USD:.2f}) for domain `{domain_row.name}`."
        )
    else:
        # Pooled single call. Use the largest repo's cost band as a
        # conservative proxy — the prompt pools entities across repos
        # but is still bounded by ``_TOTAL_ENTITY_CAP``.
        pooled_count = sum(counts.values())
        cost = _per_call_cost(pooled_count)
        reasoning = (
            f"Single pooled Sonnet call for domain `{domain_row.name}` "
            f"({n} repos, {pooled_count} pooled entities) at ${cost:.2f}."
        )
    return CostEstimate(
        estimated_usd=round(cost, 4),
        reasoning=reasoning,
        repos_to_generate=n,
        will_use_tree=will_use_tree,
    )


# ── Team scope ───────────────────────────────────────────────


async def estimate_team_handover_cost(
    *,
    squad: str,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> CostEstimate:
    """Estimate the cost of generating a handover for a team
    (squad) scope. Mirrors :func:`generate_team_handover`."""
    settings = settings or get_settings()  # noqa: F841

    squad = (squad or "").strip()
    if not squad:
        raise ValueError("squad must be a non-empty string")

    join_stmt = (
        select(Repo.id, Repo.name)
        .join(OrgRepo, OrgRepo.registered_repo_id == Repo.id)
        .where(OrgRepo.squad == squad)
        .order_by(Repo.name)
    )
    rows = list((await repo._session.execute(join_stmt)).all())
    # Dedupe by id (multiple OrgRepo rows can map to the same Repo).
    seen: set[uuid.UUID] = set()
    repo_ids: list[uuid.UUID] = []
    for rid, _name in rows:
        if rid in seen:
            continue
        seen.add(rid)
        repo_ids.append(rid)
    n = len(repo_ids)

    if n == 0:
        return CostEstimate(
            estimated_usd=0.0,
            reasoning=(
                f"Squad `{squad}` has no registered repos; generate writes "
                "a stub doc with no LLM call."
            ),
            repos_to_generate=0,
            will_use_tree=False,
        )

    counts = await _count_entities_for_repos(repo, repo_ids)
    will_use_tree = n > _TREE_THRESHOLD

    if will_use_tree:
        child_total = sum(_per_call_cost(counts.get(rid, 0)) for rid in repo_ids)
        cost = child_total + _COMPOSER_COST_USD
        reasoning = (
            f"Tree decomposition: {n} child Sonnet calls "
            f"(~${child_total:.2f}) + 1 composer call "
            f"(${_COMPOSER_COST_USD:.2f}) for squad `{squad}`."
        )
    else:
        pooled_count = sum(counts.values())
        cost = _per_call_cost(pooled_count)
        reasoning = (
            f"Single pooled Sonnet call for squad `{squad}` "
            f"({n} repos, {pooled_count} pooled entities) at ${cost:.2f}."
        )
    return CostEstimate(
        estimated_usd=round(cost, 4),
        reasoning=reasoning,
        repos_to_generate=n,
        will_use_tree=will_use_tree,
    )


# ── Region scope ───────────────────────────────────────────────────


async def estimate_region_handover_cost(
    *,
    region: str,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> CostEstimate:
    """Estimate the cost of generating a handover for a region scope.

    Mirrors :func:`estimate_team_handover_cost` exactly — only
    the join column differs (``OrgRepo.region`` vs
    ``OrgRepo.squad``)."""
    settings = settings or get_settings()  # noqa: F841

    region = (region or "").strip()
    if not region:
        raise ValueError("region must be a non-empty string")

    join_stmt = (
        select(Repo.id, Repo.name)
        .join(OrgRepo, OrgRepo.registered_repo_id == Repo.id)
        .where(OrgRepo.region == region)
        .order_by(Repo.name)
    )
    rows = list((await repo._session.execute(join_stmt)).all())
    # Dedupe by id (multiple OrgRepo rows can map to the same Repo).
    seen: set[uuid.UUID] = set()
    repo_ids: list[uuid.UUID] = []
    for rid, _name in rows:
        if rid in seen:
            continue
        seen.add(rid)
        repo_ids.append(rid)
    n = len(repo_ids)

    if n == 0:
        return CostEstimate(
            estimated_usd=0.0,
            reasoning=(
                f"Region `{region}` has no registered repos; generate writes "
                "a stub doc with no LLM call."
            ),
            repos_to_generate=0,
            will_use_tree=False,
        )

    counts = await _count_entities_for_repos(repo, repo_ids)
    will_use_tree = n > _TREE_THRESHOLD

    if will_use_tree:
        child_total = sum(_per_call_cost(counts.get(rid, 0)) for rid in repo_ids)
        cost = child_total + _COMPOSER_COST_USD
        reasoning = (
            f"Tree decomposition: {n} child Sonnet calls "
            f"(~${child_total:.2f}) + 1 composer call "
            f"(${_COMPOSER_COST_USD:.2f}) for region `{region}`."
        )
    else:
        pooled_count = sum(counts.values())
        cost = _per_call_cost(pooled_count)
        reasoning = (
            f"Single pooled Sonnet call for region `{region}` "
            f"({n} repos, {pooled_count} pooled entities) at ${cost:.2f}."
        )
    return CostEstimate(
        estimated_usd=round(cost, 4),
        reasoning=reasoning,
        repos_to_generate=n,
        will_use_tree=will_use_tree,
    )
