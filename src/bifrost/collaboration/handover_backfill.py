"""Auto-handover backfill scheduler — background task.

Runs every 6 hours and generates repo-scoped handover docs for any
repo that:

  1. Has ``repo_freshness.freshness_state = 'fresh'`` (index has been
     built; generating a handover from a stale or empty index produces
     a low-quality document).
  2. Has no existing ``handover_docs`` row with ``scope_kind = 'repo'``
     AND ``archived_at IS NULL`` (not already covered).
  3. Is not currently being generated (no ``handover_docs`` row with
     any state for this repo — guarded by the UniqueConstraint on
     scope_kind + scope_value + parent_id, but we check eagerly to
     avoid the exception path).

At most 20 repos are processed per run to bound cost spikes. The job
is idempotent: ``generate_repo_handover`` itself upserts on the DB
unique constraint, so an interrupted run leaves no orphaned rows.

The background task is opt-out via ``BIFROST_DISABLE_HANDOVER_BACKFILL=1``.
It runs in the background on every API process. SQL queries are
read-then-write, so overlapping replicas may generate the same repo
twice in a race window — that is acceptable; the second call will
overwrite the first with a fresher doc.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import and_, exists, not_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.collaboration.handover_generator import generate_repo_handover
from bifrost.storage.models import HandoverDoc, Repo, RepoFreshness
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


BACKFILL_INTERVAL_S = 6 * 3600  # 6 hours
_STARTUP_DELAY_S = 60  # let the API warm up before the first sweep
_MAX_REPOS_PER_RUN = 20  # cost-spike guard


def _is_disabled() -> bool:
    return os.environ.get("BIFROST_DISABLE_HANDOVER_BACKFILL", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _find_candidate_repo_ids(
    session: AsyncSession,
    *,
    limit: int = _MAX_REPOS_PER_RUN,
) -> list[tuple]:
    """Return up to *limit* (repo_id, repo_name) rows that need a handover doc.

    Candidates satisfy:
      - ``repo_freshness.freshness_state = 'fresh'``
      - No ``handover_docs`` row with scope_kind='repo' for this repo
        (regardless of archived_at — even an archived doc means we
        already generated one; skipping avoids redundant regen).
    """
    existing_handover = (
        select(HandoverDoc.id)
        .where(
            and_(
                HandoverDoc.scope_kind == "repo",
                HandoverDoc.scope_value == Repo.name,
            )
        )
        .correlate(Repo)
    )

    stmt = (
        select(Repo.id, Repo.name)
        .join(RepoFreshness, RepoFreshness.repo_id == Repo.id)
        .where(
            and_(
                RepoFreshness.freshness_state == "fresh",
                not_(exists(existing_handover)),
            )
        )
        .order_by(Repo.created_at.asc())
        .limit(limit)
    )

    rows = (await session.execute(stmt)).all()
    return list(rows)


async def _run_backfill(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Execute one backfill sweep: find candidates and generate handover docs."""
    async with session_factory() as session:
        candidates = await _find_candidate_repo_ids(session)

    if not candidates:
        logger.debug("handover_backfill.sweep no candidates found")
        return

    logger.info(
        "handover_backfill.sweep candidates=%d max_per_run=%d",
        len(candidates),
        _MAX_REPOS_PER_RUN,
    )

    succeeded = 0
    failed = 0

    for repo_id, repo_name in candidates:
        logger.info(
            "handover_backfill.generating repo=%s repo_id=%s",
            repo_name,
            repo_id,
        )
        try:
            async with session_factory() as session:
                repo = EntityRepository(session)
                await generate_repo_handover(repo_id=repo_id, repo=repo)
            succeeded += 1
            logger.info(
                "handover_backfill.generated repo=%s repo_id=%s",
                repo_name,
                repo_id,
            )
        except Exception:
            failed += 1
            logger.exception(
                "handover_backfill.failed repo=%s repo_id=%s",
                repo_name,
                repo_id,
            )

    logger.info(
        "handover_backfill.sweep_done candidates=%d succeeded=%d failed=%d",
        len(candidates),
        succeeded,
        failed,
    )


async def backfill_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_s: int = BACKFILL_INTERVAL_S,
) -> None:
    """Main loop. Runs until the process is killed or the task is cancelled."""
    try:
        await asyncio.sleep(_STARTUP_DELAY_S)
        while True:
            try:
                await _run_backfill(session_factory)
            except Exception:
                logger.exception("handover_backfill.tick failed")
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("handover_backfill.task cancelled (process shutting down)")
        raise


def start_backfill_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> asyncio.Task[None] | None:
    """Kick off the 6-hour backfill task. Returns None when disabled."""
    if _is_disabled():
        logger.info(
            "handover_backfill disabled via BIFROST_DISABLE_HANDOVER_BACKFILL",
        )
        return None
    task = asyncio.create_task(
        backfill_loop(session_factory),
        name="bifrost.handover_backfill",
    )
    logger.info(
        "handover_backfill.scheduled startup_delay=%ds interval=%ds max_per_run=%d",
        _STARTUP_DELAY_S,
        BACKFILL_INTERVAL_S,
        _MAX_REPOS_PER_RUN,
    )
    return task
