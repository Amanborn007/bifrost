"""Answer synthesis — merges multi-agent results into a unified response.

Handles confidence weighting, source deduplication, conflict resolution,
and the single-repo passthrough case (no synthesis needed).
"""

from __future__ import annotations

import logging

from bifrost.agents.runtime import TaskResult
from bifrost.core.enums import ConfidenceLevel
from bifrost.orchestration.task import OrchestratedTaskResult

logger = logging.getLogger(__name__)

# Confidence ordering for weighting/comparison (higher index = higher confidence)
_CONFIDENCE_ORDER: list[str] = [
    ConfidenceLevel.UNKNOWN,
    ConfidenceLevel.CONFLICTING,
    ConfidenceLevel.UNCERTAIN,
    ConfidenceLevel.STALE,
    ConfidenceLevel.INFERRED,
    ConfidenceLevel.CONFIRMED,
]


def _confidence_rank(level: str) -> int:
    """Return numeric rank for a confidence level (higher = more confident)."""
    try:
        return _CONFIDENCE_ORDER.index(level)
    except ValueError:
        return 0


def _min_confidence(levels: list[str]) -> str:
    """Return the lowest confidence level from a list."""
    if not levels:
        return ConfidenceLevel.UNKNOWN
    return min(levels, key=_confidence_rank)


def _dedup_urns(urn_lists: list[list[str]]) -> list[str]:
    """Merge and deduplicate source URNs, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for urns in urn_lists:
        for urn in urns:
            if urn not in seen:
                seen.add(urn)
                result.append(urn)
    return result


def _detect_conflicts(
    results: dict[str, TaskResult],
) -> list[dict[str, object]]:
    """Detect conflicting answers across agents.

    Two results conflict when they both succeed but report different
    confidence levels on the same topic (different answers to the same question).
    """
    conflicts: list[dict[str, object]] = []
    successful = {urn: r for urn, r in results.items() if r.success and r.answer}
    urns = list(successful.keys())

    for i, urn_a in enumerate(urns):
        for urn_b in urns[i + 1 :]:
            result_a = successful[urn_a]
            result_b = successful[urn_b]
            # If both have answers but significantly different confidence,
            # flag as a potential conflict for the coordinator to resolve
            rank_a = _confidence_rank(result_a.confidence_level)
            rank_b = _confidence_rank(result_b.confidence_level)
            if abs(rank_a - rank_b) >= 2:
                conflicts.append(
                    {
                        "repo_a": urn_a,
                        "repo_b": urn_b,
                        "confidence_a": result_a.confidence_level,
                        "confidence_b": result_b.confidence_level,
                        "type": "confidence_divergence",
                    }
                )
    return conflicts


def synthesize(results: dict[str, TaskResult]) -> OrchestratedTaskResult:
    """Merge multi-repo agent results into a single orchestrated result.

    Single-repo case: passthrough with no synthesis overhead.
    Multi-repo case: confidence-weighted merge, source dedup, conflict detection.
    """
    successful = {urn: r for urn, r in results.items() if r.success}
    failed = {urn: r for urn, r in results.items() if not r.success}

    if not successful:
        # All agents failed
        all_errors = []
        for r in failed.values():
            all_errors.extend(r.errors)
        return OrchestratedTaskResult(
            answer="Unable to answer — all consulted agents failed.",
            confidence=ConfidenceLevel.UNKNOWN,
            gaps=[f"Agent failure: {e}" for e in all_errors],
        )

    # Single-repo passthrough — no synthesis needed
    if len(successful) == 1 and not failed:
        urn, result = next(iter(successful.items()))
        return OrchestratedTaskResult(
            answer=result.answer or "",
            confidence=result.confidence_level,
            source_urns=result.source_urns,
            agent_results=[_result_to_dict(urn, result)],
        )

    # Single success with some failures — include gaps
    if len(successful) == 1:
        urn, result = next(iter(successful.items()))
        gaps: list[str] = []
        for f_urn, f_result in failed.items():
            gaps.append(f"No answer from {f_urn}: {', '.join(f_result.errors)}")
        return OrchestratedTaskResult(
            answer=result.answer or "",
            confidence=result.confidence_level,
            source_urns=result.source_urns,
            agent_results=[_result_to_dict(u, r) for u, r in results.items()],
            gaps=gaps,
        )

    # Multi-repo synthesis
    conflicts = _detect_conflicts(results)

    # Confidence-weighted answer assembly:
    # Higher confidence answers are placed first in the synthesis
    ranked = sorted(
        successful.items(),
        key=lambda pair: _confidence_rank(pair[1].confidence_level),
        reverse=True,
    )

    answer_parts: list[str] = []
    for _urn, result in ranked:
        if result.answer:
            answer_parts.append(result.answer)

    all_source_urns = _dedup_urns([r.source_urns for r in successful.values()])
    overall_confidence = _min_confidence([r.confidence_level for r in successful.values()])

    gaps: list[str] = []
    for urn, result in failed.items():
        gaps.append(f"No answer from {urn}: {', '.join(result.errors)}")

    return OrchestratedTaskResult(
        answer="\n\n".join(answer_parts),
        confidence=overall_confidence,
        source_urns=all_source_urns,
        agent_results=[_result_to_dict(u, r) for u, r in results.items()],
        conflicts=conflicts,
        gaps=gaps,
    )


def _result_to_dict(repo_urn: str, result: TaskResult) -> dict[str, object]:
    """Convert a per-agent TaskResult to a serializable dict."""
    return {
        "repo_urn": repo_urn,
        "agent_id": result.agent_id,
        "success": result.success,
        "answer": result.answer,
        "confidence": result.confidence_level,
        "source_urns": result.source_urns,
        "errors": result.errors,
    }
