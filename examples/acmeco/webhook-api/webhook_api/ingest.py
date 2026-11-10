"""Event ingest: validate, then hand off to the dispatcher via DELIVERY_QUEUE."""

from __future__ import annotations

import json
from dataclasses import dataclass

from webhook_api.config import Settings
from webhook_api.models import IncomingEvent
from webhook_api.queue_client import QueueClient


class PayloadTooLarge(ValueError):
    pass


@dataclass
class IngestService:
    settings: Settings
    queue: QueueClient

    def accept(self, event: IncomingEvent) -> str:
        """Validate the event and enqueue it for fan-out; returns the event id."""
        size = len(json.dumps(event.payload))
        if size > self.settings.max_payload_bytes:
            raise PayloadTooLarge(f"payload is {size} bytes")
        self.queue.publish(
            self.settings.delivery_queue,
            {
                "event_id": event.id,
                "account_id": event.account_id,
                "event_type": event.event_type,
                "region": self.settings.region,
                "payload": event.payload,
            },
        )
        return event.id
