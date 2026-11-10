"""Code Review Queue — "what PRs need MY attention right now".

Aggregates open PRs from two sources:

  1. Bifrost tasks where ``pr_url`` is set and the task is in
     ``success`` (PR open, awaiting review). These are the PRs the
     operator's own tasks have raised.
  2. The cached-per-conversation ``GitHubReport.recent_prs`` rows
     emitted by the per-source GitHub research agent across all
     conversations the operator has been part of. Approximate but
     low-cost (no live ``gh`` calls in the request path).

The ranking heuristic:
  • PRs from the operator's OWN tasks float to the top — those are
    the ones they care most about getting reviewed.
  • Then PRs whose repo the operator has chat conversation history
    with (proxy: "I've been thinking about this codebase").
  • Then PRs in repos the operator's email is squad-linked to (via
    Identity groups → squad mapping is future work).

Output is a flat ranked list — the FE renders sections by source.
Live ``gh pr view`` is intentionally NOT called here (60s observer
already polls task PRs; per-request live calls would add 5–30s of
latency to the page load).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class ReviewItem:
    pr_url: str
    pr_number: int | None = None
    title: str = ""
    state: str = "open"  # open | merged | closed
    author: str | None = None
    repo_name: str | None = None
    rationale: str = ""  # why this is in the queue
    source: str = ""  # "my_task" | "chat_history" | "squad"
    task_id: str | None = None
    task_title: str | None = None
    task_state: str | None = None
    last_seen_at: str = ""


@dataclass
class ReviewQueue:
    email: str
    items: list[ReviewItem] = field(default_factory=list)
    generated_at: str = ""
    note: str = ""


async def build_review_queue(
    session: AsyncSession,
    *,
    email: str,
    range_days: int = 30,
) -> ReviewQueue:
    """Pull open PRs in scope of the operator's recent activity.

    ``range_days`` bounds the lookback for chat-history-derived PRs;
    task-PRs are not bounded (they're explicitly mine)."""
    items: list[ReviewItem] = []
    seen_urls: set[str] = set()

    # ── 1) PRs from MY tasks in `success` ───────────────────────────
    my_task_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text AS id, title, state, pr_url, pr_number,
                       updated_at
                FROM tasks
                WHERE owner_email = :email
                  AND state = 'success'
                  AND pr_url IS NOT NULL
                ORDER BY updated_at DESC
                """
                ),
                {"email": email},
            )
        )
        .mappings()
        .all()
    )
    for r in my_task_rows:
        url = r["pr_url"]
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            ReviewItem(
                pr_url=url,
                pr_number=int(r["pr_number"]) if r.get("pr_number") else None,
                title=str(r.get("title") or "")[:140],
                state="open",
                author=email,
                rationale="From your task — awaiting review",
                source="my_task",
                task_id=str(r["id"]),
                task_title=str(r.get("title") or ""),
                task_state=str(r.get("state") or ""),
                last_seen_at=(r["updated_at"].isoformat() if r.get("updated_at") else ""),
            ),
        )

    # ── 2) PRs from chat conversations I've been on ────────────────
    # The GitHubReport is stored in agent_results.output_json under
    # agent_kind='github'. Walk recent runs where the conv's owner_email
    # matches and pluck recent_prs. This is approximate but free.
    since = datetime.utcnow() - timedelta(days=range_days)
    chat_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT ar.output_json, ar.created_at, r.name AS repo_name
                FROM agent_results ar
                JOIN conversations c ON c.id = ar.conversation_id
                LEFT JOIN repos r ON r.id::text = ar.target_id
                WHERE ar.agent_kind = 'github'
                  AND ar.created_at > :since
                  AND c.owner_email = :email
                  AND ar.status = 'success'
                ORDER BY ar.created_at DESC
                LIMIT 200
                """
                ),
                {"email": email, "since": since},
            )
        )
        .mappings()
        .all()
    )

    for r in chat_rows:
        out = r.get("output_json") or {}
        repo_name = r.get("repo_name")
        recent_prs = out.get("recent_prs") if isinstance(out, dict) else None
        if not isinstance(recent_prs, list):
            continue
        for pr in recent_prs:
            if not isinstance(pr, dict):
                continue
            url = pr.get("url")
            if not url or url in seen_urls:
                continue
            state = str(pr.get("state") or "").lower()
            if state and state != "open":
                continue  # only surface open PRs
            seen_urls.add(url)
            items.append(
                ReviewItem(
                    pr_url=str(url),
                    pr_number=(int(pr.get("number")) if pr.get("number") else None),
                    title=str(pr.get("title") or "")[:140],
                    state="open",
                    author=str(pr.get("author") or "") or None,
                    repo_name=repo_name,
                    rationale=("From a repo you've been researching"),
                    source="chat_history",
                    last_seen_at=(r["created_at"].isoformat() if r.get("created_at") else ""),
                ),
            )
            if len(items) >= 100:
                break
        if len(items) >= 100:
            break

    # ── 3 + 4) Live PRs via `gh` CLI ──────────────────────────────
    # Runs two gh invocations: review-requested + authored.
    # Both are gated on a single auth check so we only emit the
    # warning once and only attempt the gh calls if auth is working.
    import asyncio as _asyncio
    import json as _json

    _gh_authed = False
    _gh_note_suffix = ""

    async def _gh_run(*args: str) -> tuple[int, bytes, bytes]:
        proc = await _asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await _asyncio.wait_for(
                proc.communicate(),
                timeout=15.0,
            )
            return proc.returncode or 0, stdout, stderr
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return -1, b"", b"timeout"

    # Auth check — fast, no network needed
    auth_code, _, auth_err = await _gh_run("auth", "status")
    if auth_code == 0:
        _gh_authed = True
    else:
        err_text = auth_err.decode("utf-8", errors="replace").strip()
        auth_signal = (
            "not logged in" in err_text.lower()
            or "no token" in err_text.lower()
            or "token" in err_text.lower()
        )
        if auth_signal:
            logger.warning(
                "review_queue.gh_not_authenticated: live PR fetch is inactive. "
                "Set GH_TOKEN or GITHUB_TOKEN, or run `gh auth login` on the "
                "Bifrost host. Falling back to task/chat-history sources."
            )
            _gh_note_suffix = (
                " gh is not authenticated — live PR fetch inactive. "
                "Set GH_TOKEN or run `gh auth login`."
            )
        else:
            logger.warning(
                "review_queue.gh_check_failed exit_code=%s stderr=%s",
                auth_code,
                err_text[:200],
            )
            _gh_note_suffix = " gh check failed — live PR fetch skipped."

    if _gh_authed:
        # Source 3: review-requested
        try:
            code, stdout, _ = await _gh_run(
                "pr",
                "list",
                "--review-requested",
                "@me",
                "--state",
                "open",
                "--json",
                "number,title,url,author,updatedAt,headRepository",
                "--limit",
                "30",
            )
            if code == 0 and stdout:
                for pr in _json.loads(stdout.decode()):
                    url = pr.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    items.append(
                        ReviewItem(
                            pr_url=url,
                            pr_number=int(pr["number"]) if pr.get("number") else None,
                            title=str(pr.get("title", ""))[:140],
                            state="open",
                            author=str((pr.get("author") or {}).get("login", "")),
                            repo_name=(pr.get("headRepository") or {}).get("name"),
                            rationale="Review requested from you",
                            source="review_requested",
                            last_seen_at=str(pr.get("updatedAt", "")),
                        )
                    )
        except Exception as exc:
            logger.warning("review_queue.gh_review_requested_failed: %s", exc)

        # Source 4: my authored PRs
        try:
            code, stdout, _ = await _gh_run(
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "open",
                "--json",
                "number,title,url,reviewDecision,updatedAt,headRepository",
                "--limit",
                "20",
            )
            if code == 0 and stdout:
                for pr in _json.loads(stdout.decode()):
                    url = pr.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    items.append(
                        ReviewItem(
                            pr_url=url,
                            pr_number=int(pr["number"]) if pr.get("number") else None,
                            title=str(pr.get("title", ""))[:140],
                            state="open",
                            author=email,
                            repo_name=(pr.get("headRepository") or {}).get("name"),
                            rationale="Your PR awaiting review",
                            source="my_authored_pr",
                            last_seen_at=str(pr.get("updatedAt", "")),
                        )
                    )
        except Exception as exc:
            logger.warning("review_queue.gh_authored_failed: %s", exc)

    return ReviewQueue(
        email=email,
        items=items,
        generated_at=datetime.now(UTC).isoformat(),
        note=("Live PR state updated every 60s by the PR observer." + _gh_note_suffix),
    )
