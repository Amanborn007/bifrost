"""Exponential backoff policy for failed deliveries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_s: float = 2.0
    max_delay_s: float = 300.0

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before retry number `attempt` (1-based): 2s, 4s, 8s, ... capped."""
        return min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)

    def should_retry(self, attempt: int, status_code: int | None) -> bool:
        if attempt >= self.max_attempts:
            return False
        return status_code is None or status_code >= 500 or status_code == 429
