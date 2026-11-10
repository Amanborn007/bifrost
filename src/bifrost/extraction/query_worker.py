"""Query worker — agentic chat that combines live repo access + knowledge graph.

Claude gets Read/Glob/Grep tools scoped to the cloned repo directory, plus
a system prompt summarising the pre-extracted entity knowledge. It answers
by combining: (a) fast structured knowledge from our extraction pipeline,
(b) live inspection of actual source code via its tools.

Streaming: tokens flow via SSE `token` events. Tool uses (Read/Grep/…) flow
as `tool_use` events so the UI can show "Reading CarrierAdapter.java…" chips.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from bifrost.api.sse import SSEEvent, event_bus
from bifrost.cli.config import BifrostSettings
from bifrost.storage.models import ConversationMessage, Entity, LLMCostLog
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


CHAT_SYSTEM_PROMPT_TEMPLATE = """\
You are Bifrost, a software engineering knowledge agent for the repository
**{repo_name}** (cloned locally at `{clone_dir}`).

You have TWO sources of information — use both to give accurate, grounded answers:

1. **Pre-extracted knowledge index** (below) — our extraction pipeline has
   already identified components, flows, and dependencies. Treat this as a
   fast index/map of the codebase.

2. **Live source code** — you can read actual files via the `Read`, `Glob`,
   and `Grep` tools. The cloned repo is at `{clone_dir}`. Use these when the
   extracted index is incomplete, you need specifics (signatures, error
   handling, exact strings), or you need to verify your answer.

## Extracted knowledge index

{entity_context}

## How to answer

- Start by consulting the extracted index above. If it fully answers the
  question, you don't need to read any files.
- If details are missing, use `Grep` to find the right file(s), then `Read`
  specific files. Prefer narrow reads (offset + limit) over whole-file reads.
- Cite entities by URN when you use them (e.g., `urn:bifrost:repo/…/component/Foo`).
- Cite source files by path (e.g., `src/main/java/.../CarrierAdapter.java`).
- Write clean markdown. Use `code` for identifiers, **bold** for component
  names, bullet lists for enumerations. Short paragraphs. No filler.
