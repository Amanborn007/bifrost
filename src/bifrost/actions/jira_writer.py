"""Jira write adapter — wraps Atlassian MCP tools for Bifrost's task
lifecycle.

Why this lives behind a ``claude -p`` subprocess:
  Bifrost's Atlassian access today goes through the user's MCP-
  authenticated CLI session. Calling Atlassian REST directly would
  require capturing + storing the OAuth token (a sensitive surface
  Bifrost has deliberately avoided). Spawning a tightly-scoped
  ``claude -p`` invocation reuses the auth the user already has
  (``claude auth login`` → MCP servers configured) and keeps secrets out
  of Bifrost's process.

The trade-off is latency (~3-5s per write) and cost (~$0.005). For a
write loop that fires 4-5 times across a task lifecycle, that's
~$0.025 and ~20s additional latency per task — acceptable in
exchange for not storing OAuth tokens.

Three operations:

  * ``create_issue`` — POST to project, return new key
  * ``transition_issue`` — move ticket to a target workflow state
  * ``add_comment`` — append a comment

Every call writes a row to ``task_jira_audit`` BEFORE returning so a
failed write is visible in the operator UI immediately.

Failure modes (intentional):
  * MCP not configured → ``JiraWriteSkipped`` raised; caller logs a
    warning + moves on. Task stays local-only; user can re-sync.
  * MCP rate-limited / 5xx → audit row written with ``ok=false`` +
    ``error_message``; caller decides whether to retry.
  * Wrong project / forbidden → audit row written with ``ok=false``;
    surfaced via the FE banner.

NOTHING in this module catches exceptions silently — if a write
fails, the audit row records it and the caller sees the error.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import JiraSourceConfig

logger = logging.getLogger(__name__)


class JiraWriteSkipped(Exception):
    """Raised when no Atlassian MCP path is available — operator
    needs to ``claude auth login`` or set up MCP. Caller treats as
    recoverable: task progresses without a Jira side-effect."""


class JiraWriteFailed(Exception):
    """Raised when the write was attempted but failed at the MCP /
    API layer. Audit row IS written before this is raised so the
    failure is observable."""


@dataclass
class JiraIssueCreate:
    project_key: str
    summary: str
    description: str
    issue_type: str = "Task"  # Task | Story | Bug | Sub-task
    labels: list[str] = field(default_factory=list)


@dataclass
class JiraIssueResult:
    key: str  # e.g. "SHIP-1234"
    url: str  # empty when neither the tool nor sources.jira.base_url supplies one
    raw: dict[str, Any]


# ── audit helpers ────────────────────────────────────────────────────


async def _write_audit(
    session: AsyncSession,
    *,
    task_id: str,
    action: str,
    jira_key: str | None,
    payload: dict[str, Any],
    response: dict[str, Any] | None,
    ok: bool,
    error_message: str | None,
    actor: str,
    before_state: str | None = None,
    after_state: str | None = None,
    jira_state_before: str | None = None,
    jira_state_after: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO task_jira_audit (
                task_id, jira_key, action, before_state, after_state,
                jira_state_before, jira_state_after,
                payload, mcp_response, ok, error_message, actor
            )
            VALUES (
                :tid, :jkey, :action, :bs, :as,
                :jsb, :jsa,
                CAST(:pl AS JSONB), CAST(:resp AS JSONB),
                :ok, :err, :actor
            )
            """
        ),
        {
            "tid": task_id,
            "jkey": jira_key,
            "action": action,
            "bs": before_state,
            "as": after_state,
            "jsb": jira_state_before,
            "jsa": jira_state_after,
            "pl": json.dumps(payload, default=str),
            "resp": json.dumps(response or {}, default=str),
            "ok": ok,
            "err": (error_message or "")[:1000] or None,
            "actor": actor,
        },
    )


def _canonical_url(key: str, url: str, jira: JiraSourceConfig) -> str:
    return url or jira.browse_url(key) or ""


# ── claude-p driver ─────────────────────────────────────────────────


