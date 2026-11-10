"""Environment-driven settings; one config/<region>.env file per deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    region: str
    delivery_queue: str
    max_payload_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            region=os.environ["REGION"],
            delivery_queue=os.environ["DELIVERY_QUEUE"],
            max_payload_bytes=int(os.environ.get("MAX_PAYLOAD_BYTES", "262144")),
        )
