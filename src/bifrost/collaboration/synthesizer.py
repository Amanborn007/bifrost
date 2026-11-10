"""Synthesizer agent for collaboration sessions.

Runs at session close. Job:

  1. Read the requirement, full chat history, and ALL doubts (resolved
     + unresolved/risk).
  2. Produce a structured Markdown brief: title / summary / affected
     scopes / owning teams / estimated work / required artifacts /
     contacts / open risks / draft ask message.
  3. Write the brief to ``<handovers_dir>/sessions/<session_id>/brief.md``.
  4. Insert a ``collab_artifacts`` row pointing at it.
  5. Insert a ``synthesizer_agent`` message into the session so the
     reader sees the synthesis inline.
  6. Transition session state ``resolved`` → ``briefed``.

The synthesizer makes the brief from the chat alone (no handover-doc grounding).
A later version will pass matched scope-handover snippets so the brief can name
real owning teams / cite real flows / etc.

Single Sonnet call with a JSON schema. Inline timeout 180s — briefs
are bigger than clarifier turns and Sonnet legitimately needs more
time to produce 8 sections coherently.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.collaboration.events import (
    publish_agent_done,
    publish_agent_running,
    publish_session_updated,
)
from bifrost.collaboration.matching import (
    find_matching_handovers,
    handover_urn,
    read_handover_body,
)
from bifrost.extraction.cli_client import (
    ClaudeCliError,
    ClaudeLlmClient,
)
from bifrost.storage.models import (
    CollabDoubt,
    CollabMessage,
    HandoverDoc,
)
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


@dataclass
class SynthesizerResult:
    artifact_path: str
    title: str
    used_fallback: bool = False


_SYNTHESIZER_SYSTEM_PROMPT = f"""\
You are Bifrost's collaboration synthesizer. You produce the FINAL BRIEF
that closes a collaboration session — the document the requesting team
will hand to the owning team.

