"""Server-Sent Events module for streaming task progress.

Uses asyncio.Queue for backpressure — each connected client gets its own
queue. The task coordinator publishes events via broadcast(); connected
clients receive them through their individual queues.

Replay buffer
-------------
Tab switches, flaky wifi, and hot-reloads all close an ``EventSource``.
The browser auto-reconnects with a brand-new subscription — and by
default gets a fresh queue, missing every event that went out while it
was gone. Users see "nothing happening" and re-send the same question.

To fix: each task_id keeps a ring buffer of its last N events (default
200) with a sliding TTL (default 5 minutes). A new subscriber first
drains the buffered events in order, then receives live ones. Terminal
events (``done``, ``error``, ``assistant_message``) survive the TTL so
a late subscriber still sees the end-of-stream and closes cleanly.

Per TRD §4.4: event types are task.created, status.changed,
consultation.started, consultation.result, synthesis.started, task.delivered.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.stdlib.get_logger(__name__)


# Default buffer settings. Individual tasks can tune via constructor args.
_DEFAULT_BUFFER_SIZE = 200
_DEFAULT_BUFFER_TTL_S = 5 * 60
_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"done", "error", "assistant_message", "task_complete", "task_failed"},
)


class SSEEvent(BaseModel):
    """A single SSE event ready for wire format."""

    event_type: str
    data: dict[str, Any]


class _BufferedEvent:
    """Event + timestamp for TTL-aware replay."""

    __slots__ = ("event", "at")

    def __init__(self, event: SSEEvent, at: float) -> None:
        self.event = event
        self.at = at


class TaskEventBus:
    """Per-task event bus with subscriber queues + replay buffer.

    Subscribers (SSE clients) each get their own queue for backpressure.
    A ring buffer per task_id holds the last N events so reconnecting
    clients can catch up without the server having to keep them alive
    indefinitely.
    """

    def __init__(
        self,
        max_queue_size: int = 100,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
        buffer_ttl_s: int = _DEFAULT_BUFFER_TTL_S,
    ) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[SSEEvent | None]]] = {}
        self._buffers: dict[str, deque[_BufferedEvent]] = {}
        self._terminal_at: dict[str, float] = {}
        self._max_queue_size = max_queue_size
        self._buffer_size = buffer_size
        self._buffer_ttl_s = buffer_ttl_s

    def subscribe(self, task_id: str) -> asyncio.Queue[SSEEvent | None]:
        """Create a new subscriber queue for a task.

        Drains the existing replay buffer into the new queue *before*
        returning it, so the subscriber's first reads deliver any
        events the client missed while disconnected. Subsequent live
        events are broadcast as normal.
        """
        queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue(
            maxsize=max(self._max_queue_size, self._buffer_size + 10),
        )
        self._subscribers.setdefault(task_id, []).append(queue)
        replayed = 0
        for buffered in self._buffer_snapshot(task_id):
            try:
                queue.put_nowait(buffered.event)
                replayed += 1
            except asyncio.QueueFull:
                break
        # If this task already completed, send the close sentinel so
        # the late client cleans up instead of hanging forever.
        if task_id in self._terminal_at:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        logger.debug("sse.subscribe", task_id=task_id, replayed=replayed)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[SSEEvent | None]) -> None:
        """Remove a subscriber queue."""
        subs = self._subscribers.get(task_id, [])
        if queue in subs:
            subs.remove(queue)
        if not subs:
            self._subscribers.pop(task_id, None)
        logger.debug("sse.unsubscribe", task_id=task_id)

    async def publish(self, task_id: str, event: SSEEvent) -> None:
        """Broadcast + buffer an event.

        Uses put_nowait with backpressure — if a queue is full, the event
        is dropped for that subscriber (slow consumer protection). The
        replay buffer always records the event so late subscribers can
        still catch up.

        Multi-turn reset: when a NON-terminal event arrives for a
        task_id whose previous turn already wrote a terminal event,
        we treat that as "a new turn has started" and reset the
        buffer + ``terminal_at`` flag so a subscriber connecting now
        sees the fresh turn's events rather than the prior turn's
        stale done + close-sentinel pair. Critical for chat
        conversations where each message is a turn but the
        subscription key (conversation_id) is shared.
        """
        if task_id in self._terminal_at and event.event_type not in _TERMINAL_EVENT_TYPES:
            self._buffers.pop(task_id, None)
            self._terminal_at.pop(task_id, None)
        self._buffer_push(task_id, event)
        for queue in self._subscribers.get(task_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "sse.queue_full",
                    task_id=task_id,
                    event_type=event.event_type,
                )

    async def close(self, task_id: str) -> None:
        """Signal all subscribers for a task to disconnect (send sentinel None).

        Keeps the replay buffer alive for ``buffer_ttl_s`` so a client
        that reconnects shortly after completion still sees the tail.
        """
        self._terminal_at[task_id] = time.monotonic()
        for queue in self._subscribers.get(task_id, []):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        self._subscribers.pop(task_id, None)

    # ── Replay buffer internals ────────────────────────────────

    def _buffer_push(self, task_id: str, event: SSEEvent) -> None:
        buf = self._buffers.get(task_id)
        if buf is None:
            buf = deque(maxlen=self._buffer_size)
            self._buffers[task_id] = buf
        buf.append(_BufferedEvent(event=event, at=time.monotonic()))
        if event.event_type in _TERMINAL_EVENT_TYPES:
            self._terminal_at.setdefault(task_id, time.monotonic())

    def _buffer_snapshot(self, task_id: str) -> list[_BufferedEvent]:
        buf = self._buffers.get(task_id)
        if not buf:
            return []
        now = time.monotonic()
        # Evict TTL-expired events eagerly. Keep terminal events though:
        # if the task finished 6 minutes ago, the tail still needs to be
        # visible to a late reconnect. The dict entry itself is cleaned
        # up by ``sweep()`` below.
        expired_cutoff = now - self._buffer_ttl_s
        while buf and buf[0].at < expired_cutoff:
            if buf[0].event.event_type in _TERMINAL_EVENT_TYPES:
                break
            buf.popleft()
        return list(buf)

    def sweep(self) -> int:
        """Drop replay buffers for tasks whose terminal event is old.

        Returns the count of task_ids purged. Call from a periodic
        janitor; not called automatically to keep this module free of
        background tasks.
        """
        now = time.monotonic()
        cutoff = now - self._buffer_ttl_s
        stale: list[str] = [tid for tid, at in self._terminal_at.items() if at < cutoff]
        for tid in stale:
            # Only purge when no live subscribers (avoid racing with a
            # client mid-replay).
            if self._subscribers.get(tid):
                continue
            self._buffers.pop(tid, None)
            self._terminal_at.pop(tid, None)
        return len(stale)


# Module-level singleton — shared across the application
event_bus = TaskEventBus()


async def event_stream(task_id: str) -> AsyncGenerator[str, None]:
    """Generate SSE-formatted text for a task's event stream.

    Yields ``event: {type}\\ndata: {json}\\n\\n`` frames plus a
    ``: keepalive\\n\\n`` SSE comment every ``_HEARTBEAT_INTERVAL_S``
    seconds. Comment frames are required to keep
    proxies (nginx default ``proxy_read_timeout`` 60s, cloudflare
    100s, AWS ALB 60s) from closing the connection during long
    coordinator turns. The comment is invisible to the client's
    ``EventSource`` — it parses but doesn't dispatch — but it counts
    as bytes-on-the-wire for the proxy's idle timer.

    The ``asyncio.wait_for`` pattern lets us wake up periodically
    without blocking on the queue indefinitely. Terminates when the
    task completes (None sentinel) or the client disconnects.
    """
    _HEARTBEAT_INTERVAL_S = 15.0  # well below all common proxy timeouts

    queue = event_bus.subscribe(task_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(),
                    timeout=_HEARTBEAT_INTERVAL_S,
                )
            except TimeoutError:
                # No event in the last interval — emit a comment frame
                # to keep the connection alive. Loop continues.
                yield ": keepalive\n\n"
                continue
            if event is None:
                # Task completed — close the stream
                return
            yield f"event: {event.event_type}\ndata: {_json_dumps(event.data)}\n\n"
    finally:
        event_bus.unsubscribe(task_id, queue)


def _json_dumps(data: dict[str, Any]) -> str:
    """Compact JSON serialization."""
    import json

    return json.dumps(data, default=str, separators=(",", ":"))
