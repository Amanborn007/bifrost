"""Environment-driven settings; one config/<region>.env file per deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    region: str
    signing_keys_host: str
    max_attempts: int
    breaker_failure_threshold: int
    breaker_reset_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            region=os.environ["REGION"],
            signing_keys_host=os.environ["SIGNING_KEYS_HOST"],
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", "5")),
            breaker_failure_threshold=int(os.environ.get("BREAKER_FAILURE_THRESHOLD", "5")),
            breaker_reset_seconds=float(os.environ.get("BREAKER_RESET_SECONDS", "30")),
        )