- If the answer is one sentence, write one sentence. Don't pad.
"""


@dataclass
class QueryWorkerDeps:
    session_factory: Any
    settings: BifrostSettings


def _evt_status(message: str) -> SSEEvent:
    return SSEEvent(event_type="status", data={"message": message})


def _evt_token(text: str) -> SSEEvent:
    return SSEEvent(event_type="token", data={"text": text})


def _evt_tool_use(name: str, input_data: dict) -> SSEEvent:
    return SSEEvent(
        event_type="tool_use",
        data={"name": name, "input": input_data},
    )


def _evt_assistant_message(msg: dict[str, Any]) -> SSEEvent:
    return SSEEvent(event_type="assistant_message", data=msg)


def _evt_error(error: str) -> SSEEvent:
    return SSEEvent(event_type="error", data={"error": error})


def _evt_done() -> SSEEvent:
    return SSEEvent(event_type="done", data={})


# Canonical confidence vocabulary. Anything else gets coerced to
# ``uncertain``. The UI's ConfidenceBadge has its
# own fallback to "unknown" but the storage layer should never see
# strings outside this set — drift here cascades into bad UX (a
# coordinator typo like "definitely" would render as "Unknown" with
# no signal that it's a bug rather than a real confidence level).
_CANONICAL_CONFIDENCE = ("confirmed", "inferred", "uncertain")


def _normalise_confidence(value: Any) -> str:
    if not value:
        return "uncertain"
    s = str(value).strip().lower()
    if s in _CANONICAL_CONFIDENCE:
        return s
    # Soft mappings for synonyms a coordinator might emit.
    if s in ("high", "verified", "confirmed_by_source"):
        return "confirmed"
    if s in ("medium", "partial", "inferred_indirect"):
        return "inferred"
    if s in ("low", "unknown", "weak"):
        return "uncertain"
    logger.warning(
        "query_worker: non-canonical confidence %r → uncertain",
        s,
    )
    return "uncertain"


async def run_query_turn(
    *,
    conversation_id: uuid.UUID,
    user_message_content: str,
    deps: QueryWorkerDeps,
    scope_repo_ids: list[str] | None = None,
    research_run_id: uuid.UUID | None = None,
    turn_index: int | None = None,
) -> None:
    """Execute one query turn via the multi-repo research coordinator.

    Delegates the reasoning to `bifrost.research.coordinator.run_research`
    which fans out to N repo sub-agents + 1 external (Confluence/Jira)
    agent, then synthesises a unified answer. This module just wraps the
    event stream as SSE + persists the final message to Postgres.

    ``research_run_id`` is the durable state row created by the API
    layer; we advance its status as phases complete so a page refresh
    or backend restart can detect what's running and resume cleanly.
    """
    from bifrost.research.coordinator import ResearchDeps, run_research
    from bifrost.research.runs import (
        mark_done as runs_mark_done,
    )
    from bifrost.research.runs import (
        mark_failed as runs_mark_failed,
    )
    from bifrost.research.runs import (
        mark_phase as runs_mark_phase,
    )

    conv_key = str(conversation_id)
    research_deps = ResearchDeps(
        session_factory=deps.session_factory,
        settings=deps.settings,
    )

    async def _advance_phase(phase: str) -> None:
        """Advance the research_runs row's status. Idempotent — calling
        with the current phase is a no-op, calling backwards rewinds.
        Errors are logged but never raised (resumability state is
        secondary to the actual run).
        """
        if research_run_id is None:
            return
        try:
            await runs_mark_phase(deps.session_factory, research_run_id, phase)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.exception(
                "research_runs mark_phase(%s) failed; non-fatal",
                phase,
            )

    # Accumulate state as events flow
    final_answer = ""
    final_sources: list[dict[str, Any]] = []
    # Default to "uncertain" rather than "inferred" — a missing
    # confidence field on the done event most likely means the model
    # didn't have enough signal to grade itself, which is closer to
    # "uncertain" than "inferred".
    final_confidence = "uncertain"
    final_usage_dict: dict[str, Any] = {}
    reports_for_payload: list[dict[str, Any]] = []
    final_followups: list[dict[str, Any]] = []
    persisted = False  # set True once we've written the assistant row

    async def _persist_assistant_turn() -> None:
        """Persist whatever ``final_answer`` we've accumulated.

        Called from the ``done`` branch (full answer) AND from the
        ``finally`` block (partial / error checkpoint).
        Idempotent: a closure over ``persisted`` short-circuits
        if the row was already written.
        """
        nonlocal persisted
        if persisted:
            return
        if not final_answer:
            return
        cost_usd = float(final_usage_dict.get("total_cost_usd") or 0.0)
        token_counts = {
            "input_tokens": int(final_usage_dict.get("input_tokens") or 0),
            "output_tokens": int(final_usage_dict.get("output_tokens") or 0),
            "cache_read_tokens": int(final_usage_dict.get("cache_read_tokens") or 0),
            "calls": int(final_usage_dict.get("calls") or 0),
            "reports": len(reports_for_payload),
        }

        async with deps.session_factory() as session:
            repo = EntityRepository(session)
            assistant_msg = await repo.append_message(
                conversation_id=conversation_id,
                role="assistant",
                content=final_answer,
                sources=final_sources,
                confidence=final_confidence,
                gaps=[],
                token_counts=token_counts,
                cost_usd=cost_usd if cost_usd > 0 else None,
                suggested_followups=final_followups,
            )
            if cost_usd > 0:
                session.add(
                    LLMCostLog(
                        id=uuid.uuid4(),
                        task_id=f"chat:{conv_key}",
                        repo_id=None,
                        phase="chat",
                        model=deps.settings.agent_model,
                        input_tokens=token_counts["input_tokens"],
                        output_tokens=token_counts["output_tokens"],
                        estimated_cost_usd=cost_usd,
                        skipped=False,
                    )
                )
            await session.commit()

            # Publish AFTER commit but the commit IS
            # the persistence point — if the process dies between
            # commit and publish, the row is in the DB and a refresh
            # of the conversation will render it. The replay buffer
            # captures the published event for late SSE subscribers.
            payload = {
                "id": str(assistant_msg.id),
                "conversation_id": conv_key,
                "turn_index": assistant_msg.turn_index,
                "role": "assistant",
                "content": final_answer,
                "sources": final_sources,
                "confidence": final_confidence,
                "gaps": [],
                "tool_uses": [],
                "created_at": assistant_msg.created_at.isoformat(),
                "reports": reports_for_payload,
                "suggested_followups": final_followups,
            }
            await event_bus.publish(conv_key, _evt_assistant_message(payload))
            persisted = True

    # Track which phase we've already announced so we don't churn
    # research_runs on every event of the same kind.
    _current_phase: str | None = None

    async def _set_phase(phase: str) -> None:
        nonlocal _current_phase
        if _current_phase == phase:
            return
        _current_phase = phase
        await _advance_phase(phase)

    error_seen = False
    # The user-message turn_index is the cache key per-(conversation,
    # turn). The route handler hands it in directly via ``turn_index``;
    # an in-memory pass avoids a race against the route's pending COMMIT
    # (the worker task is spawned BEFORE the route's session.commit()
    # fires, so a DB SELECT here can return None even though the row
    # is in flight). When the caller doesn't pass it (legacy paths),
    # fall back to the DB lookup — best-effort.
    cache_turn_id: int | None = turn_index
    if cache_turn_id is None:
        try:
            from sqlalchemy import select as sa_select

            from bifrost.storage.models import ConversationMessage

            async with deps.session_factory() as session:
                stmt = (
                    sa_select(ConversationMessage.turn_index)
                    .where(
                        ConversationMessage.conversation_id == conversation_id,
                    )
                    .where(ConversationMessage.role == "user")
                    .order_by(ConversationMessage.turn_index.desc())
                    .limit(1)
                )
                cache_turn_id = (await session.execute(stmt)).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            logger.exception(
                "query_worker: cache turn lookup failed; non-fatal",
            )

    try:
        await _set_phase("scoping")
        async for evt in run_research(
            question=user_message_content,
            conversation_id=conversation_id,
            deps=research_deps,
            explicit_scope_repo_ids=scope_repo_ids,
            research_run_id=research_run_id,
            turn_id=cache_turn_id,
        ):
            etype = evt.get("type")
            # Phase transitions — driven by the first event of each kind.
            if etype in ("agent_start", "subagent_start", "external_start"):
                await _set_phase("fanout")
            elif etype == "synthesis_delta":
                await _set_phase("synthesizing")

            if etype == "status":
                await event_bus.publish(
                    conv_key,
                    _evt_status(str(evt.get("message", ""))),
                )
            elif etype == "error":
                error_seen = True
                err_msg = str(evt.get("error", ""))[:500]
                await event_bus.publish(conv_key, _evt_error(err_msg))
                if research_run_id is not None:
                    try:
                        await runs_mark_failed(
                            deps.session_factory,
                            research_run_id,
                            error_message=err_msg,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "research_runs mark_failed failed; non-fatal",
                        )
                return
            elif etype == "done":
                final_answer = str(evt.get("answer", "")) or final_answer
                final_sources = list(evt.get("sources") or [])
                final_confidence = _normalise_confidence(
                    evt.get("confidence"),
                )
                final_usage_dict = dict(evt.get("usage") or {})
                reports_for_payload = list(evt.get("reports") or [])
                final_followups = list(evt.get("suggested_followups") or [])
            elif etype == "suggested_followups":
                # Live event so the UI can render chips during streaming.
                # Also retained as a fallback in case the ``done`` event
                # doesn't carry the field (partial-answer checkpoint).
                final_followups = list(evt.get("suggestions") or [])
                await event_bus.publish(
                    conv_key,
                    SSEEvent(
                        event_type="suggested_followups",
                        data={"suggestions": final_followups},
                    ),
                )
            elif etype == "synthesis_delta":
                text = str(evt.get("text", ""))
                final_answer += text
                await event_bus.publish(conv_key, _evt_token(text))
            elif etype and not etype.startswith("_"):
                payload = {k: v for k, v in evt.items() if k != "type"}
                await event_bus.publish(
                    conv_key,
                    SSEEvent(event_type=etype, data=payload),
                )

        if not final_answer:
            error_seen = True
            empty_msg = "Coordinator returned an empty answer."
            await event_bus.publish(conv_key, _evt_error(empty_msg))
            if research_run_id is not None:
                try:
                    await runs_mark_failed(
                        deps.session_factory,
                        research_run_id,
                        error_message=empty_msg,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "research_runs mark_failed failed; non-fatal",
                    )
            return

        await _persist_assistant_turn()

        # Successful turn — durable state machine should now be done.
        if research_run_id is not None:
            try:
                await runs_mark_done(
                    deps.session_factory,
                    research_run_id,
                    final_answer_text=final_answer,
                    total_cost_usd=float(
                        final_usage_dict.get("total_cost_usd") or 0.0,
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "research_runs mark_done failed; non-fatal",
                )

    except Exception as exc:
        logger.exception("Research query turn failed: %s", exc)
        await event_bus.publish(conv_key, _evt_error(str(exc)[:500]))
        if research_run_id is not None and not error_seen:
            try:
                await runs_mark_failed(
                    deps.session_factory,
                    research_run_id,
                    error_message=f"Crash: {str(exc)[:300]}",
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "research_runs mark_failed (crash) failed; non-fatal",
                )
    finally:
        # If the stream ended (or crashed) and we
        # have *some* synthesised text, persist it so the UI doesn't
        # vanish a partial answer on refresh. Marks confidence as
        # "uncertain" if it never reached `done`.
        if not persisted and final_answer:
            try:
                await _persist_assistant_turn()
            except Exception:
                logger.exception(
                    "Partial-answer checkpoint failed for conv=%s",
                    conv_key,
                )
        await event_bus.publish(conv_key, _evt_done())


# ── Helpers ────────────────────────────────────────────────────────


def _dir_exists(path: str) -> bool:
    from pathlib import Path

    try:
        return Path(path).is_dir()
    except OSError:
        return False


async def _fetch_entities(
    repo: EntityRepository,
    repo_id: uuid.UUID,
    limit: int = 200,
) -> list[Entity]:
    return list(await repo.list_entities(repo_id=repo_id, limit=limit))


def _format_entities_for_context(entities: list[Entity]) -> str:
    """Render entities as a compact, Claude-friendly knowledge manifest."""
    if not entities:
        return "(empty)"
    by_type: dict[str, list[Entity]] = {}
    for e in entities:
        by_type.setdefault(e.entity_type, []).append(e)

    parts: list[str] = []
    for etype, items in by_type.items():
        parts.append(f"### {etype.capitalize()}s ({len(items)})")
        for e in items[:40]:  # cap per-type to keep context manageable
            meta = e.entity_metadata or {}
            purpose = meta.get("purpose") or meta.get("summary") or ""
            source_file = meta.get("source_file") or ""
            line = f"- **{e.name}**"
            if source_file:
                line += f"  `{source_file}`"
            line += f"  \n  URN: `{e.urn}`"
            if purpose:
                line += f"  \n  {str(purpose)[:180]}"
            parts.append(line)
        if len(items) > 40:
            parts.append(f"- *…and {len(items) - 40} more*")
        parts.append("")
    return "\n".join(parts)


def _format_prior_turns(
    prior: list[ConversationMessage] | Any,
    max_turns: int = 4,
) -> str:
    if not prior:
        return ""
    recent = list(prior)[-max_turns * 2 :]
    lines = []
    for m in recent:
        speaker = "User" if m.role == "user" else "Assistant"
        content = m.content[:400]
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def _build_user_message(question: str, prior_text: str) -> str:
    parts: list[str] = []
    if prior_text:
        parts.extend(["# Prior conversation", prior_text, ""])
    parts.extend(["# Current question", question])
    return "\n".join(parts)


def _derive_cited_sources(
    answer_text: str,
    entities: list[Entity],
    tool_uses: list[dict[str, Any]],
    max_sources: int = 12,
) -> list[dict[str, Any]]:
    """Pick entities whose name appears in the answer; add files Claude actually read."""
    cited: list[dict[str, Any]] = []
    seen: set[str] = set()

    lower = answer_text.lower()

    # Entities cited by name
    for e in entities:
        if e.urn in seen:
            continue
        if e.name and e.name.lower() in lower:
            cited.append(
                {"urn": e.urn, "name": e.name, "entity_type": e.entity_type},
            )
            seen.add(e.urn)
            if len(cited) >= max_sources:
                break

    # Files actually read — surface them as sources too (useful provenance)
    for use in tool_uses:
        if use.get("name") != "Read":
            continue
        fp = use.get("input", {}).get("file_path") or ""
        if fp and fp not in seen and len(cited) < max_sources:
            cited.append(
                {
                    "urn": f"file://{fp}",
                    "name": fp.rsplit("/", 1)[-1],
                    "entity_type": "file",
                    "path": fp,
                },
            )
            seen.add(fp)

    if not cited and entities:
        for e in entities[: min(max_sources, 6)]:
            cited.append(
                {"urn": e.urn, "name": e.name, "entity_type": e.entity_type},
            )
    return cited
