"""Per-plan executor — drives an agent through one atomic task.

One ``task_plans`` row → one Claude CLI invocation with full write
access scoped to the task's worktree, followed by an optional shell
``verify_cmd`` and an atomic commit on success.

Invariants
----------
* The Claude subprocess runs with ``cwd = worktree.path`` so every Read/
  Write/Edit/Bash tool call stays inside the worktree. We do NOT add
  the larger repo as an allowed directory — the LLM sees only what's
  in the worktree.
* ``--permission-mode bypassPermissions`` is set because the executor
  is already sandboxed behind a worktree; re-prompting the user for
  every tool call would defeat automation. verifier.py and debugger.py
  opt in too, and Bash(python *) means all three run arbitrary code
  as the Bifrost OS user. See SECURITY.md, "Task execution".
* Verify step runs AFTER the agent says it's done; failures flip the
  plan row to 'failed' WITHOUT a commit so the worktree stays
  bisect-friendly.
* Commits are atomic: every file the agent touched in this task is
  staged + committed with message ``[P<n>] <task name>``. If the agent
  made no changes, the plan still records "completed" but with
  git_sha = None.

Everything the agent emits (text, tool calls, verify stdout/stderr) is
captured into ``task_plans.output_log`` — that's what humans read
when triaging failed runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.actions._shell_safe import split_verify_cmd, validate_verify_cmd
from bifrost.actions.worktree import Worktree, commit_all
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import build_minimal_env

logger = logging.getLogger(__name__)


# ── Types ───────────────────────────────────────────────────────────


@dataclass
class PlanOutcome:
    plan_id: str
    plan_key: str
    status: str  # completed | failed
    git_sha: str | None
    commit_msg: str | None
    files_changed: list[str] = field(default_factory=list)
    tool_calls: int = 0
    agent_cost_usd: float = 0.0
    duration_ms: int = 0
    verify_passed: bool | None = None
    output_log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_EXECUTOR_SYSTEM_PROMPT = """\
You are Bifrost's executor agent. You will complete exactly ONE
atomic task in a freshly-created git worktree. Hard rules:

1. STAY IN SCOPE. Edit only the files listed under "Files". If you
   realize a different file needs touching, STOP and explain in
   your final message — do not silently expand the blast radius.
2. DO NOT SURPRISE. No refactors, no renames, no "while I'm here"
   cleanup. Just the change the task describes.
3. USE THE TOOLS YOU HAVE. Read/Glob/Grep/Edit/Write/Bash are
   enabled for THIS directory only. Keep Bash commands tightly
   scoped; never network-call or install packages unless the task
   explicitly says so.
4. NO COMMITS. Don't run git commit or git push — Bifrost commits
   for you with a canonical message after you say you're done.
5. WHEN DONE, WRITE A SHORT SUMMARY. Last assistant message should
   be 1–3 sentences describing what changed and why it satisfies
   the done-criterion. No patches, no code, no diff dumps.

__BIFROST_BOUNDARY_SENTINEL__
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


# ── Entry point ─────────────────────────────────────────────────────


