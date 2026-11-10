"""Tests for the in-process event bus."""

from __future__ import annotations

import asyncio

import pytest

from bifrost.core.events import (
    EntityCreated,
    EntityUpdated,
    Event,
    EventBus,
    ExtractionComplete,
    FreshnessChanged,
    TaskStateChanged,
)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


class TestEventBusSubscription:
    async def test_subscriber_receives_published_event(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EntityCreated, handler)
        event = EntityCreated(entity_id="e1", entity_type="component", urn="urn:bifrost:c:1")
        await bus.publish(event)
        await bus.drain()

        assert len(received) == 1
        assert received[0] is event

    async def test_multiple_subscribers_per_event_type(self, bus: EventBus) -> None:
        received_a: list[Event] = []
        received_b: list[Event] = []

        async def handler_a(event: Event) -> None:
            received_a.append(event)

        async def handler_b(event: Event) -> None:
            received_b.append(event)

        bus.subscribe(EntityCreated, handler_a)
        bus.subscribe(EntityCreated, handler_b)

        await bus.publish(EntityCreated(entity_id="e1"))
        await bus.drain()

        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_subscriber_only_gets_matching_event_type(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EntityCreated, handler)
        # Publish a different event type
        await bus.publish(EntityUpdated(entity_id="e1"))
        await bus.drain()

        assert len(received) == 0

    async def test_event_ordering_preserved(self, bus: EventBus) -> None:
        received: list[str] = []

        async def handler(event: Event) -> None:
            assert isinstance(event, EntityCreated)
            received.append(event.entity_id)

        bus.subscribe(EntityCreated, handler)
        for i in range(5):
            await bus.publish(EntityCreated(entity_id=f"e{i}"))
        await bus.drain()

        assert received == ["e0", "e1", "e2", "e3", "e4"]


class TestEventBusBackgroundLoop:
    async def test_start_stop_lifecycle(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EntityCreated, handler)
        await bus.start()
        await bus.publish(EntityCreated(entity_id="bg1"))
        # Give the background task a moment to process
        await asyncio.sleep(0.05)
        await bus.stop()

        assert len(received) == 1

    async def test_handler_error_does_not_stop_bus(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def failing_handler(event: Event) -> None:
            raise ValueError("boom")

        async def good_handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EntityCreated, failing_handler)
        bus.subscribe(EntityCreated, good_handler)

        await bus.publish(EntityCreated(entity_id="e1"))
        await bus.drain()

        # Good handler still ran despite the error in failing_handler
        assert len(received) == 1


class TestEventTypes:
    def test_all_event_types_are_frozen_dataclasses(self) -> None:
        for cls in (
            EntityCreated,
            EntityUpdated,
            ExtractionComplete,
            FreshnessChanged,
            TaskStateChanged,
        ):
            event = cls()
            assert hasattr(event, "event_id")
            assert hasattr(event, "timestamp")
            with pytest.raises(AttributeError):
                event.event_id = "cannot-set"  # type: ignore[misc]

    def test_extraction_complete_fields(self) -> None:
        event = ExtractionComplete(
            repo_id="r1", task_id="t1", components_found=5, flows_found=3, dependencies_found=10
        )
        assert event.repo_id == "r1"
        assert event.components_found == 5
