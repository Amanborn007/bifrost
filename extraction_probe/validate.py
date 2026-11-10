"""Pydantic validation models for extraction output — aligned with TRD §2 entity fields."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class InterfaceResult(BaseModel):
    name: str
    type: Literal["api", "event", "schema", "config"]
    description: str


class ComponentExtractionResult(BaseModel):
    name: str
    type: Literal["module", "service", "library", "package"]
    purpose: str = Field(min_length=5)
    files: list[str] = Field(min_length=1)
    interfaces: list[InterfaceResult] = Field(default_factory=list)
    internal_deps: list[str] = Field(default_factory=list)
    confidence: Literal["confirmed", "inferred"]
    known_gaps: list[str] = Field(default_factory=list)


class FlowSegmentResult(BaseModel):
    order: int = Field(ge=1)
    component_name: str
    segment_type: Literal[
        "entry", "internal", "external-call", "async-hop", "branch", "fallback", "side-effect"
    ]
    description: str


class FlowExtractionResult(BaseModel):
    name: str
    flow_type: Literal[
        "request-response", "event-driven", "batch", "saga", "pipeline", "mixed"
    ]
    entry_point: str
    segments: list[FlowSegmentResult] = Field(min_length=1)
    confidence: Literal["confirmed", "inferred"]
    known_gaps: list[str] = Field(default_factory=list)


def validate_component_result(data: dict) -> ComponentExtractionResult | None:
    """Validate a component dict against the Pydantic model. Returns None on failure."""
    try:
        return ComponentExtractionResult.model_validate(data)
    except (ValidationError, AttributeError, TypeError) as e:
        name = data.get("name", "?") if isinstance(data, dict) else "?"
        print(f"  Component validation failed: {e} — {name}")
        return None


def validate_flow_result(data: dict) -> FlowExtractionResult | None:
    """Validate a flow dict against the Pydantic model. Returns None on failure."""
    try:
        return FlowExtractionResult.model_validate(data)
    except (ValidationError, AttributeError, TypeError) as e:
        name = data.get("name", "?") if isinstance(data, dict) else "?"
        print(f"  Flow validation failed: {e} — {name}")
        return None
