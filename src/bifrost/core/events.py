"""In-process event bus — asyncio.Queue-based, fire-and-forget.

Provides typed events and a pub/sub bus for loose coupling between
extraction, freshness updates, and observability subsystems.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.stdlib.get_logger(__name__)


# ── Event types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """Base event. All events carry a unique id and timestamp."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class EntityCreated(Event):
    entity_id: str = ""
    entity_type: str = ""
    urn: str = ""
    repo_id: str = ""


@dataclass(frozen=True)
class EntityUpdated(Event):
    entity_id: str = ""
    entity_type: str = ""
    urn: str = ""
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionComplete(Event):
    repo_id: str = ""
    task_id: str = ""
    components_found: int = 0
    flows_found: int = 0
    dependencies_found: int = 0


@dataclass(frozen=True)
class FreshnessChanged(Event):
    repo_id: str = ""
    old_state: str = ""
    new_state: str = ""
    reason: str = ""


@dataclass(frozen=True)
class TaskStateChanged(Event):
    task_id: str = ""
    old_status: str = ""
    new_status: str = ""


# ── Subscriber type ───────────────────────────────────────────────────

Subscriber = Callable[[Event], Coroutine[Any, Any, None]]


# ── EventBus ──────────────────────────────────────────────────────────


class EventBus:
    """Async event bus using asyncio.Queue for non-blocking dispatch.

    Subscribers register for specific event types. Publishing puts the
    event on an internal queue; a background task drains the queue and
    dispatches to all matching subscribers. Publishers are never blocked.
    """

    def __init__(self) -> None:
        self._subscribers: dict[type[Event], list[Subscriber]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        self._dispatch_task: asyncio.Task[None] | None = None

    def subscribe(self, event_type: type[Event], handler: Subscriber) -> None:
        """Register a handler for a specific event type."""
        self._subscribers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        """Enqueue an event for async dispatch. Non-blocking for the publisher."""
        await self._queue.put(event)

    async def start(self) -> None:
        """Start the background dispatch loop."""
        if self._running:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        """Stop the dispatch loop, draining remaining events first."""
        self._running = False
        if self._dispatch_task is not None:
            # Put a sentinel to unblock the queue.get()
            await self._queue.put(_SENTINEL)  # type: ignore[arg-type]
            await self._dispatch_task
            self._dispatch_task = None

    async def drain(self) -> None:
        """Process all currently queued events. Useful in tests."""
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if event is _SENTINEL:  # type: ignore[comparison-overlap]
                break
            await self._dispatch_one(event)

    async def _dispatch_loop(self) -> None:
        """Background loop: pull events from queue, dispatch to subscribers."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if event is _SENTINEL:  # type: ignore[comparison-overlap]
                break
            await self._dispatch_one(event)

    async def _dispatch_one(self, event: Event) -> None:
        """Dispatch a single event to all registered subscribers."""
        event_type = type(event)
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "event.handler_error",
                    event_type=event_type.__name__,
                    event_id=event.event_id,
                )


# Sentinel object to signal the dispatch loop to stop
_SENTINEL = object()