_JIRA_WRITE_TOOL_NAMES = (
    "getAccessibleAtlassianResources",
    "createJiraIssue",
    "transitionJiraIssue",
    "getTransitionsForJiraIssue",
    "addCommentToJiraIssue",
    "getJiraIssue",
)

_JIRA_WRITE_SYSTEM = """\
You are a deterministic Jira write executor. You receive ONE specific
write request and MUST execute it via the Atlassian MCP tool exactly
as instructed. Do not interpret, do not add fields, do not retry on
your own.

Available tools:
  * {mcp:atlassian:createJiraIssue}
  * {mcp:atlassian:transitionJiraIssue}
  * {mcp:atlassian:getTransitionsForJiraIssue}
  * {mcp:atlassian:addCommentToJiraIssue}
  * {mcp:atlassian:getAccessibleAtlassianResources}

Steps:
  1. Call ``getAccessibleAtlassianResources`` to find the cloudId.
  2. Execute the requested write.
  3. Return ONLY a JSON object matching the schema below. No prose.

If the tool returns an error, report it verbatim in ``error`` and set
``ok=false``. Never lie about success.
"""


_JIRA_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok"],
    "properties": {
        "ok": {"type": "boolean"},
        "key": {"type": "string"},
        "url": {"type": "string"},
        "transition_id": {"type": "string"},
        "jira_state_before": {"type": "string"},
        "jira_state_after": {"type": "string"},
        "comment_id": {"type": "string"},
        "error": {"type": "string"},
        "raw": {"type": "object"},
    },
}


