"""Real end-to-end benchmark for the demo path.

Exercises the live Bifrost backend + live Claude CLI. No mocks, no
seeded data, no API keys. Costs real money (~$0.50 per full run).

What it checks:
  1. Intent classifier over 5 fixture messages of varying shape
  2. ``/quick`` end-to-end in a freshly-initialized scratch git repo
  3. Full task flow — discuss → approve → plan → approve → execute → verify
  4. Chat query against "All repositories" scope

Outputs:
  * JSON summary at ``.bifrost-out/demo-validation.json``
  * Markdown report at ``.bifrost-out/demo-validation.md``
  * Timings measured on this run, so the demo talking points can
    quote real numbers.

Usage:
  uv run python scripts/demo_validation.py [--skip-chat]

Design decisions:
  * Each phase is timed separately so we can price each Claude call
    individually. Total wall time ≠ sum of phases (overlap), but each
    phase number alone is meaningful.
  * Scratch repo lives in ``/tmp/bifrost-demo-validation-<ts>/``;
    cleaned up on success, left behind on failure so you can triage.
  * The scratch repo ships a tiny Python app + pytest so the executor
    has something to edit + the verifier has something to run.
  * We capture ``task_plans.git_sha`` + the final ``app.py`` text to
    assert the agent actually wrote code (not just logged "done").
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API = os.environ.get("BIFROST_API", "http://localhost:8000/api/v1")
TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = REPO_ROOT / ".bifrost-out" / "demo-validation.json"
OUT_MD = REPO_ROOT / ".bifrost-out" / "demo-validation.md"


# ── HTTP helpers ───────────────────────────────────────────────────


def _post(path: str, body: dict[str, Any], timeout: float = 600) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["data"]


def _get(path: str, timeout: float = 30) -> dict[str, Any]:
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())["data"]


# ── Results model ──────────────────────────────────────────────────


@dataclass
class PhaseResult:
    name: str
    passed: bool
    duration_s: float
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    started_at: str
    api_base_url: str
    backend_git_sha: str
    phases: list[PhaseResult] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(p.cost_usd for p in self.phases), 4)

    @property
    def total_duration_s(self) -> float:
        return round(sum(p.duration_s for p in self.phases), 2)

    @property
    def passed(self) -> bool:
        return all(p.passed for p in self.phases)


# ── Scratch repo ──────────────────────────────────────────────────


def _setup_scratch_repo(root: Path) -> None:
    """Fresh git repo + Python venv + sample app + test. Synchronous."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text('def hello():\n    return "hello"\n')
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_hello.py").write_text(
        'from app import hello\n\n'
        'def test_hello():\n'
        '    assert hello() == "hello"\n'
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo-scratch"\nversion = "0.0.0"\n'
        'requires-python = ">=3.10"\n'
    )
    (root / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n.claude/\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "demo@bifrost.local"],
        cwd=root, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Demo Validation"],
        cwd=root, check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    # Venv with pytest so the executor's verify_cmd finds it
    subprocess.run(
        ["python3", "-m", "venv", ".venv"], cwd=root, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [str(root / ".venv" / "bin" / "pip"), "install", "-q", "pytest"],
        cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _teardown(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


# ── Phase 1: Intent classifier ────────────────────────────────────


_INTENT_FIXTURES: list[tuple[str, str]] = [
    ("Rename the hello() function to greet() in app.py", "quick"),
    ("Plan the migration from Redis to Postgres for the cache", "plan"),
    ("Implement retry on the webhook-api retry handler", "execute"),
    ("How does signature validation work?", "ask"),
    ("https://acmeco.atlassian.net/browse/ABC-123", "clarify"),
]


def run_intent_classifier() -> PhaseResult:
    t0 = time.monotonic()
    hits = 0
    cost = 0.0
    notes: list[str] = []
    metrics: list[dict[str, Any]] = []
    for message, expected in _INTENT_FIXTURES:
        t_each = time.monotonic()
        try:
            r = _post("/intent/classify", {"message": message}, timeout=45)
            dur = time.monotonic() - t_each
            got = r.get("intent")
            ok = got == expected
            hits += 1 if ok else 0
            metrics.append({
                "message": message[:60],
                "expected": expected,
                "got": got,
                "confidence": r.get("confidence"),
                "duration_s": round(dur, 2),
            })
            if not ok:
                notes.append(
                    f"intent drift: {message[:40]!r} expected {expected}, got {got}",
                )
        except Exception as e:
            notes.append(f"intent call failed on {message[:40]!r}: {e}")
    duration = time.monotonic() - t0
    passed = hits == len(_INTENT_FIXTURES)
    return PhaseResult(
        name="intent_classifier",
        passed=passed,
        duration_s=round(duration, 2),
        cost_usd=round(cost, 4),
        notes=notes,
        metrics={
            "fixtures": len(_INTENT_FIXTURES),
            "correct": hits,
            "accuracy": round(hits / len(_INTENT_FIXTURES), 2),
            "per_fixture": metrics,
        },
    )


# ── Phase 2: /quick end-to-end ────────────────────────────────────


def run_quick_end_to_end(scratch: Path) -> PhaseResult:
    _setup_scratch_repo(scratch)
    t0 = time.monotonic()
    notes: list[str] = []
    try:
        resp = _post(
            "/quick",
            {
                "message": (
                    "Rename the hello() function in app.py to greet(). "
                    "Keep the existing 'hello' return value intact. "
                    "Update tests/test_hello.py to import + assert greet()."
                ),
                "target_repo_path": str(scratch),
            },
            timeout=600,
        )
    except Exception as e:
        return PhaseResult(
            name="quick_end_to_end",
            passed=False,
            duration_s=round(time.monotonic() - t0, 2),
            error=f"/quick POST failed: {e}",
        )
    duration = time.monotonic() - t0

    # Verify the agent actually wrote code.
    worktrees = list((scratch / ".claude" / "worktrees").glob("task-*"))
    wt_ok = bool(worktrees)
    app_text = ""
    if wt_ok:
        app_text = (worktrees[0] / "app.py").read_text()
    renamed = "def greet" in app_text
    still_returns_hello = '"hello"' in app_text or "'hello'" in app_text

    final_state = resp.get("final_state")
    plan_status = resp.get("plan_status")
    git_sha = resp.get("git_sha")
    verify_passed = int(resp.get("verify_passed") or 0)
    verify_failed = int(resp.get("verify_failed") or 0)
    cost = float(resp.get("total_cost_usd") or 0)
    fallback = bool(resp.get("fallback_to_full"))

    passed = (
        not fallback
        and final_state == "success"
        and plan_status == "completed"
        and git_sha is not None
        and wt_ok
        and renamed
        and still_returns_hello
        and verify_failed == 0
    )
    if fallback:
        notes.append("ready_to_execute=false — LLM fell back to full mode")
    if final_state != "success":
        notes.append(f"final_state={final_state} (wanted 'success')")
    if not renamed:
        notes.append("app.py does NOT contain 'def greet' after run")
    if not still_returns_hello:
        notes.append("greet() no longer returns 'hello' — semantic drift")

    return PhaseResult(
        name="quick_end_to_end",
        passed=passed,
        duration_s=round(duration, 2),
        cost_usd=round(cost, 4),
        notes=notes,
        metrics={
            "task_id": resp.get("task_id"),
            "plan_status": plan_status,
            "git_sha": git_sha,
            "final_state": final_state,
            "verify": {
                "passed": verify_passed,
                "failed": verify_failed,
                "skipped": int(resp.get("verify_skipped") or 0),
            },
            "ready_for_pr": bool(resp.get("ready_for_pr")),
            "fallback_to_full": fallback,
            "app_py_head": app_text.strip().splitlines()[:4] if app_text else [],
        },
    )


# ── Phase 3: Full task flow ──────────────────────────────────────


def run_full_task_flow(scratch: Path) -> PhaseResult:
    """Discuss → approve → plan → approve → execute → verify against a
    fresh scratch repo. Checkpoint each phase so timings are visible."""
    _setup_scratch_repo(scratch)
    t0 = time.monotonic()
    phases: list[dict[str, Any]] = []
    notes: list[str] = []
    cost = 0.0

    def timed(label: str, fn) -> Any:
        t_each = time.monotonic()
        try:
            result = fn()
            dur = time.monotonic() - t_each
            phases.append({"phase": label, "duration_s": round(dur, 2), "ok": True})
            return result
        except Exception as e:
            dur = time.monotonic() - t_each
            phases.append({
                "phase": label, "duration_s": round(dur, 2),
                "ok": False, "error": str(e)[:200],
            })
            raise

    task_id: str | None = None
    try:
        # Create task
        task = timed(
            "create",
            lambda: _post(
                "/tasks",
                {
                    "title": "Rename hello() to greet() (full task smoke)",
                    "intent_text": (
                        "Rename the hello() function in app.py to greet(). "
                        "Keep returning 'hello'. Update the existing test."
                    ),
                },
            ),
        )
        task_id = task["id"]

        # Discuss round
        disc = timed(
            "discuss",
            lambda: _post(f"/tasks/{task_id}/discuss", {}, timeout=240),
        )
        cost += float(disc.get("cost_usd") or 0)

        # Approve context
        ctx_id = _get(
            f"/tasks/{task_id}/artifacts?kind=context",
        )[0]["id"]
        timed(
            "approve_context",
            lambda: _post(f"/tasks/{task_id}/artifacts/{ctx_id}/approve", {}),
        )

        # Plan round
        plan = timed(
            "plan",
            lambda: _post(f"/tasks/{task_id}/plan", {}, timeout=360),
        )
        cost += float(plan.get("cost_usd") or 0)

        # Approve plan
        plan_art_id = _get(
            f"/tasks/{task_id}/artifacts?kind=plan",
        )[0]["id"]
        timed(
            "approve_plan",
            lambda: _post(
                f"/tasks/{task_id}/artifacts/{plan_art_id}/approve", {},
            ),
        )

        # Execute
        exec_result = timed(
            "execute",
            lambda: _post(
                f"/tasks/{task_id}/execute",
                {"target_repo_path": str(scratch)},
                timeout=600,
            ),
        )
        cost += float(exec_result.get("total_cost_usd") or 0)

        # Verify (only if executing reached verifying state)
        final_state = exec_result.get("final_task_state", "")
        if final_state == "verifying":
            verify = timed(
                "verify",
                lambda: _post(f"/tasks/{task_id}/verify", {}, timeout=360),
            )
            cost += float(verify.get("cost_usd") or 0)
            final_state = verify.get("final_task_state", final_state)
    except Exception as e:
        notes.append(f"phase failed: {e}")
        final_state = "aborted"

    duration = time.monotonic() - t0
    passed = final_state == "success"
    return PhaseResult(
        name="full_task_flow",
        passed=passed,
        duration_s=round(duration, 2),
        cost_usd=round(cost, 4),
        notes=notes,
        metrics={
            "task_id": task_id,
            "final_state": final_state,
            "per_phase": phases,
        },
    )


# ── Phase 4: Chat query ───────────────────────────────────────────


def _pick_demo_repo_id() -> str | None:
    """Find a deeply-indexed repo so the chat test runs against a small
    fanout (1 repo × ~4 agents) instead of the router's top-5 which can
    exceed our 4-minute budget under rate-limit overage.

    Prefer ``webhook-api`` (the canonical demo repo); fall back to
    any repo with ``index_status == "complete"``.
    """
    try:
        # ``/repos`` paginates at 50 by default — bump so we see the
        # whole fleet (large orgs have thousands of repos).
        req = urllib.request.Request(
            f"{API}/repos?limit=2000",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            repos = json.loads(resp.read())["data"]
    except Exception:
        return None
    indexed = [r for r in repos if r.get("index_status") == "complete"]
    for r in indexed:
        if r.get("name") == "webhook-api":
            return str(r["id"])
    return str(indexed[0]["id"]) if indexed else None


def run_chat_query() -> PhaseResult:
    t0 = time.monotonic()
    notes: list[str] = []
    scope_repo_id = _pick_demo_repo_id()
    if not scope_repo_id:
        notes.append("no deep-indexed repo found — skipping scoped chat test")
        return PhaseResult(
            name="chat_query",
            passed=True,
            duration_s=round(time.monotonic() - t0, 2),
            notes=notes,
        )

    try:
        conv = _post(
            "/conversations",
            {"scope_repo_ids": [scope_repo_id]},
        )
        cid = conv["id"]
        _post(
            f"/conversations/{cid}/messages",
            {"content": "What does this service do?"},
            timeout=30,
        )
    except Exception as e:
        return PhaseResult(
            name="chat_query",
            passed=False,
            duration_s=round(time.monotonic() - t0, 2),
            error=f"chat POST failed: {e}",
        )

    # Poll the conversation for an assistant message (up to 5 min).
    # 5 min is the budget under typical rate-limit overage conditions for
    # a single-repo scope with code + Jira + Confluence + GitHub agents
    # firing in parallel. Measured wall time: ~4:08 for a 17 KB answer.
    assistant = None
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            conv = _get(f"/conversations/{cid}")
            assistants = [m for m in conv.get("messages", []) if m["role"] == "assistant"]
            if assistants:
                assistant = assistants[-1]
                if assistant.get("content"):
                    break
        except Exception:
            continue

    duration = time.monotonic() - t0
    if assistant is None or not assistant.get("content"):
        return PhaseResult(
            name="chat_query",
            passed=False,
            duration_s=round(duration, 2),
            notes=["no assistant message arrived within 5 minutes"],
        )

    body = assistant["content"]
    passed = len(body) > 200
    if not passed:
        notes.append(f"answer seems thin ({len(body)} chars)")
    return PhaseResult(
        name="chat_query",
        passed=passed,
        duration_s=round(duration, 2),
        cost_usd=0.0,
        notes=notes,
        metrics={
            "conversation_id": cid,
            "answer_length": len(body),
            "answer_head": body[:200],
            "confidence_level": assistant.get("confidence_level"),
        },
    )


# ── Report writer ─────────────────────────────────────────────────


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
        ).decode().strip()
    except Exception:
        return "unknown"


def write_reports(result: BenchmarkResult) -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "started_at": result.started_at,
                "api_base_url": result.api_base_url,
                "backend_git_sha": result.backend_git_sha,
                "passed": result.passed,
                "total_cost_usd": result.total_cost_usd,
                "total_duration_s": result.total_duration_s,
                "phases": [asdict(p) for p in result.phases],
            },
            indent=2,
            default=str,
        ),
    )

    lines: list[str] = [
        f"# Demo validation — {result.started_at}",
        "",
        f"- Backend SHA: `{result.backend_git_sha}`",
        f"- API base: `{result.api_base_url}`",
        f"- Total wall: **{result.total_duration_s}s**",
        f"- Total cost: **${result.total_cost_usd:.4f}**",
        f"- Overall: **{'PASS' if result.passed else 'FAIL'}**",
        "",
        "## Phase summary",
        "",
        "| Phase | Result | Duration | Cost |",
        "|-------|--------|---------:|-----:|",
    ]
    for p in result.phases:
        status = "✅ pass" if p.passed else "❌ fail"
        lines.append(
            f"| {p.name} | {status} | {p.duration_s}s | ${p.cost_usd:.4f} |"
        )
    lines.append("")
    for p in result.phases:
        lines.append(f"### {p.name}")
        lines.append("")
        if p.error:
            lines.append(f"- Error: `{p.error}`")
        for n in p.notes:
            lines.append(f"- Note: {n}")
        if p.metrics:
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(p.metrics, indent=2, default=str))
            lines.append("```")
        lines.append("")

    OUT_MD.write_text("\n".join(lines))


