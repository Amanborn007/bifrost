"""Auto-archive scheduler — library entrypoint + background task.

Two callers:
  * ``POST /tasks/auto-archive`` in ``api/routes/tasks.py`` (user-driven)
  * ``start_auto_archive_task()`` below (runs on API startup + hourly)

Both share ``sweep_terminal_tasks()`` to avoid divergence. The hourly
task also runs ``sweep_expired_caches()`` (jira / confluence rows whose
``expires_at`` has passed) and ``sweep_old_worktrees()`` (terminal-state
task worktrees older than the GC window) to bound retention.

The background task is opt-out via ``BIFROST_DISABLE_AUTO_ARCHIVE=1``.
It runs in the background on every API process, but the SQL updates
are idempotent so overlapping runs from multiple replicas just produce
0-row no-ops.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)


_TERMINAL_STATES = frozenset({"success", "cancelled", "blocked"})
_STARTUP_DELAY_S = 30
_TICK_INTERVAL_S = 60 * 60  # hourly
_DEFAULT_OLDER_THAN_DAYS = 14
_WORKTREE_GC_DAYS = 7


@dataclass
class SweepResult:
    archived_ids: list[str]
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def sweep_terminal_tasks(
    session: AsyncSession,
    *,
    older_than_days: int = _DEFAULT_OLDER_THAN_DAYS,
    dry_run: bool = False,
) -> SweepResult:
    """Flip archived_at on terminal tasks older than ``older_than_days``.

    Returns the list of ids swept. Idempotent: rows already archived
    are ignored.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text
                FROM tasks
                WHERE archived_at IS NULL
                  AND state = ANY(CAST(:states AS text[]))
                  AND updated_at < NOW() - make_interval(days => :days)
                ORDER BY updated_at ASC
                """
            ),
            {
                "states": list(_TERMINAL_STATES),
                "days": older_than_days,
            },
        )
    ).all()
    ids = [r[0] for r in rows]
    if dry_run or not ids:
        return SweepResult(archived_ids=ids, dry_run=dry_run)
    await session.execute(
        text(
            """
            UPDATE tasks
            SET archived_at = :at, updated_at = NOW()
            WHERE id = ANY(CAST(:ids AS uuid[]))
              AND archived_at IS NULL
            """
        ),
        {"at": datetime.now(UTC), "ids": ids},
    )
    await session.commit()
    return SweepResult(archived_ids=ids, dry_run=False)


# ── Cache + worktree retention ─────────────────────────────────────


@dataclass
class RetentionResult:
    """One sweep tick's deletion counts.

    ``cache_rows`` is the total rows hard-deleted from ``jira_issues``
    and ``confluence_pages`` (combined). ``worktrees_removed`` is the
    list of paths the GC removed from disk.
    """

    cache_rows: int
    worktrees_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def sweep_expired_caches(session: AsyncSession) -> int:
    """Hard-delete jira/confluence rows past their ``expires_at``.

    Returns the total rows deleted across both tables. Cheap; runs at
    every tick.
    """
    total = 0
    for table in ("jira_issues", "confluence_pages"):
        result = await session.execute(
            text(
                f"""
                DELETE FROM {table}
                WHERE expires_at IS NOT NULL
                  AND expires_at < NOW()
                """
            ),
        )
        total += affected_rows(result)
    if total:
        await session.commit()
    return total


async def sweep_old_worktrees(
    session: AsyncSession,
    *,
    older_than_days: int = _WORKTREE_GC_DAYS,
) -> list[str]:
    """Remove on-disk worktrees for tasks that reached a terminal
    state more than ``older_than_days`` ago.

    Worktrees can hold uncommitted changes, embedded GitHub auth tokens
    in ``.git/config``, and source clones. Leaving them on disk
    indefinitely violates retention. Once a task is success / blocked /
    cancelled and old enough, ``rm -rf`` it.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text, worktree_path
                FROM tasks
                WHERE worktree_path IS NOT NULL
                  AND state = ANY(CAST(:states AS text[]))
                  AND updated_at < NOW() - make_interval(days => :days)
                ORDER BY updated_at ASC
                """
            ),
            {
                "states": list(_TERMINAL_STATES),
                "days": older_than_days,
            },
        )
    ).all()

    removed: list[str] = []
    cleared_ids: list[str] = []
    for row in rows:
        bid, path_str = row[0], row[1]
        path = Path(path_str)
        if not path.exists():
            # Already gone — drop the column reference too.
            cleared_ids.append(bid)
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
        except OSError as exc:
            logger.warning(
                "auto_archive: worktree GC failed for task %s at %s: %s",
                bid,
                path_str,
                exc,
            )
            continue
        removed.append(path_str)
        cleared_ids.append(bid)

    if cleared_ids:
        await session.execute(
            text(
                """
                UPDATE tasks
                SET worktree_path = NULL, updated_at = NOW()
                WHERE id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"ids": cleared_ids},
        )
        await session.commit()
    return removed


