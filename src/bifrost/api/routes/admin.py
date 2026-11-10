"""Admin endpoints — embedding backfill, diagnostic triggers, learning."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from bifrost.actions import agent_quality as _agent_quality
from bifrost.actions import learning as _learning
from bifrost.actions import learning_sync as _learning_sync
from bifrost.api.deps import DB, AdminOnly, get_session_factory
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.benchmarks.loader import is_sequenced
from bifrost.cli.config import get_settings
from bifrost.manifest.conventions import is_placeholder_team
from bifrost.manifest.graph_apply import apply_graph_plan, build_service_graph
from bifrost.research.communities import (
    ClusterStats,
    rebuild_communities,
    summarize_communities,
)
from bifrost.research.cross_repo_linker import LinkerStats, link_all
from bifrost.research.embed_backfill import (
    BackfillResult,
    backfill_confluence_embeddings,
    backfill_entity_embeddings,
    backfill_jira_embeddings,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[AdminOnly],
)


class EmbedResponse(BaseModel):
    table: str
    total_candidates: int
    embedded: int
    errors: int
    status: str = "complete"


def _to_resp(r: BackfillResult) -> EmbedResponse:
    return EmbedResponse(
        table=r.table,
        total_candidates=r.total_candidates,
        embedded=r.embedded,
        errors=r.errors,
    )


@router.post("/embed/entities", response_model=BifrostEnvelope[EmbedResponse])
async def embed_entities(
    force: bool = Query(False, description="Re-embed all, not just NULLs"),
    background: bool = Query(True, description="Run in background (default)"),
) -> BifrostEnvelope[EmbedResponse]:
    sf = get_session_factory(get_settings())
    if background:

        async def _run() -> None:
            try:
                r = await backfill_entity_embeddings(session_factory=sf, force=force)
                logger.info(
                    "entities embed: %d/%d (errors: %d)",
                    r.embedded,
                    r.total_candidates,
                    r.errors,
                )
            except Exception:
                logger.exception("entity embed backfill failed")

        asyncio.create_task(_run())
        return BifrostEnvelope(
            data=EmbedResponse(
                table="entities",
                total_candidates=0,
                embedded=0,
                errors=0,
                status="running_in_background",
            ),
        )

    r = await backfill_entity_embeddings(session_factory=sf, force=force)
    return BifrostEnvelope(data=_to_resp(r))


@router.post("/embed/confluence", response_model=BifrostEnvelope[EmbedResponse])
async def embed_confluence(
    force: bool = Query(False),
    background: bool = Query(True),
) -> BifrostEnvelope[EmbedResponse]:
    sf = get_session_factory(get_settings())
    if background:

        async def _run() -> None:
            try:
                await backfill_confluence_embeddings(session_factory=sf, force=force)
            except Exception:
                logger.exception("confluence embed backfill failed")

        asyncio.create_task(_run())
        return BifrostEnvelope(
            data=EmbedResponse(
                table="confluence_pages",
                total_candidates=0,
                embedded=0,
                errors=0,
                status="running_in_background",
            ),
        )
    r = await backfill_confluence_embeddings(session_factory=sf, force=force)
    return BifrostEnvelope(data=_to_resp(r))


@router.post("/embed/jira", response_model=BifrostEnvelope[EmbedResponse])
async def embed_jira(
    force: bool = Query(False),
    background: bool = Query(True),
) -> BifrostEnvelope[EmbedResponse]:
    sf = get_session_factory(get_settings())
    if background:

        async def _run() -> None:
            try:
                await backfill_jira_embeddings(session_factory=sf, force=force)
            except Exception:
                logger.exception("jira embed backfill failed")

        asyncio.create_task(_run())
        return BifrostEnvelope(
            data=EmbedResponse(
                table="jira_issues",
                total_candidates=0,
                embedded=0,
                errors=0,
                status="running_in_background",
            ),
        )
    r = await backfill_jira_embeddings(session_factory=sf, force=force)
    return BifrostEnvelope(data=_to_resp(r))


class CrossRepoLinkResponse(BaseModel):
    entities_scanned: int
    deps_scanned: int
    candidates_matched: int
    edges_created: int
    edges_skipped_duplicate: int
    ambiguous_skipped: int
    dry_run: bool
    status: str = "complete"


def _linker_to_resp(s: LinkerStats, dry_run: bool) -> CrossRepoLinkResponse:
    return CrossRepoLinkResponse(
        entities_scanned=s.entities_scanned,
        deps_scanned=s.deps_scanned,
        candidates_matched=s.candidates_matched,
        edges_created=s.edges_created,
        edges_skipped_duplicate=s.edges_skipped_duplicate,
        ambiguous_skipped=s.ambiguous_skipped,
        dry_run=dry_run,
    )


@router.post(
    "/link-cross-repo",
    response_model=BifrostEnvelope[CrossRepoLinkResponse],
)
async def link_cross_repo(
    dry_run: bool = Query(
        False,
        description="When true, report candidate edges without inserting",
    ),
) -> BifrostEnvelope[CrossRepoLinkResponse]:
    """Scan every entity's metadata for deps that resolve to entities in
    OTHER repos, write `cross_repo_depends_on` edges.

    Idempotent — safe to run after every extraction. The retrieval layer
    walks these edges automatically (see graph_retrieval.expand_subgraph
    with follow_cross_repo=True).
    """
    sf = get_session_factory(get_settings())
    stats = await link_all(sf, dry_run=dry_run)
    return BifrostEnvelope(data=_linker_to_resp(stats, dry_run=dry_run))


class RebuildCommunitiesResponse(BaseModel):
    scope_repo_id: str | None
    entities_scanned: int
    edges_scanned: int
    communities_created: int
    entities_assigned: int
    entities_orphaned: int
    summarize: dict[str, int] | None = None


@router.post(
    "/rebuild-communities",
    response_model=BifrostEnvelope[RebuildCommunitiesResponse],
)
async def rebuild_communities_endpoint(
    scope_repo_id: str | None = Query(
        None,
        description="If set, cluster only this repo's entities. Else "
        "cluster the whole cross-repo graph (recommended for org-wide "
        "global queries).",
    ),
    summarize: bool = Query(
        True,
        description="After clustering, generate LLM summaries for new communities.",
    ),
    summary_limit: int | None = Query(
        None,
        description="Cap summary generation at N communities (useful for first-run cost control).",
    ),
) -> BifrostEnvelope[RebuildCommunitiesResponse]:
    """Louvain-cluster the entity graph + (optionally) generate summaries.

    After this runs, `search_communities(question)` can retrieve
    relevant communities for broad/global queries that vector-top-K
    over individual entities would under-serve.
    """
    sf = get_session_factory(get_settings())
    stats: ClusterStats = await rebuild_communities(
        sf,
        scope_repo_id=scope_repo_id,
    )
    summary_stats = None
    if summarize and stats.communities_created > 0:
        summary_stats = await summarize_communities(
            sf,
            limit=summary_limit,
        )

    return BifrostEnvelope(
        data=RebuildCommunitiesResponse(
            scope_repo_id=stats.scope_repo_id,
            entities_scanned=stats.entities_scanned,
            edges_scanned=stats.edges_scanned,
            communities_created=stats.communities_created,
            entities_assigned=stats.entities_assigned,
            entities_orphaned=stats.entities_orphaned,
            summarize=summary_stats,
        )
    )


class ServiceGraphResponse(BaseModel):
    services: int
    edges: int
    domains: int
    unresolved: int
    edges_created: int
    repos_linked: int
    manifest_errors: int
    unresolved_top: list[tuple[str, int]] = []


@router.post(
    "/index-services",
    response_model=BifrostEnvelope[ServiceGraphResponse],
)
async def index_services_endpoint(
    include_catalog: bool = Query(
        True,
        description="Also read manifests of non-archived org catalog repos",
    ),
) -> BifrostEnvelope[ServiceGraphResponse]:
    """Build the service-level graph from repo manifests.

    Reads each registered repo's manifest (and, with ``include_catalog``,
    each catalog repo's) through the configured providers, upserts a
    service entity per repo and its team domain, and inserts the
    depends_on / calls_service / publishes_to / shares_topic edges the
    manifests declare. Idempotent.
    """
    settings = get_settings()
    sf = get_session_factory(settings)
    errors: dict[str, str] = {}
    plan = await build_service_graph(
        sf, include_catalog=include_catalog, settings=settings, errors=errors
    )
    applied = await apply_graph_plan(sf, plan)
    unresolved: dict[str, int] = {}
    for ref in plan.unresolved:
        unresolved[ref.target] = unresolved.get(ref.target, 0) + 1
    return BifrostEnvelope(
        data=ServiceGraphResponse(
            services=applied.services,
            edges=applied.edges,
            domains=applied.domains,
            unresolved=len(plan.unresolved),
            edges_created=applied.edges_created,
            repos_linked=applied.repos_linked,
            manifest_errors=len(errors),
            unresolved_top=sorted(unresolved.items(), key=lambda kv: -kv[1])[:10],
        )
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 4 — Continuous learning loop (observational + manual hooks)
# ─────────────────────────────────────────────────────────────────────


class ConfirmRequest(BaseModel):
    target_kind: Literal["entity", "relationship"]
    target_id: str
    new_confidence: float  # what we'd like it to be (0..1)
    reason: Literal[
        "task_verify_pass",
        "task_verify_fail",
        "correction_retraction",
        "re_extraction",
        "audit_confirmed",
        "audit_contradicted",
    ] = "audit_confirmed"
    task_id: str | None = None
    actor: str = "user"


@router.post(
    "/learning/confirm",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def confirm_target(
    body: ConfirmRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Record a positive signal for an entity or relationship.

    Governance still applies: one signal is not enough to upgrade — you
    need two distinct-task confirmations. The response's
    ``applied_new_confidence`` field is null when the delta was queued
    as pending.
    """
    try:
        result = await _learning.record_delta(
            session,
            target_kind=body.target_kind,
            target_id=body.target_id,
            proposed_confidence=Decimal(str(body.new_confidence)),
            reason=body.reason,
            task_id=body.task_id,
            actor=body.actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except IntegrityError as e:
        # A DB CHECK / FK violation (unknown reason value, task_id with no
        # matching task row, etc.) must surface as a clean 422, not a raw
        # asyncpg 500 that leaks schema internals. get_db rolls back the
        # poisoned session.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Delta rejected by a database constraint: {e.orig}",
        ) from e
    return BifrostEnvelope(data=result.to_dict())


