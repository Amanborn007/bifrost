"""Event bus — async event publishing for domain events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class EventBus:
    """Simple async event bus for publishing domain events.

    In production this would be backed by Kafka, SQS, or similar.
    """

    _handlers: dict[str, list[Callable[..., Awaitable[None]]]] = field(
        default_factory=dict
    )
    _published: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def subscribe(self, event_type: str, handler: Callable[..., Awaitable[None]]) -> None:
        """Register a handler for an event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event to all registered handlers."""
        self._published.append((event_type, payload))
        for handler in self._handlers.get(event_type, []):
            await handler(payload)
