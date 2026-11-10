"""Environment-driven settings; one config/<region>.env file per deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    region: str
    rotation_grace_hours: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            region=os.environ["REGION"],
            rotation_grace_hours=int(os.environ.get("ROTATION_GRACE_HOURS", "24")),
        )