@router.post(
    "/learning/contradict",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def contradict_target(
    body: ConfirmRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Same as /confirm but intended for negative signals. Applies
    immediately (downgrade governance)."""
    # Force a "downgrade-ish" reason if caller forgot to change it.
    reason = body.reason
    if reason in ("audit_confirmed", "task_verify_pass"):
        reason = "audit_contradicted"
    try:
        result = await _learning.record_delta(
            session,
            target_kind=body.target_kind,
            target_id=body.target_id,
            proposed_confidence=Decimal(str(body.new_confidence)),
            reason=reason,
            task_id=body.task_id,
            actor=body.actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except IntegrityError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Delta rejected by a database constraint: {e.orig}",
        ) from e
    return BifrostEnvelope(data=result.to_dict())


@router.get(
    "/learning/deltas",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_deltas(
    session: DB,
    task_id: str | None = Query(default=None),
    target_kind: Literal["entity", "relationship"] | None = Query(default=None),
    target_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    rows = await _learning.list_deltas(
        session,
        task_id=task_id,
        target_kind=target_kind,
        target_id=target_id,
        limit=limit,
    )
    return BifrostEnvelope(data=rows)


@router.get(
    "/learning/pending-upgrades",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def pending_upgrades(
    session: DB,
    limit: int = Query(default=50, ge=1, le=500),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    rows = await _learning.list_pending_upgrades(session, limit=limit)
    return BifrostEnvelope(data=rows)


@router.get(
    "/learning/judgements",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_judgements(
    session: DB,
    task_id: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    rows = await _learning.list_judgements(
        session,
        task_id=task_id,
        outcome=outcome,  # type: ignore[arg-type]
        limit=limit,
    )
    return BifrostEnvelope(data=rows)


@router.get(
    "/learning/stats",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def learning_stats(session: DB) -> BifrostEnvelope[dict[str, Any]]:
    return BifrostEnvelope(data=await _learning.learning_stats(session))


@router.get(
    "/learning/cache-stats",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def learning_cache_stats(
    session: DB,
    window_hours: int = Query(default=24, ge=1, le=24 * 365),
) -> BifrostEnvelope[dict[str, Any]]:
    """Knowledge base effectiveness — both lifetime + a recent window.

    Bifrost's agent_results table is a permanent knowledge base;
    rows live forever. This endpoint reports two views:

      ``lifetime`` — totals since the very first row. The real
                     "what does Bifrost know?" number.
      ``window``   — activity in the last ``window_hours``. Defaults
                     to 24h; useful for "is the loop active?".

    A "hit" is an ``agent_results`` row with ``was_cache_hit = TRUE``
    (Stage-2 semantic match attribution). A "miss" is a row written
    after running the agent fresh. ``saved_cost_usd`` is the running
    sum of ``research_runs.saved_cost_usd`` over runs that started in
    the window / over all time.

    ``window_hours`` capped at one year (8760) — for "lifetime" view,
    the UI passes 8760 and treats the response as the totals card.
    """
    import sqlalchemy as _sa

    by_kind_rows = (
        await session.execute(
            _sa.text(
                """
            SELECT
                agent_kind,
                SUM(CASE WHEN was_cache_hit THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN was_cache_hit THEN 0 ELSE 1 END) AS misses,
                SUM(CASE WHEN NOT was_cache_hit THEN cost_usd ELSE 0 END) AS spent_usd
            FROM agent_results
            WHERE created_at > NOW() - make_interval(hours => :h)
              AND status = 'success'
            GROUP BY agent_kind
            ORDER BY agent_kind ASC
            """,
            ),
            {"h": window_hours},
        )
    ).all()

    saved_total_row = (
        await session.execute(
            _sa.text(
                """
            SELECT
                COALESCE(SUM(saved_cost_usd), 0) AS saved,
                COALESCE(SUM(total_cost_usd), 0) AS total,
                COUNT(*)                          AS runs
            FROM research_runs
            WHERE started_at > NOW() - make_interval(hours => :h)
            """,
            ),
            {"h": window_hours},
        )
    ).first()

    # Lifetime — totals since the very first row. The real "what
    # does Bifrost know?" number, distinct from the activity window.
    lifetime_row = (
        await session.execute(
            _sa.text(
                """
            SELECT
                COUNT(*) FILTER (WHERE status = 'success' AND NOT was_cache_hit) AS misses,
                COUNT(*) FILTER (WHERE status = 'success' AND was_cache_hit)     AS hits,
                COUNT(DISTINCT (agent_kind, target_id)) FILTER (WHERE status = 'success') AS distinct_targets,
                COUNT(DISTINCT conversation_id) FILTER (WHERE status = 'success')        AS distinct_conversations,
                COALESCE(SUM(cost_usd) FILTER (WHERE NOT was_cache_hit), 0)              AS lifetime_spent
            FROM agent_results
            """,
            ),
        )
    ).first()
    lifetime_runs_row = (
        await session.execute(
            _sa.text(
                """
            SELECT
                COUNT(*) AS runs,
                COALESCE(SUM(total_cost_usd), 0) AS total,
                COALESCE(SUM(saved_cost_usd), 0) AS saved
            FROM research_runs
            """,
            ),
        )
    ).first()

    top_targets = (
        await session.execute(
            _sa.text(
                """
            SELECT
                agent_kind,
                target_id,
                SUM(CASE WHEN was_cache_hit THEN 1 ELSE 0 END) AS hits,
                COUNT(*) AS total_rows
            FROM agent_results
            WHERE created_at > NOW() - make_interval(hours => :h)
              AND status = 'success'
              AND was_cache_hit = TRUE
            GROUP BY agent_kind, target_id
            ORDER BY hits DESC
            LIMIT 10
            """,
            ),
            {"h": window_hours},
        )
    ).all()

    by_kind: dict[str, dict[str, Any]] = {}
    total_hits = 0
    total_misses = 0
    total_spent = 0.0
    for row in by_kind_rows:
        kind = row.agent_kind
        h = int(row.hits or 0)
        m = int(row.misses or 0)
        spent = float(row.spent_usd or 0.0)
        total_hits += h
        total_misses += m
        total_spent += spent
        denom = h + m
        by_kind[kind] = {
            "hits": h,
            "misses": m,
            "hit_rate": (h / denom) if denom else 0.0,
            "spent_usd": round(spent, 4),
        }

    overall_denom = total_hits + total_misses
    saved_total = float(saved_total_row.saved or 0.0) if saved_total_row else 0.0
    spent_total = float(saved_total_row.total or 0.0) if saved_total_row else 0.0
    runs_total = int(saved_total_row.runs or 0) if saved_total_row else 0

    # Lifetime numbers — the headline "what Bifrost knows" totals.
    lt_hits = int(lifetime_row.hits or 0) if lifetime_row else 0
    lt_misses = int(lifetime_row.misses or 0) if lifetime_row else 0
    lt_targets = int(lifetime_row.distinct_targets or 0) if lifetime_row else 0
    lt_convs = int(lifetime_row.distinct_conversations or 0) if lifetime_row else 0
    lt_spent_agents = float(lifetime_row.lifetime_spent or 0.0) if lifetime_row else 0.0
    lt_runs = int(lifetime_runs_row.runs or 0) if lifetime_runs_row else 0
    lt_runs_total_cost = float(lifetime_runs_row.total or 0.0) if lifetime_runs_row else 0.0
    lt_runs_saved = float(lifetime_runs_row.saved or 0.0) if lifetime_runs_row else 0.0
    lt_denom = lt_hits + lt_misses

    return BifrostEnvelope(
        data={
            "window_hours": window_hours,
            "lifetime": {
                "hits": lt_hits,
                "misses": lt_misses,
                "hit_rate": (lt_hits / lt_denom) if lt_denom else 0.0,
                "distinct_targets": lt_targets,
                "distinct_conversations": lt_convs,
                "agent_spent_usd": round(lt_spent_agents, 4),
                "runs": lt_runs,
                "runs_total_cost_usd": round(lt_runs_total_cost, 4),
                "runs_saved_cost_usd": round(lt_runs_saved, 4),
            },
            "totals": {
                "hits": total_hits,
                "misses": total_misses,
                "hit_rate": (total_hits / overall_denom if overall_denom else 0.0),
                "saved_cost_usd": round(saved_total, 4),
                "total_cost_usd": round(spent_total, 4),
                "runs": runs_total,
            },
            "by_kind": by_kind,
            "top_targets": [
                {
                    "agent_kind": r.agent_kind,
                    "target_id": r.target_id,
                    "hits": int(r.hits or 0),
                }
                for r in top_targets
            ],
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Learning weights sync — export/import portable confidence "weights"
# across Bifrost instances. Merge goes through the SAME governance path
# (learning.record_delta): noise floor, 2-confirmation upgrade gate, and
# immediate-downgrade all apply. Never a destructive overwrite.
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/learning/weights/export",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def export_learning_weights(
    session: DB,
    instance_id: str | None = Query(
        default=None,
        description="Identifier for THIS instance, stamped into the bundle.",
    ),
) -> BifrostEnvelope[dict[str, Any]]:
    """Export this instance's learned confidence weights as a portable,
    versioned bundle (keyed by stable URNs, not local row UUIDs)."""
    bundle = await _learning_sync.export_learning_weights(session, instance_id=instance_id)
    return BifrostEnvelope(data=bundle)


class ImportWeightsRequest(BaseModel):
    bundle: dict[str, Any]
    actor: str | None = None


@router.post(
    "/learning/weights/import",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def import_learning_weights(
    body: ImportWeightsRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Merge a peer instance's weights bundle through the governance path.

    Idempotent and non-destructive: imported peer signals stage as
    pending upgrades (never auto-promote without a local confirmation),
    downgrades apply immediately, and sub-noise-floor deltas no-op.
    Returns a per-direction merge report.
    """
    try:
        report = await _learning_sync.import_learning_weights(
            session, body.bundle, actor=body.actor
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except IntegrityError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Import rejected by a database constraint: {e.orig}",
        ) from e
    return BifrostEnvelope(data=report.to_dict())


# ─────────────────────────────────────────────────────────────────────
# Cleanup — remove placeholder squad domains created before the filter
# ─────────────────────────────────────────────────────────────────────


class CleanupPlaceholderResult(BaseModel):
    domains_removed: list[str]
    repos_unlinked: int
    ownership_nulled: int
    dry_run: bool


@router.post(
    "/cleanup/placeholder-squads",
    response_model=BifrostEnvelope[CleanupPlaceholderResult],
)
async def cleanup_placeholder_squads(
    session: DB,
    dry_run: bool = Query(default=True),
) -> BifrostEnvelope[CleanupPlaceholderResult]:
    """Remove auto-created Domain rows whose name is a template placeholder
    (e.g. ``your-squad-here``). Nulls ``repos.domain_id`` and
    ``repo_ownership.squad`` on affected rows.

    Defaults to ``dry_run=true`` so you can preview the blast radius.
    """
    import sqlalchemy as _sa

    # Find domain rows whose name is a placeholder
    rows = (
        await session.execute(_sa.text("SELECT id::text, name FROM domains ORDER BY name ASC"))
    ).all()
    bad_ids: list[str] = []
    bad_names: list[str] = []
    extra_placeholders = get_settings().conventions.placeholder_teams
    for did, name in rows:
        if is_placeholder_team(name, extra_placeholders):
            bad_ids.append(did)
            bad_names.append(name)

    if not bad_ids:
        return BifrostEnvelope(
            data=CleanupPlaceholderResult(
                domains_removed=[],
                repos_unlinked=0,
                ownership_nulled=0,
                dry_run=dry_run,
            )
        )

    repos_unlinked = int(
        (
            await session.execute(
                _sa.text("SELECT COUNT(*) FROM repos WHERE domain_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": bad_ids},
            )
        ).scalar_one()
    )
    ownership_nulled = int(
        (
            await session.execute(
                _sa.text(
                    """
                    SELECT COUNT(*) FROM repo_ownership
                    WHERE LOWER(COALESCE(squad, '')) = ANY(CAST(:names AS text[]))
                    """
                ),
                {"names": [n.lower() for n in bad_names]},
            )
        ).scalar_one()
    )

    if dry_run:
        return BifrostEnvelope(
            data=CleanupPlaceholderResult(
                domains_removed=bad_names,
                repos_unlinked=repos_unlinked,
                ownership_nulled=ownership_nulled,
                dry_run=True,
            )
        )

    # Apply
    await session.execute(
        _sa.text("UPDATE repos SET domain_id = NULL WHERE domain_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": bad_ids},
    )
    await session.execute(
        _sa.text(
            """
            UPDATE repo_ownership SET squad = NULL
            WHERE LOWER(COALESCE(squad, '')) = ANY(CAST(:names AS text[]))
            """
        ),
        {"names": [n.lower() for n in bad_names]},
    )
    await session.execute(
        _sa.text("DELETE FROM domains WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": bad_ids},
    )
    return BifrostEnvelope(
        data=CleanupPlaceholderResult(
            domains_removed=bad_names,
            repos_unlinked=repos_unlinked,
            ownership_nulled=ownership_nulled,
            dry_run=False,
        )
    )


# ─────────────────────────────────────────────────────────────────────
# Agent quality dashboard — aggregate drift signals
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/agent-quality",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def agent_quality_snapshot(
    session: DB,
    window_days: int = Query(default=30, ge=1, le=180),
    trend_window_days: int = Query(default=7, ge=1, le=60),
) -> BifrostEnvelope[dict[str, Any]]:
    """One-shot aggregation of agent-quality metrics.

    Bounds everything by the last ``window_days`` to keep old data
    from masking recent drift. ``trend_window_days`` isolates a
    smaller "recent" slice for side-by-side comparison.
    """
    snap = await _agent_quality.snapshot(
        session,
        window_days=window_days,
        trend_window_days=trend_window_days,
    )
    return BifrostEnvelope(data=snap.to_dict())


# ── Benchmarks (Tier 3) ──────────────────────────────────────────────────────
#
# Read-only endpoints powering the /admin/learning page's "Benchmarks"
# card. Writes happen via ``python -m bifrost.benchmarks --domain acme``
# (the runner is heavyweight and shouldn't run inside the API request
# loop). The UI surfaces averages + per-question detail across builds
# so a regression / improvement is visible at a glance.


@router.get(
    "/benchmarks/domains",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def benchmarks_list_domains(
    session: DB,
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """List every distinct benchmark domain in the DB so the UI's
    scoreboard switcher can render a button per domain dynamically.

    Returns each domain with its question count + most-recent run
    timestamp so the UI can sort by recency.
    """
    import sqlalchemy as _sa

    rows = (
        await session.execute(
            _sa.text(
                """
            SELECT bq.domain,
                   COUNT(DISTINCT bq.id)        AS n_questions,
                   COUNT(DISTINCT br.id)        AS n_runs,
                   MAX(br.started_at)           AS last_run_at
            FROM benchmark_questions bq
            LEFT JOIN benchmark_runs br ON br.question_id = bq.id
            GROUP BY bq.domain
            ORDER BY MAX(br.started_at) DESC NULLS LAST, bq.domain
            """,
            ),
        )
    ).all()
    return BifrostEnvelope(
        data=[
            {
                "domain": r.domain,
                "n_questions": int(r.n_questions or 0),
                "n_runs": int(r.n_runs or 0),
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "sequenced": is_sequenced(r.domain),
            }
            for r in rows
        ]
    )


@router.get(
    "/benchmarks/summary",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def benchmarks_summary(
    session: DB,
    domain: str = Query(default="acme"),
) -> BifrostEnvelope[dict[str, Any]]:
    """Aggregate benchmark stats per (build_label, run_kind).

    Returns a compact shape the UI scoreboard uses directly:

    .. code-block:: json

        {
          "domain": "acme",
          "n_questions": 10,
          "builds": [
            {
              "build_label": "baseline-run-1",
              "by_run_kind": {
                "A": {"n": 10, "avg_score": 0.71, "total_cost_usd": 1.34, "avg_cost": 0.134},
                "B": {"n": 10, "avg_score": 0.74, "total_cost_usd": 0.42, "avg_cost": 0.042},
                "C": {"n": 7,  "avg_score": 0.69, "total_cost_usd": 0.55, "avg_cost": 0.079}
              }
            }
          ]
        }
    """
    import sqlalchemy as _sa

    n_q = (
        await session.execute(
            _sa.text("SELECT COUNT(*) FROM benchmark_questions WHERE domain = :d"),
            {"d": domain},
        )
    ).scalar_one()

    rows = (
        await session.execute(
            _sa.text(
                """
            SELECT
              build_label,
              run_kind,
              COUNT(*)                                   AS n,
              AVG(score_total)                           AS avg_score,
              AVG(score_recall)                          AS avg_recall,
              AVG(score_precision)                       AS avg_precision,
              AVG(score_specificity)                     AS avg_specificity,
              SUM(cost_usd)                              AS total_cost,
              AVG(cost_usd)                              AS avg_cost,
              AVG(duration_ms)                           AS avg_duration_ms,
              SUM(agents_cache_hit)                      AS cache_hits,
              SUM(agents_dispatched)                     AS agents_total,
              SUM(edges_confirmed)                       AS confirmed,
              SUM(new_edges_inserted)                    AS new_edges
            FROM benchmark_runs br
            JOIN benchmark_questions bq ON bq.id = br.question_id
            WHERE bq.domain = :d
            GROUP BY build_label, run_kind
            ORDER BY MAX(br.started_at) DESC, build_label, run_kind
            """
            ),
            {"d": domain},
        )
    ).all()

    by_build: dict[str | None, dict[str, dict[str, Any]]] = {}
    for r in rows:
        bl = r.build_label
        if bl not in by_build:
            by_build[bl] = {}
        agents_total = int(r.agents_total or 0) or 1
        by_build[bl][r.run_kind] = {
            "n": int(r.n or 0),
            "avg_score": round(float(r.avg_score or 0.0), 3),
            "avg_recall": round(float(r.avg_recall or 0.0), 3),
            "avg_precision": round(float(r.avg_precision or 0.0), 3),
            "avg_specificity": round(float(r.avg_specificity or 0.0), 3),
            "total_cost_usd": round(float(r.total_cost or 0.0), 4),
            "avg_cost_usd": round(float(r.avg_cost or 0.0), 4),
            "avg_duration_ms": int(r.avg_duration_ms or 0),
            "cache_hit_rate": round(
                float(r.cache_hits or 0) / agents_total,
                3,
            ),
            "edges_confirmed": int(r.confirmed or 0),
            "new_edges_inserted": int(r.new_edges or 0),
        }

    builds = [{"build_label": bl, "by_run_kind": kinds} for bl, kinds in by_build.items()]
    return BifrostEnvelope(
        data={
            "domain": domain,
            "n_questions": int(n_q or 0),
            "builds": builds,
        }
    )


@router.get(
    "/benchmarks/questions",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def benchmarks_list_questions(
    session: DB,
    domain: str = Query(default="acme"),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """List the loaded ground-truth questions for ``domain``.

    Used by the UI to render the "questions in scope" panel and to let
    operators drill into per-question scoring history.
    """
    import sqlalchemy as _sa

    rows = (
        await session.execute(
            _sa.text(
                """
            SELECT id, question_key, question_text, paraphrase,
                   tier_floor, expects_diagram,
                   expected_format, max_words,
                   jsonb_array_length(expected_entities) AS n_entities,
                   jsonb_array_length(expected_key_facts) AS n_facts,
                   metadata
            FROM benchmark_questions
            WHERE domain = :d
            ORDER BY question_key
            """
            ),
            {"d": domain},
        )
    ).all()

    return BifrostEnvelope(
        data=[
            {
                "id": str(r.id),
                "question_key": r.question_key,
                "question_text": r.question_text,
                "paraphrase": r.paraphrase,
                "tier_floor": int(r.tier_floor),
                "expects_diagram": bool(r.expects_diagram),
                "expected_format": r.expected_format,
                "max_words": r.max_words,
                "n_entities": int(r.n_entities or 0),
                "n_facts": int(r.n_facts or 0),
                "metadata": dict(r.metadata or {}),
                "expected_score": (
                    float(r.metadata["expected_score"])
                    if (r.metadata and "expected_score" in r.metadata)
                    else None
                ),
            }
            for r in rows
        ]
    )


@router.get(
    "/benchmarks/runs",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def benchmarks_list_runs(
    session: DB,
    domain: str = Query(default="acme"),
    build_label: str | None = Query(default=None),
    run_kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """List individual scored runs. The detail-drawer in the UI uses
    this to show "this answer scored X for these reasons."
    """
    import sqlalchemy as _sa

    where = ["bq.domain = :d"]
    params: dict[str, Any] = {"d": domain, "lim": limit}
    if build_label:
        where.append("br.build_label = :bl")
        params["bl"] = build_label
    if run_kind:
        where.append("br.run_kind = :rk")
        params["rk"] = run_kind

    rows = (
        await session.execute(
            _sa.text(
                f"""
            SELECT br.id, br.question_id, bq.question_key, br.run_kind,
                   br.build_label, br.score_total, br.score_recall,
                   br.score_precision, br.score_specificity,
                   br.score_coverage, br.score_tier_integrity,
                   br.score_evidence_consistency, br.score_format_adherence,
                   br.cost_usd, br.duration_ms, br.agents_dispatched,
                   br.agents_cache_hit, br.facts_injected,
                   br.edges_confirmed, br.edges_contradicted,
                   br.new_edges_inserted, br.answer_confidence,
                   br.cited_entity_urns, br.missing_entities,
                   br.missing_key_facts, br.started_at, br.finished_at,
                   br.conversation_id
            FROM benchmark_runs br
            JOIN benchmark_questions bq ON bq.id = br.question_id
            WHERE {" AND ".join(where)}
            ORDER BY br.started_at DESC
            LIMIT :lim
            """
            ),
            params,
        )
    ).all()

    return BifrostEnvelope(
        data=[
            {
                "id": str(r.id),
                "question_id": str(r.question_id),
                "question_key": r.question_key,
                "run_kind": r.run_kind,
                "build_label": r.build_label,
                "score_total": float(r.score_total or 0.0),
                "score_recall": float(r.score_recall or 0.0),
                "score_precision": float(r.score_precision or 0.0),
                "score_specificity": float(r.score_specificity or 0.0),
                "score_coverage": float(r.score_coverage or 0.0),
                "score_tier_integrity": float(r.score_tier_integrity or 0.0),
                "score_evidence_consistency": float(
                    r.score_evidence_consistency or 0.0,
                ),
                "score_format_adherence": float(r.score_format_adherence or 0.0),
                "cost_usd": float(r.cost_usd or 0.0),
                "duration_ms": int(r.duration_ms or 0),
                "agents_dispatched": int(r.agents_dispatched or 0),
                "agents_cache_hit": int(r.agents_cache_hit or 0),
                "facts_injected": int(r.facts_injected or 0),
                "edges_confirmed": int(r.edges_confirmed or 0),
                "edges_contradicted": int(r.edges_contradicted or 0),
                "new_edges_inserted": int(r.new_edges_inserted or 0),
                "answer_confidence": r.answer_confidence,
                "cited_entity_urns": list(r.cited_entity_urns or []),
                "missing_entities": list(r.missing_entities or []),
                "missing_key_facts": list(r.missing_key_facts or []),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "conversation_id": str(r.conversation_id) if r.conversation_id else None,
            }
            for r in rows
        ]
    )
