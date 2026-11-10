"""Thin client for the internal message queue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueueClient:
    """Publishes JSON messages to a named queue.

    The in-memory list stands in for the broker SDK in this sample.
    """

    sent: list[tuple[str, str]] = field(default_factory=list)

    def publish(self, queue_name: str, message: dict[str, Any]) -> None:
        self.sent.append((queue_name, json.dumps(message, sort_keys=True)))
