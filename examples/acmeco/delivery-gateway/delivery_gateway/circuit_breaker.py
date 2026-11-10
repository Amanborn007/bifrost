"""Per-endpoint circuit breaker so one dead customer URL cannot hold up workers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class CircuitOpen(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_seconds: float = 30.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def before_call(self, endpoint_url: str) -> None:
        opened = self._opened_at.get(endpoint_url)
        if opened is None:
            return
        if time.monotonic() - opened < self.reset_seconds:
            raise CircuitOpen(endpoint_url)
        del self._opened_at[endpoint_url]
        self._failures[endpoint_url] = 0

    def record_success(self, endpoint_url: str) -> None:
        self._failures[endpoint_url] = 0

    def record_failure(self, endpoint_url: str) -> None:
        count = self._failures.get(endpoint_url, 0) + 1
        self._failures[endpoint_url] = count
        if count >= self.failure_threshold:
            self._opened_at[endpoint_url] = time.monotonic()