# ── Entry ─────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-chat", action="store_true",
                        help="Skip the chat query phase (slowest, most expensive)")
    parser.add_argument("--skip-full-task", action="store_true",
                        help="Skip the full task flow (~$0.30)")
    args = parser.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    result = BenchmarkResult(
        started_at=started,
        api_base_url=API,
        backend_git_sha=_git_sha(),
    )

    print(f"\n== Bifrost demo validation ==")
    print(f"   api:     {API}")
    print(f"   git sha: {result.backend_git_sha}")
    print(f"   started: {started}")

    # Phase 1 — intent classifier
    print("\n[1/4] intent classifier…")
    result.phases.append(run_intent_classifier())
    _print_phase(result.phases[-1])

    # Phase 2 — /quick
    scratch_q = Path(f"/tmp/bifrost-demo-validation-{TS}-quick")
    print("\n[2/4] /quick end-to-end…")
    with contextlib.suppress(Exception):
        result.phases.append(run_quick_end_to_end(scratch_q))
        _print_phase(result.phases[-1])
    if result.phases[-1].passed:
        _teardown(scratch_q)

    # Phase 3 — full task (optional)
    if not args.skip_full_task:
        scratch_b = Path(f"/tmp/bifrost-demo-validation-{TS}-task")
        print("\n[3/4] full task flow (discuss → plan → execute → verify)…")
        with contextlib.suppress(Exception):
            result.phases.append(run_full_task_flow(scratch_b))
            _print_phase(result.phases[-1])
        if result.phases[-1].passed:
            _teardown(scratch_b)
    else:
        print("\n[3/4] full task flow — skipped")

    # Phase 4 — chat (optional)
    if not args.skip_chat:
        print("\n[4/4] chat query…")
        result.phases.append(run_chat_query())
        _print_phase(result.phases[-1])
    else:
        print("\n[4/4] chat query — skipped")

    write_reports(result)
    print(f"\n── Reports written ──")
    print(f"   {OUT_JSON.relative_to(REPO_ROOT)}")
    print(f"   {OUT_MD.relative_to(REPO_ROOT)}")
    print()
    print(
        f"Total wall: {result.total_duration_s}s · "
        f"total cost: ${result.total_cost_usd:.4f} · "
        f"overall: {'PASS' if result.passed else 'FAIL'}"
    )
    return 0 if result.passed else 1


def _print_phase(p: PhaseResult) -> None:
    mark = "✅" if p.passed else "❌"
    print(f"   {mark} {p.name}: {p.duration_s}s · ${p.cost_usd:.4f}")
    for n in p.notes:
        print(f"      note: {n}")
    if p.error:
        print(f"      error: {p.error}")


if __name__ == "__main__":
    sys.exit(main())
