"""Request and storage models for subscriptions and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Subscription:
    id: str
    account_id: str
    endpoint_url: str
    event_types: list[str]
    region: str
    active: bool = True


@dataclass
class IncomingEvent:
    id: str
    account_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
