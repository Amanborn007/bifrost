"""Debug helper — diagnoses a failed plan and drafts a retry prompt.

When a ``task_plans`` row lands in ``failed``, the user can ask the
debug helper to analyze it. The helper spawns a Sonnet call with
Read/Grep/Bash (read-only by discipline) scoped to the worktree, and
feeds it:

    * The plan's ``action_body``, ``verify_cmd``, ``done_criterion``.
    * The last ~10KB of ``output_log`` (agent transcript + verify output).
    * The raw ``git diff`` of files the plan touched (if any).
    * The list of files the plan *said* it would touch.

The model returns:

    * ``diagnosis``      — 1–3 sentences about what went wrong.
    * ``suggested_fix``  — concrete actions a human or the executor
                           could take next.
    * ``retry_prompt``   — a short user_message to pass into
                           ``POST /plans/:id/retry``.
    * ``retry_safe``     — False if the failure is e.g. missing
                           infra / wrong assumption (retry won't help).

The returned object is *advisory*. Nothing is written to the plan row
itself — the UI shows the suggestion and lets the human decide whether
to click "Retry with this fix." An event row records the session so
the learning loop (Layer 4) can later correlate debug-helper calls
with outcomes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.actions.task_lifecycle import record_event
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import build_minimal_env

logger = logging.getLogger(__name__)


_DEBUG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["diagnosis", "suggested_fix", "retry_prompt", "retry_safe"],
    "properties": {
        "diagnosis": {
            "type": "string",
            "description": (
                "1–3 sentences identifying the root cause, citing the "
                "specific log line / file / error that gave it away."
            ),
        },
        "suggested_fix": {
            "type": "string",
            "description": (
                "Concrete action(s) that would resolve the issue. "
                "Can be 'update the action_body to do X' or 'this "
                "task's assumption A3 is wrong — discuss round needed'."
            ),
        },
        "retry_prompt": {
            "type": "string",
            "description": (
                "Short message to pass as user_message on the retry. "
                "1–2 sentences of hint; no need to restate the plan."
            ),
        },
        "retry_safe": {
            "type": "boolean",
            "description": (
                "True when an executor retry with retry_prompt has a "
                "plausible chance of succeeding. False when the "
                "failure is outside the executor's control (missing "
                "infra, broken assumption, needs human)."
            ),
        },
    },
}


_DEBUG_SYSTEM_PROMPT = """\
You are Bifrost's debug helper. A single executor task failed. You
have Read / Glob / Grep / Bash tools scoped to the worktree. Your job
is to diagnose WHY the task failed in one short pass — not to fix it
yourself.

Rules:

1. START FROM THE LOG. The task's own transcript + verify output are
   in the prompt. Use tools only to confirm hypotheses.
2. CITE SPECIFICS. "Tests failed" isn't a diagnosis. "tests/test_x.py
   line 14 expected 200 got 422 because route missing trailing slash"
   is.
3. DON'T WRITE. Read / Grep / Bash (read-only commands only). Do not
   Edit, Write, run `git commit`, or create files.
4. BE HONEST ABOUT ``retry_safe``. If you can't tell a plausible
   retry that would succeed with a 2-sentence hint, say False.
   Failing that honestly is better than sending the executor into a
   doomed loop.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object the schema expects.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


@dataclass
class DebugReport:
    task_id: str
    plan_id: str
    plan_key: str
    diagnosis: str
    suggested_fix: str
    retry_prompt: str
    retry_safe: bool
    duration_ms: int
    cost_usd: float
    tool_calls: int = 0
    transcript: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_debug(
    session: AsyncSession,
    *,
    plan_id: str,
    settings: BifrostSettings | None = None,
) -> DebugReport:
    """Diagnose a failed plan; returns advisory structured report."""
    started = time.monotonic()
    settings = settings or get_settings()

    plan = (
        await session.execute(
            text(
                """
                SELECT p.id, p.plan_key, p.name, p.files, p.action_body,
                       p.verify_cmd, p.done_criterion, p.status, p.output_log,
                       ph.task_id::text, b.worktree_path, b.branch_name
                FROM task_plans p
                JOIN task_phases ph ON ph.id = p.phase_id
                JOIN tasks b ON b.id = ph.task_id
                WHERE p.id = :id
                """
            ),
            {"id": plan_id},
        )
    ).first()
    if plan is None:
        raise ValueError(f"plan {plan_id} not found")
    if plan[10] is None:
        raise ValueError(
            "task has no worktree — cannot debug without one",
        )
    worktree = plan[10]
    diff = await _git_diff(worktree, files=list(plan[3] or []))

    user_prompt = _build_prompt(
        plan_key=plan[1],
        name=plan[2],
        files=list(plan[3] or []),
        action_body=plan[4] or "",
        verify_cmd=plan[5],
        done_criterion=plan[6] or "",
        output_log=plan[8] or "",
        diff=diff,
    )

    parsed, cost_usd, tool_calls, transcript = await _run_debug_agent(
        cli_path=settings.claude_cli_path,
        system_prompt=_DEBUG_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=settings.agent_model,
        cwd=worktree,
        timeout_s=min(360, settings.claude_cli_timeout_seconds),
        max_turns=20,
    )

    report = DebugReport(
        task_id=plan[9],
        plan_id=str(plan[0]),
        plan_key=plan[1],
        diagnosis=str(parsed.get("diagnosis") or "")[:2000],
        suggested_fix=str(parsed.get("suggested_fix") or "")[:2000],
        retry_prompt=str(parsed.get("retry_prompt") or "")[:1000],
        retry_safe=bool(parsed.get("retry_safe", False)),
        duration_ms=int((time.monotonic() - started) * 1000),
        cost_usd=cost_usd,
        tool_calls=tool_calls,
        transcript=transcript,
    )

    await record_event(
        session,
        task_id=plan[9],
        actor="agent:debug",
        action="debug_round",
        payload={
            "plan_id": str(plan[0]),
            "plan_key": plan[1],
            "retry_safe": report.retry_safe,
            "diagnosis": report.diagnosis[:400],
            "cost_usd": cost_usd,
            "duration_ms": report.duration_ms,
        },
    )
    return report