Read the requirement, the full chat history, and the doubt log
(resolved + unresolved/risks). Produce a brief with these fields:

  title              — one short line, requirement-shaped (≤80 chars).
  summary_md         — a paragraph (~60-120 words) restating what is
                       being asked, in the OWNING team's frame of
                       reference. Lead with the outcome.
  affected_scopes    — bullet list (Markdown ``-`` items) of likely
                       affected systems/domains/components, drawn from
                       what was discussed. Free-form text per bullet;
                       cite the chat where useful.
  owning_teams       — bullet list of likely owning teams. If unknown
                       say so; do NOT invent team names.
  estimated_work_md  — what the owning team would need to do, in their
                       own terms. Bullets or short paragraphs. If the
                       chat doesn't have enough info, say "Effort
                       estimate requires further discovery" and list
                       the specific things that need answering for an
                       estimate to be possible.
  required_artifacts_md
                     — what the asker should read or attach BEFORE
                       talking to the owning team (existing tickets,
                       runbooks, prior incidents). Bulleted.
  contacts_md        — suggested points-of-contact in prose. If you
                       don't know specific people, say so and suggest
                       a path (e.g. "post in the team's Slack channel
                       linked from their handover doc").
  open_risks_md      — every doubt with status ``unresolved`` from the
                       doubt log becomes a bullet here, faithfully
                       restated as a risk the asker is taking on.
  draft_ask_message  — a ready-to-paste Slack/email message from the
                       asker to the owning team. Friendly, concise,
                       links to the brief itself, ends with a clear
                       request. ~80-150 words.

Style rules:
  - Markdown is fine and preferred.
  - Be honest about uncertainty. The brief's job is to set up a useful
    conversation, not to pretend you have answers.
  - Never invent team names, contact names, or system names that
    weren't mentioned in the chat or doubts.
  - If the requirement is genuinely thin and you can't fill a section
    meaningfully, write one short sentence acknowledging that and
    pointing at the open risks.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object specified by the schema.
"""


_SYNTHESIZER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "title",
        "summary_md",
        "affected_scopes",
        "owning_teams",
        "estimated_work_md",
        "required_artifacts_md",
        "contacts_md",
        "open_risks_md",
        "draft_ask_message",
    ],
    "properties": {
        "title": {"type": "string", "maxLength": 200},
        "summary_md": {"type": "string"},
        "affected_scopes": {"type": "string"},
        "owning_teams": {"type": "string"},
        "estimated_work_md": {"type": "string"},
        "required_artifacts_md": {"type": "string"},
        "contacts_md": {"type": "string"},
        "open_risks_md": {"type": "string"},
        "draft_ask_message": {"type": "string"},
    },
}


def _handovers_dir() -> Path:
    """Where session-scoped handover files live on disk.

    Configurable via ``BIFROST_HANDOVERS_DIR`` env var; defaults to
    ``docs/handovers`` relative to the API process's CWD (operators
    run uvicorn from the project root, so this resolves to the
    repo's docs/handovers directory by default)."""
    return Path(os.environ.get("BIFROST_HANDOVERS_DIR", "docs/handovers"))


def _build_user_message(
    requirement_text: str,
    messages: list[CollabMessage],
    resolved_doubts: list[CollabDoubt],
    unresolved_doubts: list[CollabDoubt],
    matched_handovers: list[tuple[HandoverDoc, str]],
) -> str:
    parts: list[str] = [
        "Requirement:",
        wrap_untrusted(requirement_text, "user_intent"),
        "",
        f"Chat history ({len(messages)} turns):",
    ]
    for m in messages:
        parts.append(
            wrap_untrusted(
                m.content_md,
                "conversation_turn",
                attrs={"role": m.role, "ord": str(m.ord)},
            ),
        )
    parts.append("")

    if matched_handovers:
        parts.append(
            f"Matched handover docs ({len(matched_handovers)}) — these "
            "describe the actual owning team(s). Use them to produce the "
            "``owning_teams`` section: each handover represents one team's "
            "scope. Cite the team's scope_value verbatim. If multiple "
            "handovers matched, list each team SEPARATELY in the brief — "
            "they may have different asks of the requester:",
        )
        for doc, body in matched_handovers:
            parts.append(
                wrap_untrusted(
                    body,
                    "handover_context",
                    attrs={
                        "scope_kind": doc.scope_kind,
                        "scope_value": doc.scope_value,
                        "source_ref": handover_urn(doc),
                    },
                ),
            )
        parts.append("")

    if resolved_doubts:
        parts.append(f"Resolved doubts ({len(resolved_doubts)}):")
        for d in resolved_doubts:
            parts.append(
                wrap_untrusted(
                    f"Q: {d.question}\nA: {d.resolved_md or '(no answer captured)'}",
                    "resolved_doubt",
                    attrs={"status": d.status},
                ),
            )
        parts.append("")

    if unresolved_doubts:
        parts.append(
            f"Unresolved / risk doubts ({len(unresolved_doubts)}) — "
            "these MUST appear in open_risks_md verbatim or near-verbatim:",
        )
        for d in unresolved_doubts:
            parts.append(
                wrap_untrusted(
                    d.question,
                    "unresolved_doubt",
                    attrs={"status": d.status},
                ),
            )
        parts.append("")

    parts.append(
        "Produce the final brief now. Return the JSON object exactly per the schema.",
    )
    return "\n".join(parts)


def _render_brief_md(parsed: dict[str, Any]) -> str:
    """Compose the Markdown brief file from the parsed sections."""
    title = str(parsed.get("title", "Collaboration brief")).strip()
    return "\n".join(
        [
            f"# {title}",
            "",
            "## Summary",
            str(parsed.get("summary_md", "")).strip(),
            "",
            "## Affected scopes",
            str(parsed.get("affected_scopes", "")).strip(),
            "",
            "## Owning team(s)",
            str(parsed.get("owning_teams", "")).strip(),
            "",
            "## Estimated work on the owning team",
            str(parsed.get("estimated_work_md", "")).strip(),
            "",
            "## Required artifacts to read first",
            str(parsed.get("required_artifacts_md", "")).strip(),
            "",
            "## Suggested points of contact",
            str(parsed.get("contacts_md", "")).strip(),
            "",
            "## Open risks",
            str(parsed.get("open_risks_md", "")).strip(),
            "",
            "## Draft ask message",
            str(parsed.get("draft_ask_message", "")).strip(),
            "",
        ]
    )


def _render_synthesizer_chat_turn(parsed: dict[str, Any], fs_path: str) -> str:
    """Shorter Markdown rendered into the chat thread itself so the user
    sees the synthesizer's output without having to open the artifact."""
    title = str(parsed.get("title", "")).strip()
    summary = str(parsed.get("summary_md", "")).strip()
    risks = str(parsed.get("open_risks_md", "")).strip()
    return "\n".join(
        [
            f"**Brief produced:** {title}",
            "",
            summary,
            "",
            f"Full brief saved to `{fs_path}`. The brief includes the "
            "affected-scopes list, estimated work for the owning team, "
            "suggested contacts, and a draft ask message you can paste "
            "directly into Slack.",
            "",
            *(
                [
                    "**Open risks** (carry these into the conversation with the owning team):",
                    risks,
                ]
                if risks
                else []
            ),
        ]
    )


async def run_synthesizer(
    *,
    session_id: uuid.UUID,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> SynthesizerResult:
    """Run synthesis and persist outputs.

    Persists exactly:
      - One ``role=synthesizer_agent`` row in collab_messages.
      - One file at ``<handovers_dir>/sessions/<session_id>/brief.md``.
      - One ``collab_artifacts`` row with ``kind='final_brief'`` pointing
        at the file.
      - State transition ``resolved → briefed``.

    On LLM failure: writes a system fallback turn, leaves state at
    ``resolved`` (NOT ``briefed`` — the brief doesn't exist), returns a
    SynthesizerResult with ``used_fallback=True``."""
    settings = settings or get_settings()

    sess = await repo.get_collab_session(session_id)
    if sess is None:
        raise ValueError(f"session {session_id} not found")

    messages = list(await repo.list_collab_messages(session_id))
    all_doubts = list(await repo.list_collab_doubts(session_id))
    resolved_doubts = [
        d for d in all_doubts if d.status in ("answered_by_doc", "answered_by_human")
    ]
    unresolved_doubts = [d for d in all_doubts if d.status in ("unresolved", "escalated")]

    # Pull matched handovers (same matcher the clarifier
    # uses) so the brief can name real owning teams. Match across
    # the requirement + recent requester turns. When more than one
    # team's handover matches, the synthesizer is instructed to list
    # each team separately in the brief.
    haystack_parts = [sess.requirement_text]
    for m in messages[-5:]:
        if m.role == "requester":
            haystack_parts.append(m.content_md)
    matched_docs = await find_matching_handovers(
        repo=repo,
        haystack="\n\n".join(haystack_parts),
    )
    matched_with_bodies: list[tuple[HandoverDoc, str]] = []
    for d in matched_docs:
        body = read_handover_body(d)
        if body:
            matched_with_bodies.append((d, body))

    # Persist matched ids onto the session for the loop-back queries
    # (and so the UI can show which handover(s) the brief drew from).
    if matched_docs:
        sess.matched_doc_ids = [str(d.id) for d in matched_docs]
        await repo._session.flush()

    user_message = _build_user_message(
        sess.requirement_text,
        messages,
        resolved_doubts,
        unresolved_doubts,
        matched_with_bodies,
    )

    client = ClaudeLlmClient(settings)
    await publish_agent_running(session_id, "synthesizer")
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_SYNTHESIZER_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_SYNTHESIZER_SCHEMA,
            max_turns=1,
            timeout_override=180,
        )
        parsed = completion.parsed
    except ClaudeCliError as exc:
        logger.warning(
            "synthesizer LLM failed for session=%s: %s",
            session_id,
            exc,
        )
        await _persist_fallback(repo, session_id, exc)
        await publish_session_updated(session_id, kind="synthesizer_fallback")
        await publish_agent_done(session_id, "synthesizer", ok=False)
        return SynthesizerResult(
            artifact_path="",
            title="",
            used_fallback=True,
        )

    title = str(parsed.get("title") or "").strip() or "Collaboration brief"

    # Materialize the brief on disk (write-temp + rename pattern so a
    # crash mid-write never leaves a torn file).
    out_dir = _handovers_dir() / "sessions" / str(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "brief.md"
    tmp_path = out_dir / "brief.md.tmp"
    tmp_path.write_text(_render_brief_md(parsed), encoding="utf-8")
    tmp_path.replace(final_path)
    fs_path_str = str(final_path)

    # Persist artifact row
    await repo.add_collab_artifact(
        session_id=session_id,
        kind="final_brief",
        fs_path=fs_path_str,
    )

    # Persist a synthesizer chat turn so the message thread tells the
    # full story without users needing to open the file.
    await repo.append_collab_message(
        session_id=session_id,
        role="synthesizer_agent",
        content_md=_render_synthesizer_chat_turn(parsed, fs_path_str),
    )

    # Transition state. The close handler should have already moved
    # us to ``resolved``; we move ``resolved → briefed`` only.
    await repo.update_collab_session_state(session_id, "briefed")

    logger.info(
        "synthesizer session=%s briefed_to=%s title=%s",
        session_id,
        fs_path_str,
        title,
    )

    await publish_session_updated(session_id, kind="synthesizer_done")
    await publish_agent_done(session_id, "synthesizer", ok=True)

    return SynthesizerResult(
        artifact_path=fs_path_str,
        title=title,
        used_fallback=False,
    )


async def _persist_fallback(
    repo: EntityRepository,
    session_id: uuid.UUID,
    reason: object,
) -> None:
    """Write a graceful fallback turn so the user knows the brief
    didn't generate. State stays at ``resolved`` (not ``briefed``)
    so the user can retry."""
    fallback_md = (
        "I couldn't generate the brief on this attempt. The session "
        "is parked in **resolved** state — re-run close to try again, "
        "or fall back to the chat history + doubt log as your record."
    )
    await repo.append_collab_message(
        session_id=session_id,
        role="system",
        content_md=fallback_md,
    )
    logger.info("synthesizer fallback session=%s reason=%s", session_id, reason)
