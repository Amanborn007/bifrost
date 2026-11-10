"""Tests for the _stream_with_heartbeat helper used during synthesis.

The frontend watchdog fires after 5 minutes of SSE silence. Synthesis
on a long answer can stall 60-120s between Claude tokens. The helper
emits a sentinel ``{"type": "_heartbeat"}`` every interval_s of silence
so the frontend stays liveness-armed across token gaps.
"""

from __future__ import annotations

import asyncio

import pytest

from bifrost.research.coordinator import _stream_with_heartbeat


async def _slow_stream(items: list[dict], gap_s: float = 0.0):
    """Yield the items one at a time with ``gap_s`` between each."""
    for it in items:
        if gap_s > 0:
            await asyncio.sleep(gap_s)
        yield it


@pytest.mark.asyncio
async def test_passthrough_when_no_silence():
    """Items arriving rapidly should pass through with no heartbeats."""
    items = [{"type": "delta", "text": "a"}, {"type": "delta", "text": "b"}]
    out = []
    async for evt in _stream_with_heartbeat(_slow_stream(items, 0.0), interval_s=1.0):
        out.append(evt)
    assert out == items


@pytest.mark.asyncio
async def test_heartbeat_emitted_on_silence():
    """A gap longer than interval_s should produce at least one heartbeat."""
    # 0.25s interval, 0.4s gap → at least 1 heartbeat between consecutive
    # items (the wrapper has no view into the underlying generator's
    # cadence, so heartbeats can fire before the first item too).
    items = [{"type": "delta", "text": "a"}, {"type": "delta", "text": "b"}]
    out = []
    async for evt in _stream_with_heartbeat(_slow_stream(items, 0.4), interval_s=0.25):
        out.append(evt)
    # All real items arrive in order, just possibly interleaved with
    # heartbeats.
    real = [e for e in out if e.get("type") != "_heartbeat"]
    assert real == items
    heartbeats = [e for e in out if e.get("type") == "_heartbeat"]
    assert len(heartbeats) >= 1


@pytest.mark.asyncio
async def test_multiple_heartbeats_on_long_silence():
    """A 0.6s gap with 0.2s interval should emit at least 2 heartbeats."""
    items = [{"type": "delta", "text": "x"}, {"type": "delta", "text": "y"}]
    out = []
    async for evt in _stream_with_heartbeat(_slow_stream(items, 0.6), interval_s=0.2):
        out.append(evt)
    heartbeats = [e for e in out if e.get("type") == "_heartbeat"]
    assert len(heartbeats) >= 2


@pytest.mark.asyncio
async def test_producer_error_propagates():
    """If the underlying stream raises, the error should propagate."""

    async def _raising():
        yield {"type": "delta", "text": "a"}
        raise RuntimeError("boom")

    seen = []
    with pytest.raises(RuntimeError, match="boom"):
        async for evt in _stream_with_heartbeat(_raising(), interval_s=0.5):
            seen.append(evt)

    assert seen == [{"type": "delta", "text": "a"}]


@pytest.mark.asyncio
async def test_clean_completion_no_trailing_heartbeat():
    """When the stream ends cleanly, no extra heartbeat should follow."""
    items = [{"type": "delta", "text": "z"}, {"type": "done"}]
    out = []
    async for evt in _stream_with_heartbeat(_slow_stream(items, 0.0), interval_s=10.0):
        out.append(evt)
    # Last item should be the done event, not a heartbeat
    assert out[-1] == {"type": "done"}
