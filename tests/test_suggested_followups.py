"""Round-trip tests for the suggested follow-up prompts feature.

Validates that ``suggested_followups`` persisted on an assistant
``conversation_messages`` row survives a DB round-trip and serializes
correctly through the conversation GET response schema (``MessageData``).

This is the persistence + API-exposure half of the end-to-end feature
(coordinator → SSE → worker → DB → API → UI).
"""

from __future__ import annotations

import pytest

from bifrost.api.schemas.conversations import MessageData
from bifrost.storage.repository import EntityRepository

pytestmark = pytest.mark.asyncio


async def test_suggested_followups_round_trip(db_session) -> None:
    """append_message persists suggested_followups and list_messages
    returns them verbatim."""
    repo = EntityRepository(db_session)
    conv = await repo.create_conversation(
        session_id="test-session-followups",
        title="followup round-trip",
    )

    followups = [
        {
            "prompt": "How does order-service call the inventory repo?",
            "rationale": "inventory was not walked but is load-bearing",
            "repos_to_index": ["inventory"],
        },
        {
            "prompt": "Re-index notification-svc (remote moved).",
            "rationale": "stale extraction",
            "repos_to_index": ["notification-svc"],
        },
    ]

    await repo.append_message(
        conversation_id=conv.id,
        role="assistant",
        content="Here is the answer.",
        confidence="inferred",
        suggested_followups=followups,
    )
    await db_session.flush()

    messages = await repo.list_messages(conv.id)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.suggested_followups == followups

    # Serialize through the API response schema the conversation GET
    # endpoint uses — proves the field reaches the wire.
    wire = MessageData(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        turn_index=msg.turn_index,
        role="assistant",
        content=msg.content,
        confidence=msg.confidence,
        suggested_followups=[s for s in (msg.suggested_followups or []) if isinstance(s, dict)],
        created_at=msg.created_at,
    )
    dumped = wire.model_dump()
    assert dumped["suggested_followups"] == followups
    assert dumped["suggested_followups"][0]["prompt"].startswith("How does")

    await db_session.rollback()


async def test_suggested_followups_defaults_empty(db_session) -> None:
    """A turn with no follow-ups persists/serializes as an empty list,
    not null."""
    repo = EntityRepository(db_session)
    conv = await repo.create_conversation(
        session_id="test-session-followups-empty",
        title="followup empty",
    )
    await repo.append_message(
        conversation_id=conv.id,
        role="user",
        content="What does the auth flow look like?",
    )
    await db_session.flush()

    messages = await repo.list_messages(conv.id)
    assert messages[0].suggested_followups == []

    wire = MessageData(
        id=str(messages[0].id),
        conversation_id=str(messages[0].conversation_id),
        turn_index=messages[0].turn_index,
        role="user",
        content=messages[0].content,
        created_at=messages[0].created_at,
    )
    assert wire.suggested_followups == []

    await db_session.rollback()
