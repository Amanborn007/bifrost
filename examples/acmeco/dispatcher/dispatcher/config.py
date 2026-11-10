"""Environment-driven settings; one config/<region>.env file per deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    region: str
    delivery_queue: str
    delivery_gateway_host: str
    billing_topic: str
    subscription_db_url: str
    max_batch: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            region=os.environ["REGION"],
            delivery_queue=os.environ["DELIVERY_QUEUE"],
            delivery_gateway_host=os.environ["DELIVERY_GATEWAY_HOST"],
            billing_topic=os.environ["BILLING_TOPIC"],
            subscription_db_url=os.environ["SUBSCRIPTION_DB_URL"],
            max_batch=int(os.environ.get("MAX_BATCH", "50")),
        )
