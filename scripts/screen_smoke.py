#!/usr/bin/env python
"""Screen-smoke benchmark — hit every UI screen's backend endpoints and
fail on any 5xx.

This is the "does each screen actually work" gate. It maps the seven nav
screens (Overview / Chat / Tasks / Collaboration / Knowledge / Spend /
Learning) to the API endpoints their pages call, pulls real row IDs straight
from the DB for the parameterized ones, hits each against the running
backend, and reports a per-screen table. Exit code is non-zero if ANY
endpoint returns 5xx — so you can loop on it:

    python scripts/screen_smoke.py            # against http://localhost:8000
    BIFROST_SMOKE_BASE=http://localhost:8000 python scripts/screen_smoke.py

It found (and now guards against) real bugs: the Connections
query referencing non-existent columns (has_pr / plans_* / total_cost_usd /
message_count), the task-detail duplicate-kwarg crash, the extractions wrong-
table join, and the Learning admin gate. 4xx (auth / missing param / bad id)
is NOT a failure — only 5xx and transport errors are.

Needs the stack up (``make up`` / ``docker compose up -d``). Reads the DB via
the app's own settings, so it honors .env (port 5433 etc.).
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from sqlalchemy import text

# Import path: run from repo root (script lives in scripts/).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bifrost.api.deps import get_session_factory  # noqa: E402
from bifrost.cli.config import get_settings  # noqa: E402

BASE = os.environ.get("BIFROST_SMOKE_BASE", "http://localhost:8000") + "/api/v1"
# Local identity header (works in dev where identity.provider=header or demo mode resolves it).
HEADERS = {
    "X-Forwarded-Email": os.environ.get("BIFROST_SMOKE_EMAIL", "smoke@acmeco.example"),
}


async def _real_ids() -> dict[str, str | None]:
    """Pull one real id per entity straight from the DB for the
    parameterized endpoints (the API list may be owner-scoped/empty)."""
    out: dict[str, str | None] = {}
    sf = get_session_factory(get_settings())
    async with sf() as s:
        async def scalar(sql: str) -> str | None:
            try:
                return (await s.execute(text(sql))).scalar()
            except Exception:
                return None

        out["conv"] = await scalar(
            "SELECT id::text FROM conversations "
            "WHERE archived=FALSE AND deleted_at IS NULL "
            "ORDER BY scope_repo_ids IS NOT NULL DESC, updated_at DESC LIMIT 1"
        )
        out["task"] = await scalar("SELECT id::text FROM tasks ORDER BY updated_at DESC LIMIT 1")
        out["repo"] = await scalar("SELECT id::text FROM repos LIMIT 1")
        out["domain"] = await scalar("SELECT id::text FROM domains LIMIT 1")
        out["doc"] = await scalar("SELECT id::text FROM handover_docs LIMIT 1")
        out["session"] = await scalar("SELECT id::text FROM collab_sessions LIMIT 1")
    return out


def _screens(ids: dict[str, str | None]) -> dict[str, list[str]]:
    conv, task, repo = ids["conv"], ids["task"], ids["repo"]
    domain, doc, sess = ids["domain"], ids["doc"], ids["session"]
    s: dict[str, list[str]] = {
        "Overview": [
            "/catalog", "/catalog/summary", "/indexer/status",
            "/tasks/stats", "/costs/summary", "/health/detailed",
        ],
        "Chat": ["/conversations"],
        "Tasks": ["/tasks", "/tasks/stats"],
        "Collaboration": [
            "/collab-sessions", "/collaboration/asks", "/collaboration/inbox",
            "/collaboration/review-queue", "/collaboration/standup-draft",
            "/handover-docs", "/handover-docs/inbox",
        ],
        "Knowledge": [
            "/knowledge/repos", "/repos", "/domains", "/entities",
            "/entities/search?q=service",
        ],
        "Spend": ["/costs/summary"],
        "Learning": [
            "/admin/learning/stats", "/admin/learning/deltas",
            "/admin/learning/judgements", "/admin/learning/cache-stats",
            "/admin/learning/pending-upgrades", "/admin/benchmarks/summary",
            "/admin/benchmarks/runs", "/admin/benchmarks/questions",
        ],
    }
    if conv:
        s["Chat"] += [
            f"/conversations/{conv}", f"/conversations/{conv}/connections",
            f"/conversations/{conv}/tasks", f"/conversations/{conv}/research-status",
            f"/conversations/{conv}/settings",
        ]
    if task:
        s["Tasks"] += [
            f"/tasks/{task}", f"/tasks/{task}/artifacts", f"/tasks/{task}/phases",
            f"/tasks/{task}/plans", f"/tasks/{task}/clarifications",
            f"/tasks/{task}/ask-backs", f"/tasks/{task}/budget",
            f"/tasks/{task}/execution-status", f"/tasks/{task}/extractions",
            f"/tasks/{task}/jira-audit", f"/tasks/{task}/verify-items",
            f"/tasks/{task}/conversations",
        ]
    if repo:
        s["Knowledge"].append(f"/repos/{repo}")
    if domain:
        s["Knowledge"].append(f"/domains/{domain}")
    if doc:
        s["Collaboration"].append(f"/handover-docs/{doc}")
    if sess:
        s["Collaboration"].append(f"/collab-sessions/{sess}")
    return s


def main() -> int:
    ids = asyncio.run(_real_ids())
    screens = _screens(ids)
    client = httpx.Client(base_url=BASE, headers=HEADERS, timeout=30)
    failures: list[tuple[str, str, object]] = []
    total = 0
    for screen, paths in screens.items():
        print(f"\n=== {screen} ===")
        for p in paths:
            total += 1
            try:
                code: object = client.get(p).status_code
            except Exception as exc:  # transport error == failure
                code = f"ERR {type(exc).__name__}"
            is_fail = (isinstance(code, int) and code >= 500) or isinstance(code, str)
            if is_fail:
                failures.append((screen, p, code))
            print(f"  {code}  {p}{'   <<< FAIL' if is_fail else ''}")

    print(f"\n{'='*50}\n{total} endpoints checked across {len(screens)} screens")
    if failures:
        print(f"❌ {len(failures)} FAILED (5xx/transport):")
        for scr, p, code in failures:
            print(f"   [{scr}] {code}  {p}")
        return 1
    print("✅ all screens healthy — no 5xx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
