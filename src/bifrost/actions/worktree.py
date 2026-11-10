"""Per-task git worktrees.

Each task that enters execution gets its own git worktree so the
executor can commit code changes without touching the target repo's
working tree. Layout:

    <target_repo>/
      .claude/
        worktrees/
          task-<shortid>/          # worktree checkout lives here
            ... full repo tree ...

The branch is named ``bifrost/<shortid>-<slug>`` where slug is a URL-
safe derivative of the task title. Worktrees outlive a single
execution run so a user can resume or inspect them; the ``remove()``
helper tears them down when the task lands in SUCCESS / CANCELLED.

All operations are idempotent. ``ensure_for_task()`` re-uses an
existing worktree if one already matches (task_id, target_repo).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── Types ───────────────────────────────────────────────────────────


@dataclass
class Worktree:
    task_id: str
    repo_root: str
    path: str
    branch: str
    head_sha: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class WorktreeError(RuntimeError):
    pass


# ── Public entry points ─────────────────────────────────────────────


async def ensure_for_task(
    session: AsyncSession,
    *,
    task_id: str,
    target_repo_path: str,
    base_ref: str = "HEAD",
) -> Worktree:
    """Return a ready-to-use worktree for the task; create if missing.

    Writes ``tasks.worktree_path`` + ``branch_name`` on success.
    """
    repo_root = str(Path(target_repo_path).resolve())
    await _assert_is_git_repo(repo_root)

    title = await _load_task_title(session, task_id)
    slug = _slugify(title) if title else "task"
    shortid = task_id[:8]
    branch = f"bifrost/{shortid}-{slug}"
    wt_path = str(
        Path(repo_root) / ".claude" / "worktrees" / f"task-{shortid}",
    )

    # If already registered under this exact path, re-use.
    existing = await _worktree_exists(repo_root, wt_path)
    if not existing:
        # Make sure parent exists.
        Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
        # If branch exists already, check it out; otherwise create new.
        branch_exists = await _branch_exists(repo_root, branch)
        args = ["worktree", "add"]
        if not branch_exists:
            args += ["-b", branch]
        args += [wt_path]
        if not branch_exists:
            args += [base_ref]
        else:
            args += [branch]
        await _git(repo_root, *args)

    head_sha = await _head_sha(wt_path)

    # Persist on the task row.
    await session.execute(
        text(
            """
            UPDATE tasks
            SET worktree_path = :wt,
                branch_name = :br,
                updated_at = NOW()
            WHERE id = :id
            """
        ),
        {"wt": wt_path, "br": branch, "id": task_id},
    )
    return Worktree(
        task_id=task_id,
        repo_root=repo_root,
        path=wt_path,
        branch=branch,
        head_sha=head_sha,
    )


async def remove(
    session: AsyncSession,
    *,
    task_id: str,
    force: bool = False,
) -> dict:
    """Tear down the worktree (and its branch) for a task, if any."""
    row = (
        await session.execute(
            text("SELECT worktree_path, branch_name FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if row is None or row[0] is None:
        return {"removed": False, "reason": "no worktree recorded"}
    wt_path, branch = row[0], row[1]
    repo_root = _infer_repo_root_from_worktree(wt_path)

    try:
        args = ["worktree", "remove", wt_path]
        if force:
            args.insert(-1, "--force")
        await _git(repo_root, *args)
    except WorktreeError as e:
        logger.warning("worktree remove failed: %s", e)

    # Best-effort delete branch (may fail if checked out elsewhere).
    if branch:
        with contextlib.suppress(WorktreeError):
            await _git(repo_root, "branch", "-D", branch)

    await session.execute(
        text(
            """
            UPDATE tasks
            SET worktree_path = NULL, branch_name = NULL, updated_at = NOW()
            WHERE id = :id
            """
        ),
        {"id": task_id},
    )
    return {"removed": True, "path": wt_path, "branch": branch}


async def commit_all(
    *,
    worktree_path: str,
    message: str,
    only_files: list[str] | None = None,
) -> str | None:
    """Stage changes in the worktree + commit. Returns git_sha (short).

    * ``only_files=None`` stages everything (``git add -A``).
    * ``only_files=[...]`` stages just those paths — critical when
      parallel wave executors share a worktree so each plan's commit
      attributes only its own writes.

    No-op (returns None) when the stage is empty.
    """
    if only_files:
        # Stage each file explicitly. Includes deletions via ``-A``
        # would be wrong here (might pick up a sibling plan's work).
        await _git(worktree_path, "add", "--", *only_files)
    else:
        await _git(worktree_path, "add", "-A")
    has_staged = await _has_staged_changes(worktree_path)
    if not has_staged:
        return None
    await _git(worktree_path, "commit", "--no-gpg-sign", "-m", message)
    return await _head_sha(worktree_path)


async def current_head(worktree_path: str) -> str:
    return await _head_sha(worktree_path) or ""


# ── Internals ──────────────────────────────────────────────────────


async def _load_task_title(
    session: AsyncSession,
    task_id: str,
) -> str | None:
    row = (
        await session.execute(
            text("SELECT title FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if row is None:
        return None
    return row[0]


async def _assert_is_git_repo(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise WorktreeError(f"target_repo_path does not exist: {path}")
    if not (p / ".git").exists():
        raise WorktreeError(
            f"target_repo_path is not a git repository: {path} (run `git init` there first)",
        )


async def _worktree_exists(repo_root: str, wt_path: str) -> bool:
    out, _ = await _git(repo_root, "worktree", "list", "--porcelain")
    wanted = str(Path(wt_path).resolve())
    for line in out.splitlines():
        if line.startswith("worktree "):
            reg = line[len("worktree ") :].strip()
            if str(Path(reg).resolve()) == wanted:
                return True
    return False


async def _branch_exists(repo_root: str, branch: str) -> bool:
    try:
        await _git(
            repo_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        )
        return True
    except WorktreeError:
        return False


async def _head_sha(path: str) -> str | None:
    try:
        out, _ = await _git(path, "rev-parse", "--short", "HEAD")
        return out.strip() or None
    except WorktreeError:
        return None


async def _has_staged_changes(path: str) -> bool:
    try:
        out, _ = await _git(path, "diff", "--cached", "--name-only")
        return bool(out.strip())
    except WorktreeError:
        return False


def _infer_repo_root_from_worktree(wt_path: str) -> str:
    """From `<repo>/.claude/worktrees/task-X`, derive `<repo>`."""
    p = Path(wt_path)
    for parent in p.parents:
        if (parent / ".git").exists():
            return str(parent)
    # Fallback: 3 levels up
    return str(p.parents[2])


async def _git(cwd: str, *args: str) -> tuple[str, str]:
    """Run a git command; return (stdout, stderr). Raise on non-zero."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise WorktreeError(
            f"git {shlex.join(args)!r} failed in {cwd} (exit {proc.returncode}): {stderr.strip()}"
        )
    return stdout, stderr


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str, *, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    return (s or "task")[:max_len]
