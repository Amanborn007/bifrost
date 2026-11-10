"""CLARIFY shortcut — answer a follow-up question from the prior bundle.

When the follow-up planner is *very* confident the new question is
just a clarification or rephrasing of the prior turn (cosine
similarity ≥ 0.90 against the prior question, no new repo mentions),
we skip the entire research pipeline — no scope resolution, no repo
ranking, no agent fan-out — and stream a short Haiku-backed
clarification grounded only in the prior research bundle.

This is the "chat that learns" half (research-bundle persistence
and hydration) actually paying off: a follow-up turn that's truly a
clarification answers in seconds at Haiku cost rather than minutes at
full-pipeline cost.

Safety guard: the system prompt instructs Haiku to admit when the
prior bundle doesn't contain enough information, rather than
hallucinate. The coordinator gates this path on similarity ≥ 0.90
and a non-trivial prior answer (≥ 80 chars) so a near-empty bundle
can't drive a misleading shortcut.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from bifrost.cli.config import BifrostSettings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient, Usage
from bifrost.research.bundle_cache import ResearchBundle

logger = logging.getLogger(__name__)


# Public threshold the coordinator gates on. Set deliberately above
# the planner's _HIGH_SIMILARITY (0.85) so the planner can advise a
# CLARIFY classification while the actual short-circuit demands tighter
# evidence.
CLARIFY_SIMILARITY_FLOOR: float = 0.90

# Don't shortcut on a near-empty prior answer — there's nothing to
# clarify *from*. The full pipeline gives a better result in that case.
CLARIFY_MIN_PRIOR_ANSWER_CHARS: int = 80


_SYSTEM_PROMPT = (
    "You are clarifying a prior research answer for a software engineer. "
    "You have ONLY the prior question, the prior answer, and the prior "
    "sources. You CANNOT do new research, read code, or call tools. "
    "Your job:\n\n"
    "  1. Restate or expand on what the prior answer already covered, "
    "in a way that addresses the user's new question.\n"
    "  2. Stay grounded — use ONLY information present in the prior "
    "answer or visible in the listed sources.\n"
    "  3. If the prior answer doesn't contain enough information to "
    "address the new question, say so explicitly: 'The prior research "
    "doesn't cover this — I can do a fresh search if you'd like.' "
    "Do NOT invent details to fill the gap.\n\n"
    "Tone: terse, technical, code-snippet-friendly. Skip pleasantries. "
    "Quote URN identifiers when referencing entities (e.g. "
    "bifrost://repo/orders/component/RetryQueue)."
)


def _format_sources_for_prompt(bundle: ResearchBundle, *, max_sources: int = 12) -> str:
    """Compact textual form of the prior sources list. Caps to keep the
    prompt cheap — the whole point of CLARIFY is sub-second/cheap."""
    if not bundle.sources:
        return "(none recorded)"
    lines: list[str] = []
    for s in bundle.sources[:max_sources]:
        title = (s.title or "").strip()
        ref = (s.ref or "").strip()
        prefix = s.kind or "source"
        if title and ref:
            lines.append(f"- [{prefix}] {ref}: {title}")
        elif ref:
            lines.append(f"- [{prefix}] {ref}")
        elif title:
            lines.append(f"- [{prefix}] {title}")
    if len(bundle.sources) > max_sources:
        lines.append(f"  (+ {len(bundle.sources) - max_sources} more)")
    return "\n".join(lines) or "(none recorded)"


def _build_user_message(
    *,
    new_question: str,
    prior_bundle: ResearchBundle,
    prior_turns_text: str,
) -> str:
    """Assemble the Haiku user message: prior Q, prior A, prior sources, new Q.

    The prior answer is the load-bearing piece; sources are scaffolding.
    Caps at ~6 KB so a long prior turn doesn't blow the Haiku budget.
    """
    prior_q = (prior_bundle.last_question or "").strip()[:1000]
    prior_a = (prior_bundle.last_answer or "").strip()[:6000]
    sources_block = _format_sources_for_prompt(prior_bundle)
    repos_block = ", ".join(prior_bundle.repos_used_names[:8]) or "(none)"
    parts = [
        "<prior_question>",
        prior_q,
        "</prior_question>",
        "",
        "<prior_repos>",
        repos_block,
        "</prior_repos>",
        "",
        "<prior_sources>",
        sources_block,
        "</prior_sources>",
        "",
        "<prior_answer>",
        prior_a,
        "</prior_answer>",
    ]
    if prior_turns_text:
        parts.extend(
            [
                "",
                "<recent_chat_history>",
                prior_turns_text[:2000],
                "</recent_chat_history>",
            ]
        )
    parts.extend(
        [
            "",
            "<new_question>",
            new_question.strip()[:2000],
            "</new_question>",
            "",
            "Reply now with the clarification or expansion.",
        ]
    )
    return "\n".join(parts)


def _is_eligible(prior_bundle: ResearchBundle | None, similarity: float | None) -> bool:
    """Coordinator-side safety guard. Both must hold for the shortcut.

    Caller is also expected to have a CLARIFY plan; this function only
    enforces the similarity-floor + prior-answer-substantive contract.
    """
    if prior_bundle is None:
        return False
    if similarity is None or similarity < CLARIFY_SIMILARITY_FLOOR:
        return False
    return not len((prior_bundle.last_answer or "").strip()) < CLARIFY_MIN_PRIOR_ANSWER_CHARS


def _bundle_sources_for_done_event(prior_bundle: ResearchBundle) -> list[dict[str, Any]]:
    """Project bundle sources back into the synthesis-shaped dict the
    chat UI knows how to render. Same shape as ``run_research``'s
    ``done`` event payload."""
    sources: list[dict[str, Any]] = []
    for s in prior_bundle.sources:
        sources.append(
            {
                "urn": s.ref,
                "name": s.title or s.ref,
                "entity_type": s.kind or "repo",
                "relevance": "high",
                "role": "from prior research bundle",
            }
        )
    return sources


async def run_clarify_synthesis(
    *,
    question: str,
    prior_bundle: ResearchBundle,
    prior_turns_text: str,
    settings: BifrostSettings,
    client: ClaudeLlmClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a CLARIFY response and yield SSE-shaped events.

    Mirrors the event sequence of ``run_research`` so the SSE consumer
    can swap one for the other. Emits, in order:

      1. ``status`` — "Reusing prior research…"
      2. one or more ``synthesis_delta`` events as Haiku streams text
      3. ``done`` — same shape as run_research, with ``confidence`` set
         to the prior bundle's confidence (we're not re-evaluating —
         the prior research already established it).

    On Haiku failure: yields an ``error`` event so the coordinator can
    fall back to the full pipeline.
    """
    yield {
        "type": "status",
        "message": (
            "Reusing prior research bundle — answering from "
            f"{len(prior_bundle.repos_used_names)} prior repo(s) "
            "via Haiku clarification (no fresh fan-out)."
        ),
    }
    user_message = _build_user_message(
        new_question=question,
        prior_bundle=prior_bundle,
        prior_turns_text=prior_turns_text,
    )
    if client is None:
        client = ClaudeLlmClient(settings)
    final_text_parts: list[str] = []
    usage: Usage | None = None
    try:
        async for evt in client.stream_text(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.reasoning_model,  # premium — generates ask-back / acknowledge / answer
            max_turns=1,
        ):
            if evt.get("type") == "delta":
                text = str(evt.get("text", ""))
                if text:
                    final_text_parts.append(text)
                    yield {"type": "synthesis_delta", "text": text}
            elif evt.get("type") == "done":
                usage = evt.get("usage")
                if not final_text_parts and evt.get("final_text"):
                    final_text_parts.append(str(evt["final_text"]))
    except ClaudeCliError as exc:
        logger.exception("clarify: Haiku stream failed")
        yield {"type": "error", "error": f"Clarify failed: {exc}"}
        return

    final_text = "".join(final_text_parts).strip()
    if not final_text:
        yield {
            "type": "error",
            "error": "Clarify produced no output; falling back to full research.",
        }
        return

    yield {
        "type": "done",
        "answer": final_text,
        "sources": _bundle_sources_for_done_event(prior_bundle),
        "usage": {
            "total_cost_usd": float(usage.total_cost_usd) if usage else 0.0,
            "input_tokens": int(usage.input_tokens) if usage else 0,
            "output_tokens": int(usage.output_tokens) if usage else 0,
            "cache_read_tokens": int(usage.cache_read_input_tokens) if usage else 0,
            "calls": 1 if usage else 0,
        },
        "confidence": prior_bundle.confidence,
        "reports": [],  # no fan-out happened
        "shortcut": "clarify_from_bundle",  # observability
    }
