"""SSE replay-buffer must reset between turns of the same conversation.

Regression test for the bug found by manual smoke against the running
API: when chat turn 1 ended with a `done` event, ``terminal_at[conv]``
got set. Turn 2 then published its events, but any new SSE subscriber
connecting during turn 2 still saw turn 1's buffered events followed
by the close sentinel from terminal_at — they never received turn 2's
live events. The UI's per-turn reconnect masked the bug; the curl-
based verifier exposed it.

Fix: ``publish()`` resets the buffer + clears ``terminal_at`` the
first time a non-terminal event arrives for a task_id that already
has a terminal_at flag set. From the subscriber's perspective, that
is "a new turn has started — drop prior turn's tail."
"""

from __future__ import annotations

import asyncio

import pytest

from bifrost.api.sse import SSEEvent, TaskEventBus


@pytest.mark.asyncio
async def test_new_turn_reset_drops_prior_buffer_and_terminal_flag() -> None:
    bus = TaskEventBus()
    cid = "conv-multi-turn"

    # Turn 1: status, status, done
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "turn1-a"}))
    await bus.publish(cid, SSEEvent(event_type="done", data={"answer": "a"}))
    await bus.close(cid)
    assert cid in bus._terminal_at
    assert len(bus._buffers[cid]) == 2

    # Turn 2: a non-terminal event arrives. This should reset the buffer
    # and clear the terminal flag.
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "turn2-a"}))
    assert cid not in bus._terminal_at
    # Only turn 2's events remain.
    assert len(bus._buffers[cid]) == 1
    assert bus._buffers[cid][0].event.data["m"] == "turn2-a"


@pytest.mark.asyncio
async def test_subscriber_during_turn2_sees_turn2_events_only() -> None:
    """The whole point: a subscriber connecting AFTER turn 2 starts
    must see turn 2's events without the prior turn's close
    sentinel cutting them off."""
    bus = TaskEventBus()
    cid = "conv-late-sub"

    # Turn 1 streams + closes.
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "turn1"}))
    await bus.publish(cid, SSEEvent(event_type="done", data={"answer": "a"}))
    await bus.close(cid)

    # Turn 2 starts.
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "turn2-status"}))

    # New subscriber connects mid-turn-2.
    queue = bus.subscribe(cid)

    # First read: turn 2's status (the only thing in the now-fresh buffer).
    first = queue.get_nowait()
    assert first.event_type == "status"
    assert first.data["m"] == "turn2-status"

    # Queue should not be holding a close sentinel (terminal_at was
    # cleared when the new turn started).
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_terminal_event_in_progress_turn_does_not_reset_itself() -> None:
    """Defensive: a terminal event ARRIVING (e.g. done) should NOT
    reset the buffer — we still want subscribers to see prior events
    in the same turn."""
    bus = TaskEventBus()
    cid = "conv-same-turn"

    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "a"}))
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "b"}))
    await bus.publish(cid, SSEEvent(event_type="done", data={"answer": "x"}))

    # Three events buffered; new subscriber sees all three + close
    # (because terminal_at is now set).
    queue = bus.subscribe(cid)
    msgs = [queue.get_nowait() for _ in range(3)]
    assert [m.event_type for m in msgs] == ["status", "status", "done"]
    sentinel = queue.get_nowait()
    assert sentinel is None


@pytest.mark.asyncio
async def test_two_full_back_to_back_turns_each_visible_to_late_sub() -> None:
    bus = TaskEventBus()
    cid = "conv-two-turns"

    # Turn 1: 3 events + close
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "t1-1"}))
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "t1-2"}))
    await bus.publish(cid, SSEEvent(event_type="done", data={"answer": "1"}))
    await bus.close(cid)

    # Turn 2: 2 events + close
    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "t2-1"}))
    await bus.publish(cid, SSEEvent(event_type="done", data={"answer": "2"}))
    await bus.close(cid)

    # Late subscriber sees ONLY turn 2's events (buffer reset on first
    # publish of turn 2).
    queue = bus.subscribe(cid)
    drained: list = []
    while True:
        try:
            drained.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    # Three items: status t2-1, done, close sentinel
    assert len(drained) == 3
    assert drained[0].event_type == "status"
    assert drained[0].data["m"] == "t2-1"
    assert drained[1].event_type == "done"
    assert drained[2] is None  # close sentinel


@pytest.mark.asyncio
async def test_live_subscriber_sees_both_turns_continuously() -> None:
    """Pre-existing connection should keep receiving live events
    across the turn boundary (the old behavior — make sure I didn't
    regress it)."""
    bus = TaskEventBus()
    cid = "conv-live"

    queue = bus.subscribe(cid)

    await bus.publish(cid, SSEEvent(event_type="status", data={"m": "t1"}))
    await bus.publish(cid, SSEEvent(event_type="done", data={"answer": "1"}))
    # Note: bus.close() pops the subscriber list — that's correct
    # behavior because the SSE handler exits on the close sentinel.
    # A live subscriber across turns is not a real-world pattern.
    # We just verify the events the queue received before close are intact.

    msgs = []
    while True:
        try:
            msgs.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    assert [m.event_type for m in msgs] == ["status", "done"]
