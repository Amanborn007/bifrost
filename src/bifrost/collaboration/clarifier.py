"""Clarifier agent for collaboration sessions.

Runs after each ``role=requester`` turn. Job:

  1. Read the requirement + chat history + currently-open doubts.
  2. Decide whether the requirement is concrete enough to brief.
  3. If not, ask 1-3 specific clarifying questions (free-form Markdown
     turn) AND emit each question as a structured doubt.
  4. If yes, acknowledge and signal ``requirement_is_concrete=True``
     so the route handler can advance the session state.

The clarifier runs against the chat history alone — no handover-doc grounding.
A later version will pass matched handover-doc snippets so the agent can answer
some doubts itself with citations rather than punting them all back
to the human.

Single Sonnet call with a JSON schema. Inline timeout of 45s; on
LLM failure we log and surface a graceful fallback turn so the user
isn't blocked from continuing the session manually.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
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
from bifrost.storage.models import CollabDoubt, CollabMessage, HandoverDoc
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


@dataclass
class ClarifierResult:
    response_md: str
    new_doubts: list[str]
    requirement_is_concrete: bool
    used_fallback: bool = False


_CLARIFIER_SYSTEM_PROMPT = f"""\
You are Bifrost's collaboration clarifier. A team member is asking another
team for help on a feature or change. Your job is to make the request
concrete enough that the owning team can scope it.

Read the requirement and the chat-so-far. Then EITHER:

  (a) If the requirement is still ambiguous, ask 1 to 3 specific clarifying
      questions in your ``response_md`` AND emit each as a structured
      ``new_doubts`` entry. Do NOT ask questions that are already answered
      somewhere in the chat. Do NOT re-ask questions that are already in
      the open-doubts list. Prefer questions that bound scope, surface
      hidden cross-team dependencies, or pin down acceptance criteria.

  (b) If the requirement is concrete and complete, set
      ``requirement_is_concrete: true``, write a short Markdown
      acknowledgement in ``response_md`` summarising what you understood
      (so the requester can confirm), and return an EMPTY ``new_doubts``
      list. The session will then advance to ``resolved`` and the
      synthesizer will produce the brief.

In BOTH cases, look at the open-doubts list. For any open doubt that the
requester's most recent turn ANSWERS (fully — partial answers don't count),
include that doubt's ``index`` in ``resolved_doubt_indices``. Those doubts
will be closed with the requester's latest message captured as the
resolution. This is how the doubt-clearance loop closes — without it the
right-rail piles up forever even when the user has already answered.

You may also receive matched HANDOVER DOCS in ``<handover_context>`` blocks.
These are existing team handover documents matched to the session's scope.
For any open doubt the handover docs ALREADY ANSWER directly, return an
entry in ``doc_resolutions`` with the doubt's 1-based index, the relevant
``citation_md`` (a short verbatim or near-verbatim excerpt from the
handover that answers the question), and the handover's ``source_ref``
exactly as it appears in the ``<handover_context handover_ref="...">``
attribute. These doubts will be marked ``answered_by_doc`` and surface
in the UI as resolved with a clickable citation chip — meaning the
asker doesn't need to bother the owning team with a question that's
already documented. Be conservative: if the handover doesn't directly
address the doubt, leave it open. Don't paraphrase to the point where
you're inventing details.

Style for ``response_md``:
  - Markdown is fine — bullets, **bold**, short paragraphs.
  - Lead with one sentence acknowledging what you've understood so far.
  - Then list the questions (or, if concrete, the summary).
  - Keep it tight. ~120-200 words.

