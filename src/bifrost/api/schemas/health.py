"""Health endpoint schemas per TRD §4.7."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CheckStatus(BaseModel):
    status: str
    latency_ms: float


class HealthChecks(BaseModel):
    database: CheckStatus
    redis: CheckStatus
    adapters: dict[str, CheckStatus]


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    version: str
    track: Literal["A", "B"]
    checks: HealthChecks
    uptime_seconds: float
