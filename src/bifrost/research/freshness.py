"""Ensure every candidate repo has a fresh local clone before research.

Chat is a read-only operation on the user's timeline — we don't block on
extraction, but we DO want the code the sub-agent reads to match the remote
HEAD. This module:

  1. Clones any repo that's registered but not locally present.
  2. Fetches + resets an existing clone if its HEAD differs from the remote.
  3. Notes (but does not block on) cases where the remote has moved past
     the last extracted commit — the chat answer should flag "metadata may
     be stale".
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from bifrost.adapters.git import GitAdapter, GitAdapterError, GitCommandError
from bifrost.cli.config import BifrostSettings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoFreshness:
    """Result of ensuring a repo is locally available and up to date."""

    repo_id: str
    repo_name: str
    clone_path: str
    remote_head: str  # sha of remote HEAD right now
    local_head: str  # sha we just checked out (== remote_head on success)
    last_extracted_sha: str | None  # from repo_freshness.last_commit_indexed
    metadata_stale: bool  # True if last_extracted_sha != remote_head
    commits_behind: int  # best-effort count; 0 when fresh
    ensured_cloned: bool  # True if we had to clone from scratch this turn
    error: str | None = None  # non-empty = don't trust this repo for research


async def _ls_remote_head(git: GitAdapter, url: str) -> str | None:
    """Return the sha of HEAD at the remote without touching the worktree."""
    try:
        result = await git._run_git(["ls-remote", url, "HEAD"])  # noqa: SLF001
        # Output line: "<sha>\tHEAD"
        line = (result.stdout or "").strip().splitlines()
        if line:
            sha = line[0].split()[0].strip()
            return sha if sha else None
    except GitCommandError as exc:
        logger.warning("git ls-remote %s failed: %s", url, exc)
    return None


async def ensure_fresh_clone(
    *,
    repo_id: str,
    repo_name: str,
    repo_url: str,
    last_extracted_sha: str | None,
    settings: BifrostSettings | None = None,
) -> RepoFreshness:
    """Guarantee a usable clone at `{clone_dir}/repo-{id}` matching remote HEAD.

    Steps:
      1. Ask GitHub for remote HEAD sha (one ls-remote call).
      2. If no local clone: shallow clone.
      3. If local clone exists: fetch + hard-reset to remote HEAD.
      4. Confirm local HEAD matches remote HEAD.
      5. Compute staleness vs last_extracted_sha and return.

    Never raises on a single repo failure — populates `error` and returns.
    Caller should exclude failed repos from research fan-out.
    """
    settings = settings or get_settings()
    git = GitAdapter()
    clone_path = os.path.join(settings.clone_dir, f"repo-{repo_id}")

    remote_head = await _ls_remote_head(git, repo_url) or ""
    if not remote_head:
        return RepoFreshness(
            repo_id=repo_id,
            repo_name=repo_name,
            clone_path=clone_path,
            remote_head="",
            local_head="",
            last_extracted_sha=last_extracted_sha,
            metadata_stale=False,
            commits_behind=0,
            ensured_cloned=False,
            error="ls-remote failed (network? auth?)",
        )

    ensured_cloned = False
    try:
        if not os.path.isdir(clone_path):
            await git.clone(repo_url, clone_path, shallow=True)
            ensured_cloned = True
        else:
            # Sync: fetch + hard-reset. Falls back to reclone on failure.
            try:
                await git.fetch_all(clone_path)
                await git.reset_hard_to_remote(clone_path)
            except Exception as sync_exc:  # noqa: BLE001
                logger.warning(
                    "fetch+reset failed for %s (%s); re-cloning",
                    clone_path,
                    sync_exc,
                )
                import shutil

                shutil.rmtree(clone_path, ignore_errors=True)
                await git.clone(repo_url, clone_path, shallow=True)
                ensured_cloned = True
    except Exception as clone_exc:  # noqa: BLE001
        return RepoFreshness(
            repo_id=repo_id,
            repo_name=repo_name,
            clone_path=clone_path,
            remote_head=remote_head,
            local_head="",
            last_extracted_sha=last_extracted_sha,
            metadata_stale=False,
            commits_behind=0,
            ensured_cloned=False,
            error=f"clone/sync failed: {clone_exc}",
        )

    # Read local HEAD sha after sync
    try:
        local_meta = await git.get_metadata(clone_path)
        local_head = local_meta.get("head_commit", "") or ""
    except GitAdapterError as exc:
        logger.warning("get_metadata failed for %s: %s", clone_path, exc)
        local_head = remote_head  # best-effort assume we matched

    metadata_stale = bool(last_extracted_sha and last_extracted_sha != remote_head)

    return RepoFreshness(
        repo_id=repo_id,
        repo_name=repo_name,
        clone_path=clone_path,
        remote_head=remote_head,
        local_head=local_head,
        last_extracted_sha=last_extracted_sha,
        metadata_stale=metadata_stale,
        commits_behind=0,
        ensured_cloned=ensured_cloned,
        error=None,
    )


async def ensure_fresh_clones(
    repos: list[dict[str, str]],
    settings: BifrostSettings | None = None,
    *,
    concurrency: int = 5,
) -> list[RepoFreshness]:
    """Parallel ensure_fresh_clone over a list of repos.

    Each entry in `repos` must have keys: id, name, url, last_extracted_sha
    (may be None). Bounded concurrency so we don't hammer github with 20
    parallel fetches.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(r: dict[str, str]) -> RepoFreshness:
        async with sem:
            return await ensure_fresh_clone(
                repo_id=r["id"],
                repo_name=r["name"],
                repo_url=r["url"],
                last_extracted_sha=r.get("last_extracted_sha"),
                settings=settings,
            )

    return await asyncio.gather(*(_one(r) for r in repos))