Style for each entry in ``new_doubts``:
  - One question per entry. End with "?".
  - Self-contained — the doubt log is read out of context, so don't
    say "as I asked above" or "see my point 2".

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object specified by the schema.
"""


_CLARIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["response_md", "new_doubts", "requirement_is_concrete"],
    "properties": {
        "response_md": {"type": "string"},
        "new_doubts": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
        "requirement_is_concrete": {"type": "boolean"},
        # 1-based indices into the ``open_doubts`` list as it was
        # presented to the model. We map these back to UUIDs server-
        # side; the model never sees raw UUIDs, which keeps the schema
        # robust against the model echoing a mangled string.
        "resolved_doubt_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        # Doc-grounded resolutions. Each entry must reference an open
        # doubt by 1-based ``index``, supply a short ``citation_md``
        # excerpt from the handover doc, and echo the handover's
        # ``source_ref`` URN exactly as it appeared in the prompt.
        # Empty when no handover-context blocks were provided OR when
        # none of them directly answer any open doubt.
        "doc_resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "citation_md", "source_ref"],
                "properties": {
                    "index": {"type": "integer", "minimum": 1},
                    "citation_md": {"type": "string"},
                    "source_ref": {"type": "string"},
                },
            },
        },
    },
}


def _build_user_message(
    requirement_text: str,
    messages: list[CollabMessage],
    open_doubts: list[CollabDoubt],
    matched_handovers: list[tuple[HandoverDoc, str]],
) -> str:
    """Compose the user-facing prompt with all untrusted content boundary-
    wrapped per BOUNDARY_SENTENCE.

    ``matched_handovers`` is a list of ``(doc, body_md)`` tuples that
    the matcher selected as relevant to this session. Their bodies
    are injected as ``<handover_context>`` blocks the model can cite
    via ``doc_resolutions``."""

    parts: list[str] = [
        "Original requirement:",
        wrap_untrusted(requirement_text, "user_intent"),
        "",
    ]

    if messages:
        parts.append(f"Chat so far ({len(messages)} turns):")
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
            "describe what the relevant team owns. Cite them via "
            "``doc_resolutions`` to close any open doubt they already "
            "answer. The ``source_ref`` MUST be echoed verbatim:",
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

    if open_doubts:
        parts.append(
            f"Currently open doubts ({len(open_doubts)}) — do NOT re-ask. "
            "If the requester's latest message answers any, return their "
            "1-based index in ``resolved_doubt_indices``. If the handover "
            "context above directly answers any, use ``doc_resolutions``:",
        )
        for i, d in enumerate(open_doubts, start=1):
            parts.append(
                wrap_untrusted(
                    d.question,
                    "open_doubt",
                    attrs={"index": str(i)},
                ),
            )
        parts.append("")

    parts.append(
        "Decide whether to ask further clarifying questions or to mark "
        "the requirement concrete. Return the JSON object now.",
    )
    return "\n".join(parts)


async def run_clarifier(
    *,
    session_id: uuid.UUID,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> ClarifierResult:
    """Run one clarifier pass and persist its outputs.

    Persists exactly:
      - 1 ``role=clarifier_agent`` row in ``collab_messages``
      - 0..3 rows in ``collab_doubts`` (one per ``new_doubts`` entry)
      - Possibly a state transition to ``resolved`` if the model says
        the requirement is concrete (and there are no remaining opens).

    Failure modes (LLM down, schema mismatch) write a graceful
    fallback message instead of raising — the session stays usable
    so the requester can keep typing.
    """
    settings = settings or get_settings()

    sess = await repo.get_collab_session(session_id)
    if sess is None:
        raise ValueError(f"session {session_id} not found")

    messages = list(await repo.list_collab_messages(session_id))
    open_doubts = [d for d in await repo.list_collab_doubts(session_id) if d.status == "open"]

    # Match handover docs against the requirement + recent chat. We
    # join the most recent ~5 turns into the haystack so a follow-up
    # that mentions a new repo / team can pull a relevant handover
    # in even if the original requirement didn't.
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

    user_message = _build_user_message(
        sess.requirement_text,
        messages,
        open_doubts,
        matched_with_bodies,
    )

    client = ClaudeLlmClient(settings)
    await publish_agent_running(session_id, "clarifier")
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_CLARIFIER_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_CLARIFIER_SCHEMA,
            max_turns=1,
            # Follow-up turns include the full chat history + open-doubt
            # list, so Sonnet routinely needs 60-90s. 45s was tight on
            # the first follow-up and blew up on richer ones; 120s gives
            # headroom without making a hung CLI permanently stick.
            timeout_override=120,
        )
        parsed = completion.parsed
        response_md = str(parsed.get("response_md", "")).strip()
        new_doubts_raw = parsed.get("new_doubts") or []
        # Defensive: reject non-list / non-string / over-limit inputs so a
        # misbehaving model doesn't poison our DB.
        new_doubts: list[str] = [
            str(q).strip() for q in new_doubts_raw if isinstance(q, str) and str(q).strip()
        ][:3]
        is_concrete = bool(parsed.get("requirement_is_concrete", False))
        resolved_indices_raw = parsed.get("resolved_doubt_indices") or []
        resolved_indices: list[int] = [
            i for i in resolved_indices_raw if isinstance(i, int) and 1 <= i <= len(open_doubts)
        ]
        # Doc-grounded resolutions. Defensive parse: each entry must
        # have an integer index in range, a non-empty citation, and a
        # source_ref string. We also de-duplicate against the
        # human-resolved indices so a doubt isn't double-resolved
        # (the human-answer wins for downstream UX consistency).
        doc_resolutions_raw = parsed.get("doc_resolutions") or []
        doc_resolutions: list[dict[str, Any]] = []
        seen_indices = set(resolved_indices)
        for entry in doc_resolutions_raw:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            if not isinstance(idx, int) or not (1 <= idx <= len(open_doubts)):
                continue
            if idx in seen_indices:
                continue
            citation = str(entry.get("citation_md") or "").strip()
            source_ref = str(entry.get("source_ref") or "").strip()
            if not citation or not source_ref:
                continue
            seen_indices.add(idx)
            doc_resolutions.append(
                {
                    "index": idx,
                    "citation_md": citation,
                    "source_ref": source_ref,
                }
            )
    except ClaudeCliError as exc:
        logger.warning(
            "clarifier LLM failed for session=%s: %s",
            session_id,
            exc,
        )
        result = await _persist_fallback(repo, session_id, exc)
        await publish_session_updated(session_id, kind="clarifier_fallback")
        await publish_agent_done(session_id, "clarifier", ok=False)
        return result

    if not response_md:
        # Empty response is functionally a failure — write a fallback.
        result = await _persist_fallback(
            repo,
            session_id,
            "empty response_md from model",
        )
        await publish_session_updated(session_id, kind="clarifier_fallback")
        await publish_agent_done(session_id, "clarifier", ok=False)
        return result

    # Resolve any doubts the model says were answered by the latest
    # requester message. We capture that message as the resolution_md
    # so the doubt log shows the actual answer the user gave.
    last_requester_md: str | None = next(
        (m.content_md for m in reversed(messages) if m.role == "requester"),
        None,
    )
    for idx in resolved_indices:
        target = open_doubts[idx - 1]
        await repo.resolve_collab_doubt(
            target.id,
            status="answered_by_human",
            resolved_md=last_requester_md,
        )
    # Doc-grounded resolutions: status=answered_by_doc, resolved_md
    # carries the model's citation excerpt verbatim, source_ref is
    # the handover URN the UI turns into a clickable chip.
    for entry in doc_resolutions:
        target = open_doubts[entry["index"] - 1]
        await repo.resolve_collab_doubt(
            target.id,
            status="answered_by_doc",
            resolved_md=entry["citation_md"],
            source_ref=entry["source_ref"],
        )

    # Persist the clarifier turn
    await repo.append_collab_message(
        session_id=session_id,
        role="clarifier_agent",
        content_md=response_md,
    )
    # Persist new doubts
    for q in new_doubts:
        await repo.add_collab_doubt(session_id=session_id, question=q)

    # State transitions:
    #   open → clarifying  whenever the clarifier emits a turn at all
    #     (used to be: only flipped on the requester's NEXT message,
    #     which left the badge showing "Open" while the agent was
    #     visibly clarifying — a UX lie).
    #   open|clarifying → resolved  iff the model says concrete AND
    #     there are no remaining open doubts. Conservative on purpose.
    remaining_opens = [d for d in await repo.list_collab_doubts(session_id) if d.status == "open"]
    advanced = False
    if is_concrete and not remaining_opens:
        await repo.update_collab_session_state(
            session_id,
            "resolved",
            resolved=True,
        )
        advanced = True
    elif sess.state == "open":
        await repo.update_collab_session_state(session_id, "clarifying")

    logger.info(
        "clarifier session=%s matched_handovers=%d doubts_added=%d "
        "human_resolved=%d doc_resolved=%d concrete=%s advanced_to_resolved=%s",
        session_id,
        len(matched_with_bodies),
        len(new_doubts),
        len(resolved_indices),
        len(doc_resolutions),
        is_concrete,
        advanced,
    )

    await publish_session_updated(session_id, kind="clarifier_done")
    await publish_agent_done(session_id, "clarifier", ok=True)

    return ClarifierResult(
        response_md=response_md,
        new_doubts=new_doubts,
        requirement_is_concrete=is_concrete and not remaining_opens,
    )


async def _persist_fallback(
    repo: EntityRepository,
    session_id: uuid.UUID,
    reason: object,
) -> ClarifierResult:
    """Write a graceful fallback turn so the session stays driveable
    when the clarifier can't be reached. The requester can still type
    follow-ups; manual doubt resolution still works; close still works."""
    fallback_md = (
        "I couldn't run the clarifier on this turn. You can keep "
        "describing the requirement; I'll try again on your next "
        "message, or you can close the session and brief it with the "
        "current state."
    )
    await repo.append_collab_message(
        session_id=session_id,
        role="system",
        content_md=fallback_md,
    )
    logger.info("clarifier fallback session=%s reason=%s", session_id, reason)
    return ClarifierResult(
        response_md=fallback_md,
        new_doubts=[],
        requirement_is_concrete=False,
        used_fallback=True,
    )