async def execute_plan(
    session: AsyncSession,
    *,
    plan_id: str,
    worktree: Worktree,
    settings: BifrostSettings | None = None,
    max_turns: int = 30,
    timeout_s: int | None = None,
    commit_lock: asyncio.Lock | None = None,
) -> PlanOutcome:
    """Execute a single approved plan in the given worktree.

    ``commit_lock`` serializes the git add/commit phase across
    concurrent executors in the same worktree. When None, no locking —
    safe only if exactly one executor is active.
    """
    started = time.monotonic()
    settings = settings or get_settings()

    plan_row = (
        await session.execute(
            text(
                """
                SELECT id, plan_key, name, files, urns, action_body,
                       verify_cmd, done_criterion, status
                FROM task_plans WHERE id = :id
                """
            ),
            {"id": plan_id},
        )
    ).first()
    if plan_row is None:
        raise ValueError(f"plan {plan_id} not found")
    (
        pid_val,
        plan_key,
        name,
        files_json,
        urns_json,
        action_body,
        verify_cmd,
        done_criterion,
        current_status,
    ) = plan_row
    files = list(files_json or [])
    urns = list(urns_json or [])

    if current_status in ("completed", "executing"):
        raise ValueError(
            f"plan {plan_id} is {current_status}; refusing to re-run without /retry",
        )

    # 1. Mark executing (before we spawn — so UI can see it immediately)
    await session.execute(
        text(
            "UPDATE task_plans SET status = 'executing' WHERE id = :id",
        ),
        {"id": plan_id},
    )
    await session.commit()

    # 2. Build agent prompt
    user_prompt = _build_user_prompt(
        name=name,
        action_body=action_body,
        files=files,
        urns=urns,
        done_criterion=done_criterion,
    )

    # 3. Spawn Claude CLI
    cost_usd = 0.0
    tool_calls = 0
    final_text = ""
    spawn_output_parts: list[str] = []
    agent_files: set[str] = set()
    spawn_failed = False
    try:
        final_text, cost_usd, tool_calls, spawn_output_parts, agent_files = await _run_agent(
            cli_path=settings.claude_cli_path,
            system_prompt=_EXECUTOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=settings.agent_model,
            cwd=worktree.path,
            timeout_s=(timeout_s or min(600, settings.claude_cli_timeout_seconds)),
            max_turns=max_turns,
        )
    except Exception as exc:
        spawn_failed = True
        logger.exception("executor: agent spawn failed for plan %s", plan_id)
        spawn_output_parts.append(f"[spawn error] {exc!r}")

    # 4. Verify (if a command was specified + agent didn't already crash)
    verify_passed: bool | None = None
    verify_log: list[str] = []
    if not spawn_failed and verify_cmd:
        verify_passed, verify_out = await _run_verify(verify_cmd, worktree.path)
        verify_log.append("[verify] " + verify_cmd)
        verify_log.append(verify_out)

    # 5. Commit if everything worked. Serialize via commit_lock when
    #    the caller is running multiple plans in parallel in the same
    #    worktree (git index isn't safe for concurrent writes).
    #
    #    Commit scope: the files this agent Wrote/Edited. When the set
    #    is empty but the worktree has dirty files (Bash-only writes),
    #    fall back to ``git add -A`` so work still lands — attribution
    #    is imperfect in that case but better than losing the commit.
    git_sha: str | None = None
    commit_msg: str | None = None
    files_changed: list[str] = []
    if not spawn_failed and (verify_passed is not False):
        lock_ctx = commit_lock if commit_lock is not None else _null_lock()
        async with lock_ctx:
            dirty = await _changed_files(worktree.path)
            scoped: list[str] | None = None
            if agent_files:
                # Intersect declared-writes with actually-dirty files so
                # we don't try to add unchanged paths (git would warn but
                # still proceed — skipping keeps logs clean).
                dirty_set = set(dirty)
                scoped = sorted(p for p in agent_files if p in dirty_set)
                # If agent_files mentions paths that *aren't* dirty,
                # it's either a no-op edit or a stale tool-call echo;
                # either way we skip them.
                untracked_dirty = [p for p in dirty if p not in agent_files]
                if untracked_dirty:
                    spawn_output_parts.append(
                        "[commit] untracked-by-agent files in worktree "
                        f"(likely sibling plan or Bash write): {untracked_dirty[:8]}"
                    )
            files_changed = scoped if scoped is not None else dirty
            if files_changed:
                commit_msg = _commit_message(plan_key, name, done_criterion)
                try:
                    git_sha = await commit_all(
                        worktree_path=worktree.path,
                        message=commit_msg,
                        only_files=scoped,
                    )
                except Exception:
                    logger.exception(
                        "executor: commit failed for plan %s",
                        plan_id,
                    )
                    spawn_output_parts.append(
                        f"[commit error] see logs for plan {plan_id}",
                    )
                    spawn_failed = True

    # 6. Decide final status
    status = "completed"
    if spawn_failed or verify_passed is False:
        status = "failed"

    # 7. Compose output_log (capped + scrubbed)
    log_pieces = [
        f"=== Agent final message ===\n{final_text or '(empty)'}",
    ]
    if spawn_output_parts:
        log_pieces.append("=== Agent transcript ===\n" + "\n".join(spawn_output_parts))
    if verify_log:
        log_pieces.append("=== Verify ===\n" + "\n".join(verify_log))
    if files_changed:
        log_pieces.append("=== Files changed ===\n" + "\n".join(files_changed))
    output_log = "\n\n".join(log_pieces)[:20000]
    # The output_log captures the agent's full Bash + Read
    # transcript; if the agent accidentally echoed a token or PEM key,
    # it gets persisted in task_plans.output_log without this pass.
    output_log = _scrub_secrets_for_log(output_log)

    # 8. Persist
    now = datetime.now(UTC)
    await session.execute(
        text(
            """
            UPDATE task_plans
            SET status = :status,
                git_sha = :sha,
                commit_msg = :msg,
                output_log = :log,
                executed_at = :at
            WHERE id = :id
            """
        ),
        {
            "status": status,
            "sha": git_sha,
            "msg": commit_msg,
            "log": output_log,
            "at": now,
            "id": plan_id,
        },
    )
    await session.commit()

    duration_ms = int((time.monotonic() - started) * 1000)
    return PlanOutcome(
        plan_id=plan_id,
        plan_key=plan_key,
        status=status,
        git_sha=git_sha,
        commit_msg=commit_msg,
        files_changed=files_changed,
        tool_calls=tool_calls,
        agent_cost_usd=cost_usd,
        duration_ms=duration_ms,
        verify_passed=verify_passed,
        output_log=output_log,
    )


