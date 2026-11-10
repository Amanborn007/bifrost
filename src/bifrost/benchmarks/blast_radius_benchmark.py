"""Blast radius benchmark — measures how accurately expand_subgraph()
identifies which repos/services are affected when entity X in repo A changes.

Ground truth approach
---------------------
The ``entity_relationships`` graph itself serves as ground truth.  If there
is a direct (1-hop) or 2-hop path from a seed entity to an entity in a
*different* repo, that other repo should appear in the blast radius.  This
is fully testable without any historical PR data.

Algorithm per entity
--------------------
1. Run ``expand_subgraph(seed_ids=[entity.id], direction="downstream",
       depth=3, confidence_floor=threshold)`` — mimics production blast-radius
   walk.
2. **Predicted** repos = {node.repo_id for node in subgraph.nodes} - {seed_repo}.
3. **Ground truth** repos = repos reachable via direct OR 2-hop edges in
   ``entity_relationships`` from this entity, excluding seed repo.
4. precision = |predicted ∩ ground_truth| / |predicted|   (0 if predicted=∅)
5. recall    = |predicted ∩ ground_truth| / |ground_truth| (1.0 if ground_truth=∅)

Threshold sweep
---------------
The benchmark runs each entity at five ``confidence_floor`` levels
(0.0, 0.3, 0.5, 0.7, 0.9) to show the precision/recall trade-off as the
walk becomes more conservative.

CLI
---
    python -m bifrost.benchmarks.blast_radius_benchmark --repos dispatcher,webhook-api
    python -m bifrost.benchmarks.blast_radius_benchmark --all-repos --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid as _uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.api.deps import get_session_factory
from bifrost.cli.config import validate_startup
from bifrost.research.graph_retrieval import expand_subgraph
from bifrost.storage.models import Entity, Repo

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ENTITY_TYPES_OF_INTEREST = ("component", "service", "flow")
MAX_ENTITIES_PER_REPO = 10
CONFIDENCE_THRESHOLDS = (0.0, 0.3, 0.5, 0.7, 0.9)
DEFAULT_WALK_DEPTH = 3


# ── Data containers ──────────────────────────────────────────────────────────


@dataclass
class EntityEvalResult:
    """Evaluation result for one entity at one confidence threshold."""

    entity_id: str
    entity_name: str
    entity_type: str
    seed_repo_id: str
    threshold: float
    predicted_repos: set[str]
    ground_truth_repos: set[str]
    cross_repo_edges_found: int

    @property
    def true_positives(self) -> set[str]:
        return self.predicted_repos & self.ground_truth_repos

    @property
    def precision(self) -> float:
        if not self.predicted_repos:
            # No predictions — convention: precision=1.0 only if ground truth
            # is also empty; otherwise 0.0 (missed everything).
            return 1.0 if not self.ground_truth_repos else 0.0
        return len(self.true_positives) / len(self.predicted_repos)

    @property
    def recall(self) -> float:
        if not self.ground_truth_repos:
            # No cross-repo neighbours — nothing to miss, vacuously satisfied.
            return 1.0
        return len(self.true_positives) / len(self.ground_truth_repos)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        denom = p + r
        if denom == 0.0:
            return 0.0
        return 2 * p * r / denom


@dataclass
class ThresholdStats:
    """Aggregated precision/recall/F1 across all entities for one threshold."""

    threshold: float
    precisions: list[float] = field(default_factory=list)
    recalls: list[float] = field(default_factory=list)
    f1s: list[float] = field(default_factory=list)
    edges_found_total: int = 0

    def add(self, result: EntityEvalResult) -> None:
        self.precisions.append(result.precision)
        self.recalls.append(result.recall)
        self.f1s.append(result.f1)
        self.edges_found_total += result.cross_repo_edges_found

    @property
    def mean_precision(self) -> float:
        return sum(self.precisions) / len(self.precisions) if self.precisions else 0.0

    @property
    def mean_recall(self) -> float:
        return sum(self.recalls) / len(self.recalls) if self.recalls else 0.0

    @property
    def mean_f1(self) -> float:
        return sum(self.f1s) / len(self.f1s) if self.f1s else 0.0


@dataclass
class BenchmarkReport:
    """Full benchmark report across all repos and thresholds."""

    repos_tested: list[str]
    entities_evaluated: int
    threshold_stats: dict[float, ThresholdStats]
    # Per-entity detailed results, keyed by (entity_id, threshold)
    entity_results: list[EntityEvalResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ── Ground truth query ────────────────────────────────────────────────────────


async def _ground_truth_repos(
    session: AsyncSession,
    entity_id: str,
    seed_repo_id: str,
) -> set[str]:
    """Return the set of repo_ids reachable from entity_id via direct or
    2-hop downstream edges in entity_relationships, excluding seed_repo_id.

    Direct: entity -> target (target.scope_repo_id != seed)
    2-hop:  entity -> intermediate -> target (target.scope_repo_id != seed)

    We use raw SQL for performance — two queries joined via CTEs would be
    fine but the union approach here is simpler to understand.
    """
    eid = _uuid.UUID(entity_id)

    # 1-hop direct downstream neighbours in other repos.
    direct_rows = (
        await session.execute(
            sa_text(
                """
                SELECT DISTINCT e_tgt.scope_repo_id AS repo_id
                FROM entity_relationships r
                JOIN entities e_tgt ON e_tgt.id = r.target_id
                WHERE r.source_id = :eid
                  AND e_tgt.scope_repo_id IS NOT NULL
                  AND e_tgt.scope_repo_id != :seed_repo
                """
            ),
            {"eid": eid, "seed_repo": _uuid.UUID(seed_repo_id)},
        )
    ).all()

    # 2-hop downstream (entity -> hop1 -> hop2 in other repos).
    two_hop_rows = (
        await session.execute(
            sa_text(
                """
                SELECT DISTINCT e_tgt.scope_repo_id AS repo_id
                FROM entity_relationships r1
                JOIN entities e_hop ON e_hop.id = r1.target_id
                JOIN entity_relationships r2 ON r2.source_id = e_hop.id
                JOIN entities e_tgt ON e_tgt.id = r2.target_id
                WHERE r1.source_id = :eid
                  AND e_tgt.scope_repo_id IS NOT NULL
                  AND e_tgt.scope_repo_id != :seed_repo
                """
            ),
            {"eid": eid, "seed_repo": _uuid.UUID(seed_repo_id)},
        )
    ).all()

    result: set[str] = set()
    for row in direct_rows:
        if row.repo_id is not None:
            result.add(str(row.repo_id))
    for row in two_hop_rows:
        if row.repo_id is not None:
            result.add(str(row.repo_id))

    return result


# ── Entity fetching ───────────────────────────────────────────────────────────


async def _fetch_repos(
    session: AsyncSession,
    *,
    repo_names: list[str] | None,
) -> list[Repo]:
    """Return all registered repos, or only those named in repo_names."""
    stmt = select(Repo).order_by(Repo.name)
    if repo_names:
        stmt = stmt.where(Repo.name.in_(repo_names))
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _fetch_entities_for_repo(
    session: AsyncSession,
    repo_id: _uuid.UUID,
    *,
    limit: int = MAX_ENTITIES_PER_REPO,
) -> list[Entity]:
    """Return up to ``limit`` component/service/flow entities in this repo,
    ordered by name for determinism.
    """
    stmt = (
        select(Entity)
        .where(
            Entity.scope_repo_id == repo_id,
            Entity.entity_type.in_(ENTITY_TYPES_OF_INTEREST),
        )
        .order_by(Entity.name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


# ── Single-entity evaluation ──────────────────────────────────────────────────


async def _evaluate_entity(
    session: AsyncSession,
    entity: Entity,
    threshold: float,
) -> EntityEvalResult:
    """Run expand_subgraph for one entity + threshold and score the result."""
    seed_repo_id = str(entity.scope_repo_id) if entity.scope_repo_id else ""
    entity_id = str(entity.id)

    # Run the blast-radius walk.
    subgraph = await expand_subgraph(
        session,
        seed_ids=[entity_id],
        depth=DEFAULT_WALK_DEPTH,
        direction="downstream",
        confidence_floor=threshold,
        follow_cross_repo=True,
        soft_cap=None,  # no cap during benchmarking — we want complete coverage
    )

    # Predicted = repos found in the walk (excluding seed repo).
    predicted_repos: set[str] = {
        n.repo_id for n in subgraph.nodes if n.repo_id and n.repo_id != seed_repo_id
    }

    # Ground truth from the raw graph.
    ground_truth_repos = await _ground_truth_repos(session, entity_id, seed_repo_id)

    cross_repo_edges = sum(1 for e in subgraph.edges if e.is_cross_repo)

    return EntityEvalResult(
        entity_id=entity_id,
        entity_name=entity.name,
        entity_type=entity.entity_type,
        seed_repo_id=seed_repo_id,
        threshold=threshold,
        predicted_repos=predicted_repos,
        ground_truth_repos=ground_truth_repos,
        cross_repo_edges_found=cross_repo_edges,
    )


# ── Per-repo evaluation ───────────────────────────────────────────────────────


async def _evaluate_repo(
    session_factory: async_sessionmaker[AsyncSession],
    repo: Repo,
    entity_limit: int,
) -> tuple[list[EntityEvalResult], list[str]]:
    """Evaluate all entities in a repo across all confidence thresholds.

    Returns (results, errors).  A repo with no matching entities logs a
    warning and returns empty results rather than crashing.
    """
    results: list[EntityEvalResult] = []
    errors: list[str] = []
    repo_name = repo.name
    repo_id = repo.id

    log = logger.bind(repo=repo_name)

    async with session_factory() as session:
        entities = await _fetch_entities_for_repo(session, repo_id, limit=entity_limit)

    if not entities:
        log.warning("no_entities_found", entity_types=list(ENTITY_TYPES_OF_INTEREST))
        return results, errors

    log.info("evaluating_repo", entity_count=len(entities))

    for entity in entities:
        elog = log.bind(entity=entity.name, entity_type=entity.entity_type)
        for threshold in CONFIDENCE_THRESHOLDS:
            try:
                async with session_factory() as session:
                    result = await _evaluate_entity(session, entity, threshold)
                results.append(result)
                elog.debug(
                    "entity_evaluated",
                    threshold=threshold,
                    predicted=len(result.predicted_repos),
                    ground_truth=len(result.ground_truth_repos),
                    precision=round(result.precision, 3),
                    recall=round(result.recall, 3),
                )
            except Exception as exc:
                msg = f"entity={entity.name} threshold={threshold}: {exc}"
                errors.append(msg)
                elog.exception("entity_eval_failed", threshold=threshold)

    return results, errors


# ── Aggregation ───────────────────────────────────────────────────────────────


def _aggregate_results(
    all_results: list[EntityEvalResult],
) -> dict[float, ThresholdStats]:
    """Group results by threshold and compute aggregate metrics."""
    stats: dict[float, ThresholdStats] = {
        t: ThresholdStats(threshold=t) for t in CONFIDENCE_THRESHOLDS
    }
    for result in all_results:
        if result.threshold in stats:
            stats[result.threshold].add(result)
    return stats


# ── Report printing ───────────────────────────────────────────────────────────


def _print_report(report: BenchmarkReport) -> None:
    """Print a structured human-readable benchmark report to stdout."""
    # Count unique entities (at threshold 0.0 as representative).
    total_cross_repo = report.threshold_stats.get(0.0)
    cross_repo_edges = total_cross_repo.edges_found_total if total_cross_repo else 0

    print()
    print("BLAST RADIUS BENCHMARK RESULTS")
    print("================================")
    print(f"Repos tested: {len(report.repos_tested)}")
    print(f"Entities evaluated: {report.entities_evaluated}")
    print(f"Cross-repo edges detected: {cross_repo_edges}  (at threshold=0.0)")
    print(f"Duration: {report.duration_seconds:.1f}s")
    if report.errors:
        print(f"Errors encountered: {len(report.errors)}")
        for err in report.errors[:5]:
            print(f"  - {err}")
        if len(report.errors) > 5:
            print(f"  ... and {len(report.errors) - 5} more")
    print()

    col_w = dict(threshold=9, precision=11, recall=8, f1=7, edges=12)
    header = (
        f"{'Threshold':<{col_w['threshold']}} "
        f"{'Precision':>{col_w['precision']}} "
        f"{'Recall':>{col_w['recall']}} "
        f"{'F1':>{col_w['f1']}} "
        f"{'Edges found':>{col_w['edges']}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    for threshold in CONFIDENCE_THRESHOLDS:
        ts = report.threshold_stats.get(threshold)
        if ts is None or not ts.precisions:
            print(
                f"{threshold:<{col_w['threshold']}.1f} "
                f"{'—':>{col_w['precision']}} "
                f"{'—':>{col_w['recall']}} "
                f"{'—':>{col_w['f1']}} "
                f"{'—':>{col_w['edges']}}"
            )
            continue
        print(
            f"{threshold:<{col_w['threshold']}.1f} "
            f"{ts.mean_precision:>{col_w['precision']}.2f} "
            f"{ts.mean_recall:>{col_w['recall']}.2f} "
            f"{ts.mean_f1:>{col_w['f1']}.2f} "
            f"{ts.edges_found_total:>{col_w['edges']}}"
        )
    print()

    # Per-repo breakdown at threshold 0.0.
    if report.entity_results:
        _print_repo_breakdown(report)


def _print_repo_breakdown(report: BenchmarkReport) -> None:
    """Print a per-repo summary table at threshold=0.0."""
    # Group entity results by (seed_repo_id, threshold=0.0).
    from collections import defaultdict

    repo_buckets: dict[str, list[EntityEvalResult]] = defaultdict(list)
    for r in report.entity_results:
        if r.threshold == 0.0:
            repo_buckets[r.seed_repo_id].append(r)

    if not repo_buckets:
        return

    # Build a repo_id → name lookup from the repos_tested list (we only have
    # names; query for ids isn't available here, so we show repo_id prefix).
    print("PER-REPO BREAKDOWN  (threshold=0.0)")
    print("-" * 65)
    header2 = f"{'Repo ID (prefix)':<38} {'Entities':>8} {'Prec':>6} {'Rec':>6} {'F1':>6}"
    print(header2)
    print("-" * len(header2))
    for repo_id, bucket in sorted(repo_buckets.items()):
        n = len(bucket)
        prec = sum(r.precision for r in bucket) / n
        rec = sum(r.recall for r in bucket) / n
        f1 = sum(r.f1 for r in bucket) / n
        short_id = repo_id[:36]
        print(f"{short_id:<38} {n:>8} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f}")
    print()


# ── Main benchmark runner ─────────────────────────────────────────────────────


async def run_blast_radius_benchmark(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_names: list[str] | None = None,
    entity_limit: int = MAX_ENTITIES_PER_REPO,
) -> BenchmarkReport:
    """Run the full blast radius benchmark and return a report.

    Parameters
    ----------
    session_factory:
        Async session factory (from ``get_session_factory(settings)``).
    repo_names:
        If provided, restrict to these repo names.  None evaluates all
        registered repos.
    entity_limit:
        Maximum entities to sample per repo (default 10).
    """
    started = time.monotonic()
    all_errors: list[str] = []

    # 1. Fetch repos to evaluate.
    async with session_factory() as session:
        repos = await _fetch_repos(session, repo_names=repo_names)

    if not repos:
        scope_msg = f"repos={repo_names}" if repo_names else "all repos"
        raise RuntimeError(f"no registered repos found for scope: {scope_msg}")

    logger.info(
        "blast_radius_benchmark_starting",
        repos=len(repos),
        entity_limit=entity_limit,
        thresholds=CONFIDENCE_THRESHOLDS,
    )

    # 2. Evaluate each repo (sequentially — avoids connection pool exhaustion
    #    on large org installs; each repo's entities run at all 5 thresholds).
    all_results: list[EntityEvalResult] = []
    evaluated_repo_names: list[str] = []

    for repo in repos:
        t0 = time.monotonic()
        repo_results, repo_errors = await _evaluate_repo(session_factory, repo, entity_limit)
        elapsed = time.monotonic() - t0
        all_results.extend(repo_results)
        all_errors.extend(repo_errors)
        if repo_results:
            evaluated_repo_names.append(repo.name)
            logger.info(
                "repo_done",
                repo=repo.name,
                entities=len({r.entity_id for r in repo_results}),
                elapsed_s=round(elapsed, 2),
            )
        else:
            logger.warning("repo_skipped_no_entities", repo=repo.name)

    # 3. Aggregate.
    threshold_stats = _aggregate_results(all_results)

    # Unique entities = distinct entity_ids (results repeated per threshold).
    unique_entities = len({r.entity_id for r in all_results})

    report = BenchmarkReport(
        repos_tested=evaluated_repo_names,
        entities_evaluated=unique_entities,
        threshold_stats=threshold_stats,
        entity_results=all_results,
        duration_seconds=time.monotonic() - started,
        errors=all_errors,
    )
    return report


# ── CLI ───────────────────────────────────────────────────────────────────────


async def _async_main(args: argparse.Namespace) -> int:
    settings = validate_startup()
    session_factory = get_session_factory(settings)

    repo_names: list[str] | None = None
    if not args.all_repos:
        if not args.repos:
            print("error: specify --repos <name,...> or --all-repos")
            return 1
        repo_names = [r.strip() for r in args.repos.split(",") if r.strip()]

    entity_limit = args.limit if args.limit else MAX_ENTITIES_PER_REPO

    try:
        report = await run_blast_radius_benchmark(
            session_factory,
            repo_names=repo_names,
            entity_limit=entity_limit,
        )
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 1

    _print_report(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bifrost-blast-radius-bench",
        description=(
            "Blast radius benchmark — measures expand_subgraph() accuracy for "
            "cross-repo impact detection.  Uses entity_relationships as ground truth."
        ),
    )

    scope_group = parser.add_mutually_exclusive_group(required=True)
    scope_group.add_argument(
        "--repos",
        default=None,
        metavar="REPO,...",
        help="Comma-separated repo names to benchmark (e.g. dispatcher,webhook-api).",
    )
    scope_group.add_argument(
        "--all-repos",
        action="store_true",
        default=False,
        help="Benchmark all registered repos.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_ENTITIES_PER_REPO,
        metavar="N",
        help=f"Max entities to sample per repo (default {MAX_ENTITIES_PER_REPO}).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        help="Log level: DEBUG | INFO | WARNING (default INFO).",
    )

    args = parser.parse_args(argv)

    # Configure structlog to honour the requested level.
    import logging

    import structlog

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(message)s",
    )
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, args.log_level.upper(), logging.INFO)
        ),
    )

    return asyncio.run(_async_main(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
