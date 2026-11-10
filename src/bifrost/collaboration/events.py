"""SSE event helpers for collaboration sessions.

Reuses the existing ``TaskEventBus`` singleton from ``api/sse`` —
the bus's ``task_id`` parameter is arbitrary string so we just
pass ``str(session_id)`` and get per-session pub/sub for free,
including the replay buffer that the conversation streams already
rely on.

Event types emitted:

  ``session_updated`` — fired any time a session-scoped mutation
      lands (a new message, doubt added/resolved, artifact created,
      state transition). Frontend subscribers refetch on it. Cheap
      and broad on purpose: avoids the trap of having a dozen
      fine-grained event types each with its own client-side branch.

  ``agent_running``   — the clarifier or synthesizer kicked off an
      LLM call. Carries ``{"agent": "clarifier"|"synthesizer"}``.
      Drives the "Bifrost is thinking…" pill in the UI.

  ``agent_done``      — paired with ``agent_running``; clears the pill.

A future version may add token-streaming events for the agent text itself.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from bifrost.api.sse import SSEEvent, event_bus

logger = logging.getLogger(__name__)


async def publish_session_updated(
    session_id: uuid.UUID,
    *,
    kind: str | None = None,
) -> None:
    """Emit a generic ``session_updated`` event for the given session.

    ``kind`` is purely informational metadata for debugging / future
    fine-grained client logic; the current frontend ignores it and just
    invalidates the session query."""
    await _publish(session_id, "session_updated", {"kind": kind} if kind else {})


async def publish_agent_running(
    session_id: uuid.UUID,
    agent: str,
) -> None:
    """Mark an agent as currently mid-LLM-call. Pair with
    ``publish_agent_done`` in a try/finally."""
    await _publish(session_id, "agent_running", {"agent": agent})


async def publish_agent_done(
    session_id: uuid.UUID,
    agent: str,
    *,
    ok: bool = True,
) -> None:
    """Mark an agent as finished. ``ok=False`` lets the UI surface
    a transient warning (e.g. clarifier fell back)."""
    await _publish(session_id, "agent_done", {"agent": agent, "ok": ok})


async def _publish(
    session_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Best-effort publish — never raise; if the bus is misbehaving we
    log and continue. The agents must not crash because SSE failed."""
    try:
        await event_bus.publish(
            str(session_id),
            SSEEvent(event_type=event_type, data=data),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "couldn't publish %s for session=%s; continuing",
            event_type,
            session_id,
        )


# ── Handover-generation events ──────────────────────


def handover_channel_key(scope_kind: str, scope_value: str) -> str:
    """Stable event-bus channel for one handover scope. We key by
    (kind, value) rather than doc_id because the doc row is created
    DURING generation — the UI needs to subscribe at click time
    when only the scope is known."""
    return f"handover:{scope_kind}:{scope_value}"


async def publish_handover_event(
    scope_kind: str,
    scope_value: str,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Emit a generation-progress event on the scope's channel.

    Event types emitted:
      ``scope_resolved`` — manifest of repos/sources known.
      ``child_started`` — per-repo child generation kicked off (tree path).
      ``child_done`` — per-repo child finished (success or failure).
      ``compose_started`` — parent-composer Sonnet call started.
      ``compose_done`` — parent-composer Sonnet call finished.
      ``fs_written`` — Markdown atomic-write committed to disk.
      ``complete`` — terminal, success path. Carries ``doc_id``.
      ``error`` — terminal, failure path. Carries ``reason``.

    Best-effort — never raise.
    """
    try:
        await event_bus.publish(
            handover_channel_key(scope_kind, scope_value),
            SSEEvent(event_type=event_type, data=data or {}),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "couldn't publish handover event %s/%s/%s; continuing",
            scope_kind,
            scope_value,
            event_type,
        )