# ── Agent subprocess ────────────────────────────────────────────────


async def _run_agent(
    *,
    cli_path: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    cwd: str,
    timeout_s: int,
    max_turns: int,
) -> tuple[str, float, int, list[str], set[str]]:
    """Spawn `claude -p` with write tools enabled.

    Returns (final_text, cost, tool_call_count, transcript_lines,
    files_touched). ``files_touched`` is the set of repo-relative paths
    this agent called ``Write`` or ``Edit`` on — the commit phase uses
    it to stage only this plan's changes, avoiding cross-plan
    attribution when waves run concurrently.
    """
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
        # Read/Glob/Grep are wide-open and can read any file the OS user can.
        # Write/Edit are bounded by --add-dir cwd. Bash is a prefix allowlist,
        # not a sandbox: Bash(python *) runs arbitrary code. See SECURITY.md.
        "--tools",
        (
            "Read,Write,Edit,Glob,Grep,"
            "Bash(pytest *),Bash(uv *),Bash(python *),Bash(python3 *),"
            "Bash(npm test),Bash(npm run *),"
            "Bash(cargo test),Bash(cargo check),Bash(cargo build),"
            "Bash(go test *),Bash(go build *),"
            "Bash(git *),Bash(ls *),Bash(cat *)"
        ),
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        cwd,
    ]
    # Minimal env: PATH, HOME, venv and the session flag. No JIRA/DB/AWS
    # tokens are passed, but files under HOME stay readable.
    env = build_minimal_env({"CLAUDE_CODE_NO_SESSION_PERSISTENCE": "1"})
    # Find a project-local venv so Bash tool calls see `python`/`pytest`
    # wired to the right interpreter. Git worktrees don't duplicate
    # .venv (it's usually at the main repo root), so walk parents.
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
    files_touched: set[str] = set()
    try:
        while True:
            line = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=timeout_s,
            )
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
                        tool_calls += 1
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {}) or {}
                        summary = _summarize_tool(tool_name, tool_input)
                        transcript.append(f"[tool] {tool_name} {summary}")
                        # Track per-plan file authorship for scoped commit.
                        if tool_name in ("Write", "Edit", "NotebookEdit"):
                            path = _to_relpath(cwd, tool_input.get("file_path"))
                            if path:
                                files_touched.add(path)
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
        raise RuntimeError(f"executor agent timed out after {timeout_s}s")

    stderr_b = b""
    if proc.stderr is not None:
        with contextlib.suppress(TimeoutError):
            stderr_b = await asyncio.wait_for(proc.stderr.read(), timeout=3)
    rc = await proc.wait()
    if rc != 0:
        err = stderr_b.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"claude exited {rc}: {err[:800] or '(no stderr)'}",
        )

    final_text = text_buffer or "".join(final_text_parts)
    return final_text, cost_usd, tool_calls, transcript, files_touched