async def sweep_retention(
    session: AsyncSession,
    *,
    worktree_older_than_days: int = _WORKTREE_GC_DAYS,
) -> RetentionResult:
    """Combined cache + worktree retention pass."""
    errors: list[str] = []
    cache_rows = 0
    try:
        cache_rows = await sweep_expired_caches(session)
    except Exception as exc:
        logger.exception("auto_archive: cache sweep failed")
        errors.append(f"cache: {exc!r}")

    worktrees_removed: list[str] = []
    try:
        worktrees_removed = await sweep_old_worktrees(
            session,
            older_than_days=worktree_older_than_days,
        )
    except Exception as exc:
        logger.exception("auto_archive: worktree GC failed")
        errors.append(f"worktrees: {exc!r}")

    return RetentionResult(
        cache_rows=cache_rows,
        worktrees_removed=worktrees_removed,
        errors=errors,
    )


# ── Background task ────────────────────────────────────────────────


def is_disabled() -> bool:
    return os.environ.get("BIFROST_DISABLE_AUTO_ARCHIVE", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    older_than_days: int,
    tick_interval_s: int,
    startup_delay_s: int,
) -> None:
    """Main loop. Runs until the process is killed."""
    try:
        if startup_delay_s > 0:
            await asyncio.sleep(startup_delay_s)
        while True:
            try:
                async with session_factory() as session:
                    result = await sweep_terminal_tasks(
                        session,
                        older_than_days=older_than_days,
                        dry_run=False,
                    )
                if result.archived_ids:
                    logger.info(
                        "auto_archive.sweep archived=%d older_than_days=%d",
                        len(result.archived_ids),
                        older_than_days,
                    )
                else:
                    logger.debug(
                        "auto_archive.sweep nothing to archive (older_than_days=%d)",
                        older_than_days,
                    )
            except Exception:
                logger.exception("auto_archive.sweep tick failed")

            # Cache + worktree retention. Independent of task
            # archival above; failures here don't stop the next tick.
            try:
                async with session_factory() as session:
                    rresult = await sweep_retention(session)
                if rresult.cache_rows or rresult.worktrees_removed:
                    logger.info(
                        "auto_archive.retention cache_rows=%d worktrees=%d errors=%d",
                        rresult.cache_rows,
                        len(rresult.worktrees_removed),
                        len(rresult.errors),
                    )
            except Exception:
                logger.exception("auto_archive.retention tick failed")

            await asyncio.sleep(tick_interval_s)
    except asyncio.CancelledError:
        logger.info("auto_archive.task cancelled (process shutting down)")
        raise


def start_auto_archive_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    older_than_days: int = _DEFAULT_OLDER_THAN_DAYS,
    tick_interval_s: int = _TICK_INTERVAL_S,
    startup_delay_s: int = _STARTUP_DELAY_S,
) -> asyncio.Task[None] | None:
    """Kick off the hourly sweep task. Returns None when disabled."""
    if is_disabled():
        logger.info(
            "auto_archive disabled via BIFROST_DISABLE_AUTO_ARCHIVE",
        )
        return None
    task = asyncio.create_task(
        _run_forever(
            session_factory,
            older_than_days=older_than_days,
            tick_interval_s=tick_interval_s,
            startup_delay_s=startup_delay_s,
        ),
        name="bifrost.auto_archive",
    )
    logger.info(
        "auto_archive.scheduled startup_delay=%ds tick=%ds older_than_days=%d",
        startup_delay_s,
        tick_interval_s,
        older_than_days,
    )
    return task
