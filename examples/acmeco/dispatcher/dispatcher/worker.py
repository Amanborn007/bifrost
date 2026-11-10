"""Consumes DELIVERY_QUEUE and drives fan-out, delivery, and usage reporting."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from dispatcher.billing import BillingPublisher
from dispatcher.config import Settings
from dispatcher.fanout import build_jobs
from dispatcher.gateway_client import GatewayClient
from dispatcher.models import QueuedEvent
from dispatcher.subscription_index import SubscriptionIndex

logger = logging.getLogger(__name__)


@dataclass
class DispatchWorker:
    settings: Settings
    index: SubscriptionIndex
    gateway: GatewayClient
    billing: BillingPublisher

    def handle_event(self, event: QueuedEvent) -> int:
        """Fan one event out to every matching subscription; returns jobs sent."""
        jobs = build_jobs(event, self.index)
        for job in jobs:
            delivered = self.gateway.deliver(job)
            self.billing.record_delivery(event.account_id, job, delivered)
        logger.info("event %s fanned out to %d endpoints", event.event_id, len(jobs))
        return len(jobs)


def build_worker(settings: Settings) -> DispatchWorker:
    return DispatchWorker(
        settings=settings,
        index=SubscriptionIndex(db_url=settings.subscription_db_url),
        gateway=GatewayClient(host=settings.delivery_gateway_host),
        billing=BillingPublisher(topic=settings.billing_topic),
    )


def run(settings: Settings, queue_messages: list[dict]) -> int:
    """Process a batch pulled from settings.delivery_queue."""
    worker = build_worker(settings)
    sent = 0
    for raw in queue_messages[: settings.max_batch]:
        sent += worker.handle_event(QueuedEvent(**raw))
    return sent
