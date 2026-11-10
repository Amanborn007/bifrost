"""PR creation — push the task's branch and (optionally) open a PR via gh.

Triggered once a task lands in ``success``. Flow:

    1. Verify a worktree exists for the task.
    2. Assemble a PR body from context.md, plan.md, commit log,
       verify grades, and top assumptions (readable markdown).
    3. ``git push -u origin <branch>`` (unless dry_run).
    4. ``gh pr create --base <base> [--draft] --title <T> --body <B>``
       — unless ``use_gh=False`` in which case we only push.
    5. Persist ``pr_url`` + ``pr_number`` + ``pushed_at`` on the task.

Dry-run
-------
``dry_run=True`` returns the computed title + body + git commands that
would run, without invoking git/gh. Useful to preview in the UI.

Remote assumptions
------------------
We always push to ``origin``. If the repo has no ``origin`` remote
the call fails with a clear message. ``gh`` must be authenticated for
the remote host; if it isn't, the command fails and we surface the
stderr.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions.task_lifecycle import record_event

logger = logging.getLogger(__name__)


# ── Result types ────────────────────────────────────────────────────


@dataclass
class PRResult:
    task_id: str
    branch: str
    base: str
    title: str
    body: str
    pushed: bool = False
    pushed_at: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    dry_run: bool = False
    commands_run: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PRError(RuntimeError):
    pass


# ── Entry point ─────────────────────────────────────────────────────


async def create_pr(
    session: AsyncSession,
    *,
    task_id: str,
    base_branch: str = "main",
    draft: bool = False,
    use_gh: bool = True,
    dry_run: bool = False,
) -> PRResult:
    """Push the task's branch and optionally open a GitHub PR via ``gh``."""
    task = (
        await session.execute(
            text(
                """
                SELECT id, title, jira_key, worktree_path, branch_name,
                       state, intent_text
                FROM tasks WHERE id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    if task is None:
        raise ValueError(f"task {task_id} not found")
    if task[3] is None or task[4] is None:
        raise PRError(
            "task has no worktree/branch — run /execute before /create-pr",
        )

    worktree_path = task[3]
    branch = task[4]
    title = _build_title(task[1], task[2])
    body = await _build_body(
        session,
        task_id=task_id,
        task_title=task[1],
        intent_text=task[6] or "",
    )

    push_cmd = ["git", "push", "-u", "origin", branch]
    gh_cmd = [
        "gh",
        "pr",
        "create",
        "--base",
        base_branch,
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        gh_cmd.append("--draft")

    result = PRResult(
        task_id=task_id,
        branch=branch,
        base=base_branch,
        title=title,
        body=body,
        dry_run=dry_run,
        commands_run=[],
    )

    if dry_run:
        result.commands_run.append(shlex.join(push_cmd))
        if use_gh:
            # gh body contains newlines; render a collapsed preview for UI
            result.commands_run.append(_preview_cmd(gh_cmd))
        else:
            result.notes.append("use_gh=False — would push only")
        return result

    # Actual push
    try:
        await _run(push_cmd, cwd=worktree_path)
        result.pushed = True
        result.pushed_at = datetime.now(UTC).isoformat()
        result.commands_run.append(shlex.join(push_cmd))
    except PRError as e:
        raise PRError(f"git push failed: {e}") from e

    await session.execute(
        text("UPDATE tasks SET pushed_at = :at, updated_at = NOW() WHERE id = :id"),
        {"at": datetime.now(UTC), "id": task_id},
    )
    await session.commit()

    if use_gh:
        try:
            stdout, _ = await _run(gh_cmd, cwd=worktree_path)
            pr_url = _extract_pr_url(stdout)
            if pr_url:
                result.pr_url = pr_url
                result.pr_number = _parse_pr_number(pr_url)
                await session.execute(
                    text(
                        """
                        UPDATE tasks
                        SET pr_url = :url,
                            pr_number = :num,
                            updated_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "url": pr_url,
                        "num": result.pr_number,
                        "id": task_id,
                    },
                )
                await session.commit()
            result.commands_run.append("gh pr create (body omitted)")
        except PRError as e:
            # Surface the failure but keep the push result.
            result.notes.append(f"gh pr create failed: {e}")
    else:
        result.notes.append("use_gh=False — skipped gh pr create")

    await record_event(
        session,
        task_id=task_id,
        actor="user",
        action="pr_created" if result.pr_url else "branch_pushed",
        payload={
            "branch": branch,
            "base": base_branch,
            "draft": draft,
            "pr_url": result.pr_url,
            "pr_number": result.pr_number,
            "pushed": result.pushed,
        },
    )

    return result


# ── Body + title builders ───────────────────────────────────────────


_MAX_INTENT_HEAD = 400


def _build_title(task_title: str, jira_key: str | None) -> str:
    """PR title: `[JIRA] short verb-phrase task title` capped at 70 chars."""
    t = (task_title or "").strip()
    if jira_key:
        prefix = f"[{jira_key}] "
        avail = max(10, 70 - len(prefix))
        return prefix + t[:avail].rstrip()
    return t[:70].rstrip()


async def _build_body(
    session: AsyncSession,
    *,
    task_id: str,
    task_title: str,
    intent_text: str,
) -> str:
    """Compose the PR description from artifact bodies + plans + verify."""
    ctx_row = (
        await session.execute(
            text(
                """
                SELECT a.body_md, a.version
                FROM task_artifacts a
                JOIN task_phases p ON p.id = a.phase_id
                WHERE p.task_id = :b AND a.kind = 'context'
                ORDER BY a.version DESC LIMIT 1
                """
            ),
            {"b": task_id},
        )
    ).first()
    plan_row = (
        await session.execute(
            text(
                """
                SELECT a.body_md, a.version
                FROM task_artifacts a
                JOIN task_phases p ON p.id = a.phase_id
                WHERE p.task_id = :b AND a.kind = 'plan'
                ORDER BY a.version DESC LIMIT 1
                """
            ),
            {"b": task_id},
        )
    ).first()
    plans = (
        await session.execute(
            text(
                """
                SELECT pl.plan_key, pl.name, pl.git_sha, pl.status,
                       pl.files
                FROM task_plans pl
                JOIN task_phases p ON p.id = pl.phase_id
                WHERE p.task_id = :b
                ORDER BY pl.wave ASC, pl.order_index ASC
                """
            ),
            {"b": task_id},
        )
    ).all()
    verify_items = (
        await session.execute(
            text(
                """
                SELECT v.text, v.status, v.user_notes
                FROM task_verify_items v
                JOIN task_phases p ON p.id = v.phase_id
                WHERE p.task_id = :b
                ORDER BY v.order_index ASC
                """
            ),
            {"b": task_id},
        )
    ).all()
    assumptions = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (a.key) a.key, a.value, a.source_type,
                       a.tier, a.confidence
                FROM task_assumptions a
                JOIN task_artifacts art ON art.id = a.artifact_id
                JOIN task_phases p ON p.id = art.phase_id
                WHERE p.task_id = :b AND a.state = 'active'
                ORDER BY a.key, a.created_at DESC
                """
            ),
            {"b": task_id},
        )
    ).all()

    parts: list[str] = []
    parts.append(f"# {task_title}")
    parts.append("")

    # What
    parts.append("## What")
    if ctx_row and ctx_row[0]:
        goal = _extract_goal_section(ctx_row[0])
        parts.append(goal)
    else:
        parts.append(_truncate(intent_text, _MAX_INTENT_HEAD))
    parts.append("")

    # Why (intent)
    if intent_text and (not ctx_row or intent_text not in (ctx_row[0] or "")):
        parts.append("## Why")
        parts.append(_truncate(intent_text, 600))
        parts.append("")

    # How
    if plan_row and plan_row[0]:
        parts.append("## How")
        parts.append(_extract_plan_overview(plan_row[0]))
        parts.append("")

    # Commits
    committed = [p for p in plans if p[2]]
    if committed:
        parts.append("## Commits")
        for pk, name, sha, _status, files in committed:
            files_txt = ", ".join((files or [])[:4]) or "—"
            parts.append(f"- `{sha}` **[{pk}]** {name}  _({files_txt})_")
        parts.append("")

    # Verify
    if verify_items:
        parts.append("## Acceptance checks")
        for txt, status, _notes in verify_items:
            icon = _status_icon(status)
            parts.append(f"- {icon} {txt}")
        parts.append("")

    # Assumptions (trimmed — top 8 by highest tier + confidence)
    if assumptions:
        top = sorted(
            assumptions,
            key=lambda a: (a[3], -float(a[4])),  # tier asc, then -conf
        )[:8]
        parts.append("## Assumptions (active)")
        for key, val, source, tier, conf in top:
            parts.append(f"- **{key}** (T{tier} `{source}` conf {float(conf):.2f}): {val}")
        parts.append("")

    parts.append("---")
    parts.append(f"🤖 Generated by **Bifrost** · task `{task_id}`")
    return "\n".join(parts).strip() + "\n"


# ── Helpers ────────────────────────────────────────────────────────


_SECTION_RE = re.compile(r"^\s*##\s+", re.MULTILINE)


def _extract_goal_section(md: str) -> str:
    """Pull the `## Goal` section out of context.md, or first paragraph."""
    m = re.search(r"^##\s+Goal\s*\n(.+?)(?=\n##\s+|\Z)", md, re.DOTALL | re.MULTILINE)
    if m:
        return m.group(1).strip()
    # Fallback: first non-heading paragraph
    for para in md.split("\n\n"):
        s = para.strip()
        if s and not s.startswith("#"):
            return s
    return md.strip()[:600]


def _extract_plan_overview(md: str) -> str:
    """Pull `## Overview` section out of plan.md."""
    m = re.search(
        r"^##\s+Overview\s*\n(.+?)(?=\n##\s+|\Z)",
        md,
        re.DOTALL | re.MULTILINE,
    )
    if m:
        return m.group(1).strip()
    return _truncate(md, 1200)


def _status_icon(status: str) -> str:
    return {
        "pass": "✅",
        "fail": "❌",
        "skipped": "⏸️",
        "pending": "⏳",
    }.get(status, "•")


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _extract_pr_url(stdout: str) -> str | None:
    """gh pr create prints the URL on its own line; pick it up."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("https://") and ("/pull/" in line or "/pulls/" in line):
            return line
    return None


def _parse_pr_number(url: str) -> int | None:
    m = re.search(r"/pull/(\d+)\b", url)
    return int(m.group(1)) if m else None


def _preview_cmd(argv: list[str]) -> str:
    """Compact preview of a command — replaces giant --body argument."""
    out: list[str] = []
    skip = False
    for _i, a in enumerate(argv):
        if skip:
            skip = False
            out.append("<body — omitted>")
            continue
        if a == "--body":
            out.append(a)
            skip = True
            continue
        out.append(shlex.quote(a))
    return " ".join(out)


async def _run(cmd: list[str], *, cwd: str) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    stdout = out_b.decode("utf-8", errors="replace")
    stderr = err_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise PRError(
            f"{shlex.join(cmd)} exited {proc.returncode}: {stderr.strip() or stdout.strip()}"
        )
    return stdout, stderr
