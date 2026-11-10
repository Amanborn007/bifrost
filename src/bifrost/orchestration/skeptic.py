"""Adversarial skeptic — challenges low-confidence synthesized answers.

A fresh LLM call (no shared context) reviews the synthesis output against
raw evidence. Checks for unsupported claims, contradictions, and overreach.

Per spec S9 §10.7: optional stage between synthesizing and delivered.
Default: enabled for assess_impact, plan, trace_flow task types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bifrost.core.enums import ConfidenceLevel
from bifrost.extraction.cli_client import ClaudeLlmClient
from bifrost.orchestration.task import OrchestratedTaskResult, QueryType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

SKEPTIC_SYSTEM_PROMPT = """\
You are a skeptical reviewer for an engineering knowledge platform. Your job is
to find unsupported claims, contradictions, or overreach in a synthesized answer.

Review the answer against the provided evidence. For each issue found, explain:
1. What claim is problematic
2. Why (unsupported, contradicted, or overreach)
3. A suggested correction

Use the review_answer tool to report your findings.
"""

REVIEW_TOOL_SCHEMA: dict[str, Any] = {
    "name": "review_answer",
    "description": "Report issues found in the synthesized answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "passed": {
                "type": "boolean",
                "description": "True if the answer is well-supported, false if issues found.",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "problem": {
                            "type": "string",
                            "enum": ["unsupported", "contradicted", "overreach"],
                        },
                        "explanation": {"type": "string"},
                        "correction": {"type": "string"},
                    },
                    "required": ["claim", "problem", "explanation"],
                },
                "description": "List of issues found. Empty if passed=true.",
            },
            "confidence_adjustment": {
                "type": "string",
                "enum": ["none", "downgrade_one", "downgrade_two"],
                "description": "How much to reduce confidence based on issues found.",
            },
        },
        "required": ["passed", "issues", "confidence_adjustment"],
    },
}

# Confidence ordering for downgrade logic
_CONFIDENCE_ORDER: list[str] = [
    ConfidenceLevel.UNKNOWN,
    ConfidenceLevel.CONFLICTING,
    ConfidenceLevel.UNCERTAIN,
    ConfidenceLevel.STALE,
    ConfidenceLevel.INFERRED,
    ConfidenceLevel.CONFIRMED,
]

# Default: skeptic runs on these task types
DEFAULT_SKEPTIC_TASK_TYPES: set[QueryType] = {
    QueryType.ASSESS_IMPACT,
    QueryType.PLAN,
    QueryType.TRACE_FLOW,
}


@dataclass
class SkepticConfig:
    """Configuration for adversarial skeptic."""

    enabled: bool = True
    task_types: set[QueryType] = field(default_factory=lambda: DEFAULT_SKEPTIC_TASK_TYPES)
    confidence_threshold: str = ConfidenceLevel.INFERRED  # challenge if below this
    max_challenge_rounds: int = 1
    model: str = "claude-sonnet-4-6"


@dataclass
class SkepticResult:
    """Outcome of adversarial review."""

    passed: bool
    issues: list[dict[str, str]] = field(default_factory=list)
    original_confidence: str = ""
    adjusted_confidence: str = ""
    rounds_used: int = 0


def _downgrade_confidence(current: str, steps: int) -> str:
    """Downgrade confidence by N steps in the ordering."""
    try:
        idx = _CONFIDENCE_ORDER.index(current)
    except ValueError:
        return ConfidenceLevel.UNKNOWN
    new_idx = max(0, idx - steps)
    return _CONFIDENCE_ORDER[new_idx]


def should_challenge(
    result: OrchestratedTaskResult,
    task_type: QueryType,
    config: SkepticConfig,
) -> bool:
    """Determine whether the skeptic should challenge this result."""
    if not config.enabled:
        return False
    if task_type not in config.task_types:
        return False
    try:
        threshold_rank = _CONFIDENCE_ORDER.index(config.confidence_threshold)
        result_rank = _CONFIDENCE_ORDER.index(result.confidence)
    except ValueError:
        return True  # Unknown confidence always gets challenged
    return result_rank < threshold_rank


async def challenge(
    client: ClaudeLlmClient,
    result: OrchestratedTaskResult,
    config: SkepticConfig | None = None,
) -> SkepticResult:
    """Run the adversarial skeptic against a synthesized result.

    This is a FRESH LLM call with no prior context — the skeptic sees only
    the synthesis output and source URNs, not the original agent reasoning.
    """
    if config is None:
        config = SkepticConfig()

    user_content = (
        f"Synthesized answer:\n{result.answer}\n\n"
        f"Confidence level: {result.confidence}\n\n"
        f"Source URNs cited: {', '.join(result.source_urns) if result.source_urns else 'none'}\n\n"
        f"Conflicts detected: {len(result.conflicts)}\n"
        f"Gaps detected: {len(result.gaps)}\n\n"
        "Review this answer for unsupported claims, contradictions, or overreach."
    )

    completion = await client.call_with_json_schema(
        system_prompt=SKEPTIC_SYSTEM_PROMPT,
        user_message=user_content,
        model=config.model,
        json_schema=REVIEW_TOOL_SCHEMA["input_schema"],
    )
    review_data = completion.parsed

    passed = review_data.get("passed", True)
    issues = review_data.get("issues", [])
    adjustment = review_data.get("confidence_adjustment", "none")

    # Apply confidence adjustment
    steps = {"none": 0, "downgrade_one": 1, "downgrade_two": 2}.get(adjustment, 0)
    adjusted = _downgrade_confidence(result.confidence, steps) if steps > 0 else result.confidence

    return SkepticResult(
        passed=passed,
        issues=issues,
        original_confidence=result.confidence,
        adjusted_confidence=adjusted,
        rounds_used=1,
    )


def apply_skeptic_result(
    result: OrchestratedTaskResult,
    skeptic: SkepticResult,
) -> OrchestratedTaskResult:
    """Apply skeptic findings to the orchestrated result.

    Modifies the result in place and returns it for chaining.
    """
    if not skeptic.passed:
        result.confidence = skeptic.adjusted_confidence
        for issue in skeptic.issues:
            result.gaps.append(
                f"Skeptic: {issue.get('problem', 'issue')} — {issue.get('claim', 'unknown claim')}"
            )
    return result
