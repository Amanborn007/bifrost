"""Publishes one usage record per delivery attempt to BILLING_TOPIC."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from dispatcher.models import DeliveryJob


@dataclass
class BillingPublisher:
    topic: str
    published: list[tuple[str, str]] = field(default_factory=list)

    def record_delivery(self, account_id: str, job: DeliveryJob, delivered: bool) -> None:
        message = {
            "account_id": account_id,
            "event_id": job.event_id,
            "subscription_id": job.subscription_id,
            "delivered": delivered,
        }
        self.published.append((self.topic, json.dumps(message, sort_keys=True)))
