"""Standard response envelope -- wraps all API responses per S10 section 6.

S10 (interfaces.md) section 6 mandates the response envelope carry
confidence, freshness, evidence_summary, gaps, conflicts, cache_status,
data_source, task_id, and duration_ms in addition to the request_id /
timestamp / track baseline. These were absent on every
route, breaking the spec's load-bearing trust contract.

All new fields are Optional with sensible defaults, so existing routes
that only populate the baseline three fields keep working unchanged.
Routes that have richer data should populate the corresponding meta
fields. See routes/ask.py for the reference implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

T = TypeVar("T")


class EnvelopeMeta(BaseModel):
    """Response metadata -- the spec's trust contract.

    Routes are expected to populate whatever subset they have honest
    answers for. A None value means "the route did not measure this,"
    not "the value is zero or empty."
    """

    # Baseline identity fields (every route gets these).
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    track: Literal["A", "B"] = "A"

    # S10 trust-contract fields. All optional; populate when known.
    task_id: str | None = None
    duration_ms: float | None = None
    confidence: str | None = None  # ConfidenceLevel value
    freshness: str | None = None  # "fresh" | "stale" | "unknown" | etc.
    evidence_summary: str | None = None
    gaps: list[str] | None = None
    conflicts: list[dict[str, Any]] | None = None
    cache_status: Literal["live", "cached", "stale-revalidating"] | None = None
    data_source: str | None = None  # "extraction" | "cache" | "live-llm" | etc.

    # Confidence gate (S9). When the answer's confidence falls at/below the
    # configured floor, ``gated`` is True and the answer should be treated
    # as needs-review rather than authoritative. ``gate_reason`` explains it.
    gated: bool = False
    gate_reason: str | None = None


class BifrostEnvelope(BaseModel, Generic[T]):  # noqa: UP046 -- Pydantic requires Generic[]
    data: T
    meta: EnvelopeMeta = Field(default_factory=EnvelopeMeta)


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    detail: dict[str, object] | None = None


class BifrostErrorResponse(BaseModel):
    error: ErrorDetail
    meta: EnvelopeMeta = Field(default_factory=EnvelopeMeta)