def _to_relpath(cwd: str, file_path: Any) -> str | None:
    """Normalize an agent tool's file_path to a cwd-relative path.

    Agents often pass absolute paths (our own system prompt gives them
    the full worktree path). We canonicalize so ``git add <path>``
    works from the worktree root, and so sibling plans don't see each
    others' paths mistakenly.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    p = Path(file_path)
    try:
        if p.is_absolute():
            return str(p.resolve().relative_to(Path(cwd).resolve()))
        return str(p)
    except ValueError:
        # Path was absolute but outside the cwd — ignore for commit.
        return None


def _summarize_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Compact one-line summary of a tool call for the transcript."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:200].replace("\n", " ")
    if tool_name in ("Write", "Edit"):
        return tool_input.get("file_path", "")
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    return json.dumps(tool_input)[:200]


# ── Verify subprocess ──────────────────────────────────────────────


async def _run_verify(
    cmd: str,
    cwd: str,
    *,
    timeout_s: int = 120,
) -> tuple[bool, str]:
    """Run the plan's verify_cmd in a shell. Returns (passed, output).

    If the worktree has a ``.venv/bin`` directory, prepend it to PATH
    so verify commands like ``pytest`` or ``python`` pick up the
    project-local interpreter without the plan having to hardcode it.
    """
    # Validate before exec. Reject metachars + require known-safe prefix.
    ok, reason = validate_verify_cmd(cmd)
    if not ok:
        return False, f"verify_cmd rejected: {reason}"

    # Minimal env (no JIRA/DB/AWS secrets even if pytest plugin
    # were compromised).
    env = build_minimal_env()
    venv = _find_project_venv(Path(cwd))
    if venv is not None:
        env["PATH"] = str(venv / "bin") + ":" + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv)
    try:
        # subprocess_exec, not subprocess_shell. The shlex.split call is
        # safe because validate_verify_cmd already rejected unbalanced quotes
        # and any shell metacharacters.
        argv = split_verify_cmd(cmd)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            out_b, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"verify timed out after {timeout_s}s"
        out = out_b.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return True, out[:4000]
        return False, (f"verify exited {proc.returncode}:\n{out[:4000]}")
    except Exception as e:
        return False, f"verify subprocess crashed: {e}"


# ── Helpers ────────────────────────────────────────────────────────


async def _changed_files(cwd: str) -> list[str]:
    """Every file git sees in the worktree that differs from HEAD."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, _ = await proc.communicate()
    files: list[str] = []
    for line in out_b.decode("utf-8", errors="replace").splitlines():
        if len(line) < 4:
            continue
        # status codes live in cols 0-1, filename starts at col 3
        name = line[3:].strip()
        # Handle "OLD -> NEW" renames: keep NEW
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name:
            files.append(name)
    return files


def _build_user_prompt(
    *,
    name: str,
    action_body: str,
    files: list[str],
    urns: list[str],
    done_criterion: str,
) -> str:
    parts: list[str] = []
    parts.append(f"Task: {name}")
    parts.append("")
    parts.append("Action:")
    # action_body is LLM-generated by PlanAgent, but PlanAgent ran
    # on a prompt that included attacker-controlled task.intent_text.
    # Treat it as untrusted data; the executor system prompt warns the
    # model to refuse directives that appear inside the boundary.
    parts.append(
        wrap_untrusted(
            action_body or "(no action body provided)",
            "action_body",
        )
    )
    parts.append("")
    if files:
        parts.append("Files (edit only these — extend only if strictly necessary):")
        for f in files:
            parts.append(f"  - {f}")
    if urns:
        parts.append("")
        parts.append("Related URNs (knowledge-graph references for context only):")
        for u in urns:
            parts.append(f"  - {u}")
    parts.append("")
    parts.append(f"Done criterion: {done_criterion}")
    parts.append("")
    parts.append(
        "When you're done, send a short 1-3 sentence summary as your "
        "FINAL message. Do NOT run git commit."
    )
    return "\n".join(parts)


from contextlib import asynccontextmanager


@asynccontextmanager
async def _null_lock():
    yield


_OUTPUT_LOG_SCANNER = None


def _scrub_secrets_for_log(text: str) -> str:
    """Run the extraction-time secret scanner on the agent's output_log
    so credentials echoed by Bash tool calls don't land in
    ``task_plans.output_log``.

    Closes a sink that bypassed the
    ``extraction/secrets.py`` regex pass.
    """
    if not text:
        return text
    global _OUTPUT_LOG_SCANNER
    if _OUTPUT_LOG_SCANNER is None:
        from bifrost.extraction.secrets import SecretScanner

        _OUTPUT_LOG_SCANNER = SecretScanner()
    result = _OUTPUT_LOG_SCANNER.scan_and_redact(
        text,
        file_path="task_plans.output_log",
    )
    return result.redacted_content


def _find_project_venv(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``.venv/bin/python``.

    Worktrees don't duplicate the venv (it's at the main repo root),
    so we search up to 6 parents. We cap the walk so we don't match
    a stray venv in ``$HOME`` or ``/tmp``.
    """
    current = start
    for _ in range(6):
        for name in (".venv", "venv"):
            venv = current / name
            if (venv / "bin" / "python").exists() or (venv / "bin" / "python3").exists():
                return venv
        if current.parent == current:
            return None
        current = current.parent
    return None


def _commit_message(plan_key: str, name: str, done_criterion: str) -> str:
    header = f"[{plan_key}] {name[:60]}"
    trailer = f"\n\n{done_criterion}\n\n🤖 bifrost.executor"
    return header + trailer
