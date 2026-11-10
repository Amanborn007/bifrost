"""Turns one queued event into one DeliveryJob per matching subscription."""

from __future__ import annotations

from dispatcher.models import DeliveryJob, QueuedEvent
from dispatcher.subscription_index import SubscriptionIndex


def build_jobs(event: QueuedEvent, index: SubscriptionIndex) -> list[DeliveryJob]:
    return [
        DeliveryJob(
            event_id=event.event_id,
            subscription_id=t.subscription_id,
            endpoint_url=t.endpoint_url,
            event_type=event.event_type,
            payload=event.payload,
        )
        for t in index.targets_for(event.account_id, event.event_type)
    ]
