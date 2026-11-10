"""Verifier agent (Layer 7) — grades task_verify_items against reality.

Runs after the executor finishes. Loads every ``task_verify_items``
row with ``status = pending`` for the task, plus context:
    * task intent + latest context.md + latest plan.md
    * commits that landed during this task's execution
    * file diffs per completed plan
    * worktree path (so the LLM can Read/Grep to verify things itself)

Asks Claude (Sonnet) to grade each item as pass / fail / skipped with
a 1–2 sentence rationale, plus an ``overall_assessment`` + a
``ready_for_pr`` flag. The task's lifecycle advances:

    all pass         → SUCCESS
    any fail         → BLOCKED
    only skipped +
    pass (no fail)   → SUCCESS

Tools
-----
Claude gets Read, Glob, Grep, Bash — **read-only by convention**. Bash
is enabled so the verifier can run the plan's ``verify_cmd`` itself
(e.g. ``pytest``, ``python -c '...'``) to double-check pass/fail. We
do NOT hand it Write / Edit — this pass is strictly evaluative.

Manual override
---------------
A user can override any one item's verdict via the API (see the
admin endpoint in tasks.py). The override flips status + writes
``user_notes``; the overall task state is re-evaluated lazily on the
next /verify or /execute call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions import tools as _tools
from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.actions.task_lifecycle import TaskState, record_event, transition
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import build_minimal_env

logger = logging.getLogger(__name__)


# ── Schema for grading output ───────────────────────────────────────


_VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "overall_assessment", "ready_for_pr"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "verdict", "rationale"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "UUID of the verify item being graded.",
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["pass", "fail", "skipped"],
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "Short human-readable reason — cite a commit, "
                            "file, or command output. Max ~2 sentences."
                        ),
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "overall_assessment": {
            "type": "string",
            "description": (
                "2-4 sentence summary: what got built, what's solid, "
                "what the human should double-check before merging."
            ),
        },
        "ready_for_pr": {
            "type": "boolean",
            "description": (
                "True when every verify item is pass or deliberately "
                "skipped — i.e. the branch is PR-ready."
            ),
        },
    },
}


_VERIFIER_SYSTEM_PROMPT = """\
You are Bifrost's verifier agent. You have Read / Glob / Grep / Bash
in a git worktree where an executor agent just made code changes. You
grade a list of ACCEPTANCE CHECKS against that worktree.

HARD RULES:

1. USE TOOLS TO VERIFY. For each check:
     * Read / Grep the files that should have changed.
     * If the plan had a verify_cmd, run it via Bash to double-check.
     * If the check says "tests pass", run the tests — don't just
       believe the executor's self-report.
2. ONE LINE OF EVIDENCE PER VERDICT. Cite the commit sha, file:line,
   or command output that backs your verdict. "Looks good" is not a
   rationale.
3. VERDICT RULES:
     * ``pass``    — You ran a tool that confirmed the check, and the
                     output is unambiguous. Examples: pytest exit 0
                     and the target test is in the "passed" list;
                     `grep` finds the expected string; `git log`
                     shows the expected commit. This is the COMMON case.
     * ``fail``    — A tool call produced evidence AGAINST the check.
                     A test failed, a file is missing, the wrong
                     value is returned, etc.
     * ``skipped`` — You were UNABLE to verify: needed dep is missing,
                     env is broken, command timed out, tool call
                     surfaced no signal either way. Skipping because
                     you're just being cautious is WRONG — if pytest
                     showed green, that's pass.
   False-pass and false-skip are both bugs. Skipped must reflect
   genuine inability to check.
4. NO WRITES. Do not use Edit or Write. Do not git commit. If Bash
   would create files, don't.
5. ``ready_for_pr`` requires every item to be ``pass`` OR ``skipped``.
   Any ``fail`` → ready_for_pr = false.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object the schema expects.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


# ── Result type ─────────────────────────────────────────────────────


