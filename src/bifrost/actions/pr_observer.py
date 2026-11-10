"""PR observer — closes the SDLC loop by polling open task PRs and
transitioning the task to ``pr_merged`` when the PR is merged.

Why polling, not webhooks:
  Webhooks need a public endpoint, secret rotation, replay
  protection, IP allowlisting. Polling reuses local ``gh`` auth, has
  no secrets, no infrastructure dependency, and is deterministic.
  60s lag on merge → done is acceptable for the workflow.

Behavior:
  Every ``OBSERVE_INTERVAL_S`` (default 60s), find every task with
  ``state='success'`` AND ``pr_url IS NOT NULL`` AND
  ``state != 'pr_merged'``. For each, run ``gh pr view <url>
  --json state,mergedAt`` and inspect:

    state == 'MERGED'  → transition task to PR_MERGED. The
        task_lifecycle hook fires ``jira_sync.on_transition`` which
        moves the Jira ticket to "Done". Closes the loop.
    state == 'CLOSED' (without merge) → leave the task at SUCCESS.
        Operator can manually transition to CANCELLED if intended.
    state == 'OPEN'    → no-op; check next cycle.

Failure handling:
  ``gh`` not installed / not authenticated: log warning at startup,
  observer skips its work but the lifespan task stays alive (it'll
  retry next cycle).
  Per-task ``gh`` failures: logged, task stays at SUCCESS, observer
  moves on. NO retries within a cycle.

Tests use the ``poll_one_cycle`` entry point with a stub ``gh``
runner so we don't need a real gh binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


OBSERVE_INTERVAL_S = 60.0
GH_TIMEOUT_S = 20.0


@dataclass
class _PRStatus:
    state: str  # OPEN | CLOSED | MERGED
    merged_at: str | None
    raw: dict


GhRunner = Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]]


# ── default subprocess runner ───────────────────────────────────────


async def _default_gh(argv: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=GH_TIMEOUT_S,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return 124, b"", b"timeout"
    return (proc.returncode or 0), stdout or b"", stderr or b""


async def _query_pr_status(
    pr_url: str,
    *,
    gh_runner: GhRunner,
) -> _PRStatus | None:
    """Run ``gh pr view <url> --json state,mergedAt`` and parse."""
    if not _looks_like_gh_pr_url(pr_url):
        return None
    code, stdout, stderr = await gh_runner(
        [
            "gh",
            "pr",
            "view",
            pr_url,
            "--json",
            "state,mergedAt",
        ]
    )
    if code != 0:
        logger.debug(
            "pr_observer: gh pr view failed url=%s code=%d stderr=%s",
            pr_url,
            code,
            (stderr or b"").decode("utf-8", errors="replace")[:200],
        )
        return None
    try:
        raw = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return None
    state = str(raw.get("state") or "").upper().strip()
    merged_at = raw.get("mergedAt")
    return _PRStatus(state=state, merged_at=merged_at, raw=raw)


_GH_PR_RE = re.compile(
    r"^https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+$",
)


def _looks_like_gh_pr_url(url: str) -> bool:
    return bool(_GH_PR_RE.match((url or "").strip().rstrip("/")))


# ── polling entry points ────────────────────────────────────────────


# Maximum staleness for an open task PR before the observer stops
# polling. PRs that sit open for 30+ days are almost certainly
# abandoned (closed without merge, vendor-blocked, or a branch the
# author moved on from); polling them every 60s for the lifetime of
# the process wastes ``gh`` calls and grows the loop cost linearly
# with backlog. After 30 days, we leave the task at ``success`` but
# stop watching — the operator can manually transition or use the
# task detail page's "re-check" button to wake it back up.
PR_OBSERVER_MAX_AGE_DAYS = 30


async def _list_open_pr_tasks(
    session_factory: async_sessionmaker,
) -> list[dict]:
    """Return tasks with ``state='success'`` AND ``pr_url`` set, that
    were updated within the staleness cutoff. The cutoff prevents the
    observer from churning on indefinitely-open PRs."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id::text AS id, state, pr_url, jira_key "
                        "FROM tasks "
                        "WHERE state = 'success' "
                        "  AND pr_url IS NOT NULL "
                        "  AND updated_at > NOW() - make_interval(days => :max_age) "
                    ),
                    {"max_age": PR_OBSERVER_MAX_AGE_DAYS},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def poll_one_cycle(
    session_factory: async_sessionmaker,
    *,
    gh_runner: GhRunner | None = None,
) -> dict[str, int]:
    """Run a single observer pass. Returns counters for tests/metrics."""
    runner = gh_runner or _default_gh
    counters = {"checked": 0, "merged": 0, "errors": 0}

    tasks = await _list_open_pr_tasks(session_factory)
    counters["checked"] = len(tasks)
    if not tasks:
        return counters

    from bifrost.actions.task_lifecycle import (
        TaskState,
        fire_pending_jira_hooks,
        transition,
    )

    for t in tasks:
        try:
            status = await _query_pr_status(
                str(t["pr_url"]),
                gh_runner=runner,
            )
            if status is None:
                continue
            if status.state == "MERGED":
                async with session_factory() as session:
                    try:
                        await transition(
                            session,
                            task_id=str(t["id"]),
                            to_state=TaskState.PR_MERGED,
                            actor="pr_observer",
                            reason=(
                                f"PR merged on {status.merged_at}"
                                if status.merged_at
                                else "PR merged"
                            ),
                        )
                        await session.commit()
                        await fire_pending_jira_hooks(
                            session,
                            session_factory=session_factory,
                        )
                        counters["merged"] += 1
                    except Exception:  # noqa: BLE001
                        await session.rollback()
                        logger.exception(
                            "pr_observer: transition failed task=%s",
                            t["id"],
                        )
                        counters["errors"] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "pr_observer: per-task probe failed task=%s",
                t.get("id"),
            )
            counters["errors"] += 1
    return counters


async def observer_loop(
    session_factory: async_sessionmaker,
    *,
    interval_s: float = OBSERVE_INTERVAL_S,
    gh_runner: GhRunner | None = None,
) -> None:
    """Long-running coroutine — call as ``asyncio.create_task`` from
    the API lifespan. Cancellation-safe: re-raises CancelledError so
    the lifespan tear-down terminates cleanly."""
    logger.info(
        "pr_observer.started interval_s=%.0f",
        interval_s,
    )
    while True:
        try:
            counts = await poll_one_cycle(
                session_factory,
                gh_runner=gh_runner,
            )
            if counts["checked"] or counts["merged"] or counts["errors"]:
                logger.info(
                    "pr_observer.cycle checked=%d merged=%d errors=%d",
                    counts["checked"],
                    counts["merged"],
                    counts["errors"],
                )
        except asyncio.CancelledError:
            logger.info("pr_observer.stopped")
            raise
        except Exception:  # noqa: BLE001
            # Top-level safety: a bug in poll_one_cycle should NOT
            # kill the lifespan task. Log + continue.
            logger.exception("pr_observer: cycle crashed; continuing")
        await asyncio.sleep(interval_s)