# ── Helpers ────────────────────────────────────────────────────────


def _build_prompt(
    *,
    plan_key: str,
    name: str,
    files: list[str],
    action_body: str,
    verify_cmd: str | None,
    done_criterion: str,
    output_log: str,
    diff: str,
) -> str:
    parts: list[str] = [
        f"Failed plan: {plan_key}",
        f"Name: {name}",
        f"Done criterion: {done_criterion}",
    ]
    if verify_cmd:
        parts.append(f"Verify cmd that was run: `{verify_cmd}`")
    if files:
        parts.append("Files the plan said it would touch:")
        for f in files:
            parts.append(f"  - {f}")
    parts.append("")
    parts.append("## action_body")
    parts.append(action_body or "(empty)")
    parts.append("")
    parts.append("## output_log (last 10KB)")
    parts.append("```")
    parts.append(_tail(output_log, 10_000))
    parts.append("```")
    if diff:
        parts.append("")
        parts.append("## git diff of declared files")
        parts.append("```diff")
        parts.append(_tail(diff, 6_000))
        parts.append("```")
    parts.append("")
    parts.append("Diagnose. Use Read/Grep/Bash to check hypotheses. Return JSON.")
    return "\n".join(parts)


async def _git_diff(cwd: str, *, files: list[str]) -> str:
    """Return `git diff <files>` (or full diff if no files)."""
    argv = ["git", "diff", "HEAD"]
    if files:
        argv.append("--")
        argv.extend(files)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _ = await proc.communicate()
        return out_b.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _tail(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return "…\n" + s[-n:]


async def _run_debug_agent(
    *,
    cli_path: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    cwd: str,
    timeout_s: int,
    max_turns: int,
) -> tuple[dict[str, Any], float, int, list[str]]:
    """Spawn claude -p and parse JSON out of the final message."""
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
        # Diagnostic with the verifier's tool list. Not read-only: Bash(python *) runs code.
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
    # Minimal env (see executor.py).
    env = build_minimal_env({"CLAUDE_CODE_NO_SESSION_PERSISTENCE": "1"})
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
    tool_calls = 0
    cost_usd = 0.0
    transcript: list[str] = []
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
                for block in (evt.get("message", {}) or {}).get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        t = block.get("text") or ""
                        if t and t != text_buffer:
                            text_buffer = t
                    elif bt == "tool_use":
                        tool_calls += 1
                        transcript.append(
                            f"[{block.get('name', '?')}] "
                            + str(block.get("input", {}))[:140].replace("\n", " ")
                        )
            elif etype == "result":
                if evt.get("result"):
                    final_text_parts.append(str(evt["result"]))
                cost_usd = float(evt.get("total_cost_usd", 0.0) or 0.0)
            elif etype == "stream_event":
                ev = evt.get("event", {}) or {}
                d = ev.get("delta", {}) or {}
                if d.get("type") == "text_delta":
                    text_buffer += d.get("text") or ""
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"debug agent timed out after {timeout_s}s")

    stderr_b = b""
    if proc.stderr is not None:
        with contextlib.suppress(asyncio.TimeoutError):
            stderr_b = await asyncio.wait_for(proc.stderr.read(), timeout=3)
    rc = await proc.wait()
    if rc != 0:
        err = stderr_b.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude exited {rc}: {err[:800] or '(no stderr)'}")

    final = text_buffer or "".join(final_text_parts)
    return _parse_json_permissive(final), cost_usd, tool_calls, transcript


def _parse_json_permissive(s: str) -> dict[str, Any]:
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if s.startswith("```"):
        body = s.split("\n", 1)[-1]
        if body.endswith("```"):
            body = body[:-3].rstrip()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
    start = s.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in debug response: {s[:200]!r}")
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    break
    raise ValueError(f"failed to parse JSON from debug agent: {s[:200]!r}")