@dataclass
class VerifyGrade:
    item_id: str
    verdict: str  # pass | fail | skipped
    rationale: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class VerifyRunResult:
    task_id: str
    graded: int
    passed: int
    failed: int
    skipped: int
    overall_assessment: str
    ready_for_pr: bool
    cost_usd: float
    duration_ms: int
    final_task_state: str
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ─────────────────────────────────────────────────────


async def run_verification(
    session: AsyncSession,
    *,
    task_id: str,
    settings: BifrostSettings | None = None,
) -> VerifyRunResult:
    """Grade every pending verify item for a task."""
    started = time.monotonic()
    settings = settings or get_settings()

    task = (
        await session.execute(
            text(
                """
                SELECT id, title, intent_text, state, worktree_path
                FROM tasks WHERE id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    if task is None:
        raise ValueError(f"task {task_id} not found")
    if task[4] is None:
        raise ValueError(
            "task has no worktree; run /execute first",
        )
    bstate = TaskState(task[3])
    if bstate not in (TaskState.VERIFYING, TaskState.BLOCKED, TaskState.EXECUTING):
        raise ValueError(
            f"task is {bstate.value}; nothing to verify from this state",
        )
    worktree_path = task[4]

    # Pending items
    items = (
        await session.execute(
            text(
                """
                SELECT v.id::text, v.text, v.order_index
                FROM task_verify_items v
                JOIN task_phases p ON p.id = v.phase_id
                WHERE p.task_id = :b AND v.status = 'pending'
                ORDER BY v.order_index ASC
                """
            ),
            {"b": task_id},
        )
    ).all()
    if not items:
        # Nothing pending — maybe everything is already graded. Re-evaluate.
        return await _finalize_without_llm(
            session,
            task_id=task_id,
            worktree_path=worktree_path,
            started=started,
        )

    # Context: commits + artifacts + completed plan outputs
    ctx_text = await _build_context(session, task_id=task_id, worktree_path=worktree_path)
    items_for_prompt = [{"id": r[0], "text": r[1], "order_index": r[2]} for r in items]

    # Spawn the agent
    user_prompt = _build_user_prompt(
        task_title=task[1],
        intent_text=task[2],
        context_text=ctx_text,
        items=items_for_prompt,
    )
    cost_usd = 0.0
    parsed: dict[str, Any] = {}
    transcript: list[str] = []
    try:
        parsed, cost_usd, transcript = await _run_verifier_agent(
            cli_path=settings.claude_cli_path,
            system_prompt=_VERIFIER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=settings.agent_model,
            cwd=worktree_path,
            timeout_s=min(600, settings.claude_cli_timeout_seconds),
            max_turns=40,
        )
    except Exception:
        logger.exception("verifier LLM call failed")
        raise

    # Apply grades
    grades_by_id: dict[str, VerifyGrade] = {}
    for g in parsed.get("items") or []:
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        grades_by_id[gid] = VerifyGrade(
            item_id=gid,
            verdict=str(g.get("verdict", "skipped")),
            rationale=str(g.get("rationale", ""))[:2000],
            evidence=list(g.get("evidence") or []),
        )

    graded = 0
    passed = failed = skipped = 0
    now = datetime.now(UTC)
    result_items: list[dict[str, Any]] = []
    for r in items:
        iid = r[0]
        grade = grades_by_id.get(iid)
        if grade is None:
            # Model forgot one; leave it pending + log.
            result_items.append(
                {
                    "id": iid,
                    "text": r[1],
                    "verdict": "pending",
                    "rationale": "(verifier did not return a grade)",
                }
            )
            continue
        graded += 1
        if grade.verdict == "pass":
            passed += 1
        elif grade.verdict == "fail":
            failed += 1
        else:
            skipped += 1
        notes_payload = json.dumps(
            {"rationale": grade.rationale, "evidence": grade.evidence},
        )
        await session.execute(
            text(
                """
                UPDATE task_verify_items
                SET status = :v,
                    user_notes = :notes,
                    answered_at = :at
                WHERE id = :id
                """
            ),
            {"v": grade.verdict, "notes": notes_payload, "at": now, "id": iid},
        )
        result_items.append(
            {
                "id": iid,
                "text": r[1],
                "verdict": grade.verdict,
                "rationale": grade.rationale,
                "evidence": grade.evidence,
            }
        )

    overall = str(parsed.get("overall_assessment") or "")[:4000]
    ready = bool(parsed.get("ready_for_pr") and failed == 0)

    # Lifecycle move
    final_state = await _advance_lifecycle(
        session,
        task_id=task_id,
        any_failed=failed > 0,
        ready_for_pr=ready,
    )

    # Audit
    ab_id = await _tools._insert_ask_back(
        session,
        task_id=task_id,
        phase_id=None,
        agent_kind="verify",
        question="(verification round)",
        scope={
            "graded": graded,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        answered_from="research_pipeline",
        answer_summary=overall[:800],
        research_tokens=0,  # (token totals not returned by our wrapper)
        research_cost=Decimal(str(cost_usd)),
        duration_ms=int((time.monotonic() - started) * 1000),
        answer_confidence=Decimal("0.90") if ready else Decimal("0.40"),
    )
    _ = ab_id

    await record_event(
        session,
        task_id=task_id,
        actor="agent:verify",
        action="verify_round",
        payload={
            "graded": graded,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "ready_for_pr": ready,
            "cost_usd": cost_usd,
        },
    )

    return VerifyRunResult(
        task_id=task_id,
        graded=graded,
        passed=passed,
        failed=failed,
        skipped=skipped,
        overall_assessment=overall,
        ready_for_pr=ready,
        cost_usd=cost_usd,
        duration_ms=int((time.monotonic() - started) * 1000),
        final_task_state=final_state,
        items=result_items,
    )


async def override_verify_item(
    session: AsyncSession,
    *,
    task_id: str,
    item_id: str,
    verdict: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """User forces a verify item's verdict; re-evaluates task lifecycle."""
    if verdict not in ("pass", "fail", "skipped"):
        raise ValueError(f"invalid verdict: {verdict}")
    now = datetime.now(UTC)
    notes_payload = json.dumps(
        {"rationale": notes or "user override", "evidence": [], "source": "user"},
    )
    row = (
        await session.execute(
            text(
                """
                UPDATE task_verify_items
                SET status = :v,
                    user_notes = :notes,
                    answered_at = :at
                WHERE id = :id
                RETURNING id
                """
            ),
            {"v": verdict, "notes": notes_payload, "at": now, "id": item_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"verify item {item_id} not found")

    # Re-evaluate from current row counts
    counts = (
        await session.execute(
            text(
                """
                SELECT v.status, COUNT(*)
                FROM task_verify_items v
                JOIN task_phases p ON p.id = v.phase_id
                WHERE p.task_id = :b
                GROUP BY v.status
                """
            ),
            {"b": task_id},
        )
    ).all()
    counts_map = {s: int(n) for s, n in counts}
    any_failed = counts_map.get("fail", 0) > 0
    any_pending = counts_map.get("pending", 0) > 0
    final_state = await _advance_lifecycle(
        session,
        task_id=task_id,
        any_failed=any_failed,
        ready_for_pr=(not any_failed and not any_pending),
    )
    await record_event(
        session,
        task_id=task_id,
        actor="user",
        action="verify_override",
        payload={
            "item_id": item_id,
            "verdict": verdict,
            "notes": notes,
        },
    )
    return {
        "item_id": item_id,
        "verdict": verdict,
        "task_state": final_state,
    }


# ── Helpers ────────────────────────────────────────────────────────


async def _build_context(
    session: AsyncSession,
    *,
    task_id: str,
    worktree_path: str,
) -> str:
    """Concatenate context.md + plan.md summaries + commits for the LLM."""
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
                SELECT pl.plan_key, pl.name, pl.status, pl.git_sha,
                       pl.files, pl.done_criterion
                FROM task_plans pl
                JOIN task_phases p ON p.id = pl.phase_id
                WHERE p.task_id = :b
                ORDER BY pl.wave ASC, pl.order_index ASC
                """
            ),
            {"b": task_id},
        )
    ).all()
    commits = await _git_log(worktree_path, limit=30)

    parts: list[str] = []
    if ctx_row:
        parts.append(f"## context.md (v{ctx_row[1]})")
        parts.append(ctx_row[0] or "")
    if plan_row:
        parts.append(f"\n## plan.md (v{plan_row[1]})")
        parts.append(plan_row[0] or "")
    if plans:
        parts.append("\n## Plan tasks")
        for p in plans:
            sha = p[3] or "(no commit)"
            parts.append(f"- {p[0]} [{p[2]}] {sha} — {p[1]} (files: {', '.join(p[4] or [])})")
    if commits:
        parts.append("\n## Commits on branch")
        parts.append(commits)
    return "\n".join(parts)


async def _git_log(cwd: str, *, limit: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "--oneline",
            "-n",
            str(limit),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _ = await proc.communicate()
        return out_b.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _build_user_prompt(
    *,
    task_title: str,
    intent_text: str,
    context_text: str,
    items: list[dict[str, Any]],
) -> str:
    lines: list[str] = [
        f"Task: {task_title}",
        "",
        "Original intent:",
        "```",
        intent_text or "(none)",
        "```",
        "",
        context_text,
        "",
        "## Acceptance checks to grade",
        "",
        "Return one entry in `items` per check, using its `id` verbatim.",
        "",
    ]
    for it in items:
        lines.append(f"- id: {it['id']}")
        lines.append(f"  text: {it['text']}")
    lines.append("")
    lines.append(
        "Use Read/Grep/Bash to verify each check against the worktree. "
        "For any failures, cite the file + line or command output."
    )
    return "\n".join(lines)


async def _advance_lifecycle(
    session: AsyncSession,
    *,
    task_id: str,
    any_failed: bool,
    ready_for_pr: bool,
) -> str:
    row = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if row is None:
        return ""
    current = TaskState(row[0])
    target: TaskState | None = None
    if any_failed:
        if current != TaskState.BLOCKED:
            target = TaskState.BLOCKED
    elif ready_for_pr:
        target = TaskState.SUCCESS
    if target is not None and target != current:
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=target,
                actor="agent:verify",
                reason=(
                    "verify pass — all checks satisfied"
                    if target == TaskState.SUCCESS
                    else "verify fail — checks failing"
                ),
            )
        except Exception:
            logger.exception(
                "verifier: lifecycle transition to %s failed",
                target.value,
            )
    row = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    return row[0] if row else current.value


async def _finalize_without_llm(
    session: AsyncSession,
    *,
    task_id: str,
    worktree_path: str,
    started: float,
) -> VerifyRunResult:
    """No pending items — just re-check counts and advance lifecycle."""
    counts = (
        await session.execute(
            text(
                """
                SELECT v.status, COUNT(*)
                FROM task_verify_items v
                JOIN task_phases p ON p.id = v.phase_id
                WHERE p.task_id = :b
                GROUP BY v.status
                """
            ),
            {"b": task_id},
        )
    ).all()
    counts_map = {s: int(n) for s, n in counts}
    passed = counts_map.get("pass", 0)
    failed = counts_map.get("fail", 0)
    skipped = counts_map.get("skipped", 0)
    ready = failed == 0 and (passed > 0 or skipped > 0)
    final_state = await _advance_lifecycle(
        session,
        task_id=task_id,
        any_failed=failed > 0,
        ready_for_pr=ready,
    )
    return VerifyRunResult(
        task_id=task_id,
        graded=0,
        passed=passed,
        failed=failed,
        skipped=skipped,
        overall_assessment=(
            "No pending verify items; task lifecycle re-evaluated from existing grades."
        ),
        ready_for_pr=ready,
        cost_usd=0.0,
        duration_ms=int((time.monotonic() - started) * 1000),
        final_task_state=final_state,
    )


# ── Agent subprocess ────────────────────────────────────────────────


async def _run_verifier_agent(
    *,
    cli_path: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    cwd: str,
    timeout_s: int,
    max_turns: int,
) -> tuple[dict[str, Any], float, list[str]]:
    """Spawn `claude -p` with Read/Grep/Bash; return (parsed_json, cost, transcript)."""
    argv = [
        cli_path,
        "-p",
        "--system-prompt",
        system_prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--include-partial-messages",
        "--verbose",
        "--no-session-persistence",
        "--max-turns",
        str(max_turns),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--setting-sources",
        "",
        # No Write or Edit tool, but Bash(python *) and bypassPermissions
        # still allow code execution, so this is not a read-only sandbox.
        # See SECURITY.md, "Task execution".
        "--tools",
        (
            "Read,Glob,Grep,"
            "Bash(pytest *),Bash(uv *),Bash(python *),Bash(python3 *),"
            "Bash(git status*),Bash(git log*),Bash(git diff*),Bash(git show*),"
            "Bash(ls *),Bash(cat *)"
        ),
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        cwd,
    ]
    # Minimal env (see executor.py for rationale).
    env = build_minimal_env({"CLAUDE_CODE_NO_SESSION_PERSISTENCE": "1"})
    # Pick up project-local venv if present (same logic as executor)
    from bifrost.actions.executor import _find_project_venv

    venv = _find_project_venv(Path(cwd))
    if venv is not None:
        env["PATH"] = str(venv / "bin") + ":" + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        limit=16 * 1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None

    proc.stdin.write(user_prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    final_text_parts: list[str] = []
    text_buffer = ""
    transcript: list[str] = []
    cost_usd = 0.0
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_s)
            if not line:
                break
            try:
                evt = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "assistant":
                msg = evt.get("message", {}) or {}
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        t = block.get("text") or ""
                        if t and t != text_buffer:
                            text_buffer = t
                    elif btype == "tool_use":
                        tname = block.get("name", "?")
                        tinput = block.get("input", {}) or {}
                        transcript.append(f"[tool] {tname} " + str(tinput)[:160].replace("\n", " "))
            elif etype == "result":
                if evt.get("result"):
                    final_text_parts.append(str(evt["result"]))
                cost_usd = float(evt.get("total_cost_usd", 0.0) or 0.0)
            elif etype == "stream_event":
                ev = evt.get("event", {}) or {}
                delta = ev.get("delta", {}) or {}
                if delta.get("type") == "text_delta":
                    text_buffer += delta.get("text") or ""
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"verifier agent timed out after {timeout_s}s")

    stderr_b = b""
    if proc.stderr is not None:
        with contextlib.suppress(TimeoutError):
            stderr_b = await asyncio.wait_for(proc.stderr.read(), timeout=3)
    rc = await proc.wait()
    if rc != 0:
        err = stderr_b.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude exited {rc}: {err[:800] or '(no stderr)'}")

    final_text = text_buffer or "".join(final_text_parts)
    parsed = _parse_json_permissive(final_text)
    return parsed, cost_usd, transcript


def _parse_json_permissive(s: str) -> dict[str, Any]:
    """Parse a JSON object out of a possibly-prose response.

    Tries direct parse first, then strips code fences, then extracts
    the first balanced { ... } block.
    """
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Strip ```json / ``` fences
    if s.startswith("```"):
        s2 = s.split("\n", 1)[-1]
        if s2.endswith("```"):
            s2 = s2[:-3].rstrip()
        try:
            return json.loads(s2)
        except json.JSONDecodeError:
            pass
    # Find first balanced { ... }
    start = s.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in verifier response: {s[:200]!r}")
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                chunk = s[start : i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    break
    raise ValueError(f"failed to parse JSON from verifier: {s[:200]!r}")