async def _run_jira_write(
    *,
    user_message: str,
    settings: BifrostSettings | None = None,
) -> dict[str, Any]:
    """Spawn ``claude -p`` with the deterministic Jira-write prompt
    and return the parsed JSON result. Raises ``JiraWriteSkipped`` on
    auth/MCP-missing failures (recoverable) or ``JiraWriteFailed`` on
    other errors."""
    settings = settings or get_settings()
    from bifrost.extraction.cli_client import (
        ClaudeCliError,
        ClaudeLlmClient,
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_dynamic_max_turns(
            initial_max_turns=4,
            timeout_override=120,
            system_prompt=settings.mcp.render(_JIRA_WRITE_SYSTEM),
            user_message=user_message,
            model=settings.triage_model,
            json_schema=_JIRA_WRITE_SCHEMA,
            allowed_tools=settings.mcp.tools("atlassian", _JIRA_WRITE_TOOL_NAMES),
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        msg = str(exc).lower()
        if "auth" in msg or "not authenticated" in msg:
            raise JiraWriteSkipped(
                f"Claude CLI not authenticated for Atlassian MCP: {exc}",
            ) from exc
        raise JiraWriteFailed(f"claude -p subprocess failed: {exc}") from exc

    parsed = completion.parsed or {}
    return parsed


# ── public API ──────────────────────────────────────────────────────


async def create_issue(
    session: AsyncSession,
    *,
    task_id: str,
    body: JiraIssueCreate,
    actor: str = "system",
) -> JiraIssueResult:
    """Create a new Jira issue and stamp ``tasks.jira_key``.

    Always writes an audit row. Raises ``JiraWriteSkipped`` (caller
    catches and degrades gracefully) or ``JiraWriteFailed`` (caller
    surfaces error to operator)."""
    user_msg = (
        "Create a Jira issue with the following fields. Return only "
        "the schema JSON.\n\n"
        f"project_key: {body.project_key}\n"
        f"issue_type: {body.issue_type}\n"
        f"summary: {body.summary}\n"
        f"labels: {', '.join(body.labels) if body.labels else '(none)'}\n\n"
        "description (verbatim):\n"
        f"{body.description}\n"
    )
    payload = {
        "project_key": body.project_key,
        "issue_type": body.issue_type,
        "summary": body.summary,
        "labels": list(body.labels),
    }
    try:
        out = await _run_jira_write(user_message=user_msg)
    except JiraWriteSkipped:
        raise
    except Exception as exc:  # noqa: BLE001
        await _write_audit(
            session,
            task_id=task_id,
            action="create",
            jira_key=None,
            payload=payload,
            response=None,
            ok=False,
            error_message=str(exc),
            actor=actor,
        )
        raise JiraWriteFailed(str(exc)) from exc

    if not out.get("ok"):
        err = str(out.get("error") or "MCP returned ok=false")
        await _write_audit(
            session,
            task_id=task_id,
            action="create",
            jira_key=None,
            payload=payload,
            response=out,
            ok=False,
            error_message=err,
            actor=actor,
        )
        raise JiraWriteFailed(err)

    key = str(out.get("key") or "").strip()
    url = str(out.get("url") or "").strip()
    if not key:
        # Try to recover a key from the raw payload — different MCP
        # versions surface different fields.
        raw = out.get("raw") or {}
        for cand in (raw.get("key"), raw.get("id")):
            if isinstance(cand, str) and "-" in cand:
                key = cand
                break
    url = _canonical_url(key, url, get_settings().sources.jira)

    await _write_audit(
        session,
        task_id=task_id,
        action="create",
        jira_key=key or None,
        payload=payload,
        response=out,
        ok=bool(key),
        error_message=None if key else "MCP did not return a key",
        actor=actor,
    )
    if not key:
        raise JiraWriteFailed("Jira create succeeded but no key was returned")

    # Stamp the task with the new key.
    await session.execute(
        text("UPDATE tasks SET jira_key = :k, updated_at = NOW() WHERE id = :id"),
        {"k": key, "id": task_id},
    )
    return JiraIssueResult(key=key, url=url, raw=out.get("raw") or {})


async def transition_issue(
    session: AsyncSession,
    *,
    task_id: str,
    jira_key: str,
    target_state: str,
    actor: str = "system",
    before_state: str | None = None,
    after_state: str | None = None,
) -> dict[str, Any]:
    """Transition an existing Jira issue to ``target_state``.

    ``target_state`` is the WORKFLOW state name, not the Bifrost task
    state — the caller is responsible for mapping (see
    ``jira_state_map.py``). The MCP call itself does the
    name→transition_id lookup via ``getTransitionsForJiraIssue``.
    """
    user_msg = (
        "Transition a Jira issue to a target workflow state. Return "
        "only the schema JSON.\n\n"
        f"jira_key: {jira_key}\n"
        f"target_state_name: {target_state}\n\n"
        "Steps:\n"
        "  1. Call getTransitionsForJiraIssue to list available "
        "transitions.\n"
        "  2. Find the transition whose ``to.name`` (case-insensitive) "
        "matches the target_state_name. If none matches exactly, pick "
        "the closest synonym (e.g. 'In Progress' ≈ 'Started' / "
        "'Doing'); document the choice in ``raw``.\n"
        "  3. Call transitionJiraIssue with that transition id.\n"
        "  4. Return ok=true with key, transition_id, "
        "jira_state_before, jira_state_after.\n"
        "  5. If no transition is available (the workflow is locked or "
        "the target state isn't reachable from the current state), "
        "set ok=false with a clear error string.\n"
    )
    payload = {"jira_key": jira_key, "target_state": target_state}
    try:
        out = await _run_jira_write(user_message=user_msg)
    except JiraWriteSkipped:
        raise
    except Exception as exc:  # noqa: BLE001
        await _write_audit(
            session,
            task_id=task_id,
            action="transition",
            jira_key=jira_key,
            payload=payload,
            response=None,
            ok=False,
            error_message=str(exc),
            actor=actor,
            before_state=before_state,
            after_state=after_state,
        )
        raise JiraWriteFailed(str(exc)) from exc

    ok = bool(out.get("ok"))
    err = None if ok else str(out.get("error") or "transition failed")
    await _write_audit(
        session,
        task_id=task_id,
        action="transition",
        jira_key=jira_key,
        payload=payload,
        response=out,
        ok=ok,
        error_message=err,
        actor=actor,
        before_state=before_state,
        after_state=after_state,
        jira_state_before=str(out.get("jira_state_before") or "") or None,
        jira_state_after=str(out.get("jira_state_after") or "") or None,
    )
    if not ok:
        raise JiraWriteFailed(err or "transition failed")
    return out


async def add_comment(
    session: AsyncSession,
    *,
    task_id: str,
    jira_key: str,
    body: str,
    actor: str = "system",
) -> dict[str, Any]:
    """Append a comment to a Jira issue. Body is markdown."""
    user_msg = (
        "Add a comment to a Jira issue. Return only the schema JSON.\n\n"
        f"jira_key: {jira_key}\n"
        "comment_body (verbatim, markdown):\n"
        f"{body}\n"
    )
    payload = {"jira_key": jira_key, "body_len": len(body)}
    try:
        out = await _run_jira_write(user_message=user_msg)
    except JiraWriteSkipped:
        raise
    except Exception as exc:  # noqa: BLE001
        await _write_audit(
            session,
            task_id=task_id,
            action="comment",
            jira_key=jira_key,
            payload=payload,
            response=None,
            ok=False,
            error_message=str(exc),
            actor=actor,
        )
        raise JiraWriteFailed(str(exc)) from exc

    ok = bool(out.get("ok"))
    await _write_audit(
        session,
        task_id=task_id,
        action="comment",
        jira_key=jira_key,
        payload={**payload, "comment_id": out.get("comment_id")},
        response=out,
        ok=ok,
        error_message=None if ok else str(out.get("error") or "comment failed"),
        actor=actor,
    )
    if not ok:
        raise JiraWriteFailed(str(out.get("error") or "comment failed"))
    return out


# ── retract: reverse a prior write ──────────────────────────────────


async def retract_audit(
    session: AsyncSession,
    *,
    audit_id: str,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Reverse a prior Jira write by posting a retraction comment.

    We do NOT delete the Jira issue or undo a transition by force —
    that's irreversible from Atlassian's side. Instead we post a
    ``[bifrost-retracted]`` comment and mark our audit row so the FE
    surfaces the retraction. For transitions, the operator can move
    the ticket back manually; for creates, the operator can close the
    issue from Jira UI.
    """
    row = (
        await session.execute(
            text(
                "SELECT task_id, jira_key, action, ok, retracted_at "
                "FROM task_jira_audit WHERE id = CAST(:id AS uuid)"
            ),
            {"id": audit_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"audit row {audit_id} not found")
    task_id, jira_key, action_kind, ok, retracted_at = row
    if retracted_at is not None:
        raise ValueError("audit row already retracted")
    if not ok or not jira_key:
        # Nothing actually landed in Jira — just mark the row.
        await session.execute(
            text("UPDATE task_jira_audit SET retracted_at = NOW() WHERE id = CAST(:id AS uuid)"),
            {"id": audit_id},
        )
        return {"ok": True, "noop": True}

    body = (
        "[bifrost-retracted] This action was retracted in Bifrost.\n\n"
        f"original_action: {action_kind}\n"
        f"reason: {reason}\n"
        f"audit_id: {audit_id}\n"
    )
    out = await add_comment(
        session,
        task_id=str(task_id),
        jira_key=str(jira_key),
        body=body,
        actor=actor,
    )
    await session.execute(
        text("UPDATE task_jira_audit SET retracted_at = NOW() WHERE id = CAST(:id AS uuid)"),
        {"id": audit_id},
    )
    # Drop the linkage so future lifecycle hooks treat the task as
    # unlinked (so the operator can re-spawn into the right project).
    if action_kind == "create":
        await session.execute(
            text(
                "UPDATE tasks SET jira_key = NULL, updated_at = NOW() WHERE id = CAST(:id AS uuid)"
            ),
            {"id": task_id},
        )
    return out


# ── helper: extract a Jira key from claude -p output ────────────────


_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b")


def find_jira_key_in_text(text_blob: str) -> str | None:
    m = _JIRA_KEY_RE.search(text_blob or "")
    return m.group(0) if m else None
