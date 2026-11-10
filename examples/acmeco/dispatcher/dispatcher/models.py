"""Messages consumed from DELIVERY_QUEUE and units of work sent downstream."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueuedEvent:
    event_id: str
    account_id: str
    event_type: str
    region: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Target:
    subscription_id: str
    endpoint_url: str
    event_types: list[str]


@dataclass
class DeliveryJob:
    event_id: str
    subscription_id: str
    endpoint_url: str
    event_type: str
    payload: dict[str, Any]
