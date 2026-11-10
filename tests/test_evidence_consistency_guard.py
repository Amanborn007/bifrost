"""Tests for the evidence-consistency guard.

"headline=confirmed but body admits coverage gap" is a
trust-breaker for readers. The guard
is the deterministic safety net that downgrades ``confirmed`` →
``inferred`` whenever a per-source agent or the answer body itself
contains a gap marker.

Pure-function tests — no DB, no event loop.
"""

from __future__ import annotations

from types import SimpleNamespace

from bifrost.research.coordinator import (
    _has_evidence_gap_in_answer,
    _has_evidence_gap_in_bundles,
    apply_evidence_consistency_guard,
)


def _bundle(
    *,
    repo_name: str = "AcmeApp",
    code=None,
    jira=None,
    confluence=None,
    github=None,
    slack=None,
):
    """Minimal RepoBundle-shaped object for the guard's walker."""
    return SimpleNamespace(
        repo_id="b" * 32,
        repo_name=repo_name,
        code=code,
        jira=jira,
        confluence=confluence,
        github=github,
        slack=slack,
    )


def _agent(
    *,
    relevance: str = "medium",
    error: str | None = None,
    gaps: list[str] | None = None,
):
    """Minimal per-source-agent shape."""
    return SimpleNamespace(
        relevance=relevance,
        error=error,
        gaps=gaps or [],
    )


# ── answer-text gap detection ────────────────────────────────────────────────


def test_answer_text_gap_search_failed() -> None:
    a = "TL;DR: works.\n### Source coverage\n- Slack: search failed."
    assert _has_evidence_gap_in_answer(a) == "search failed"


def test_answer_text_gap_not_routed() -> None:
    a = "Note: RulesEngine and Scheduler were not routed this turn."
    assert _has_evidence_gap_in_answer(a) == "not routed"


def test_answer_text_gap_speculative() -> None:
    a = "Documented but not code-verified. Treat as speculative."
    # First match wins; both needles are present, the function returns
    # whichever appears earliest in the iteration order.
    out = _has_evidence_gap_in_answer(a)
    assert out in {"documented but not code-verified", "treat as speculative"}


def test_answer_text_no_gap_returns_none() -> None:
    a = "Clean answer. Cited urn:bifrost:repo/OrderService. All sources contributed."
    assert _has_evidence_gap_in_answer(a) is None


def test_answer_text_empty_returns_none() -> None:
    assert _has_evidence_gap_in_answer("") is None
    assert _has_evidence_gap_in_answer(None) is None  # type: ignore[arg-type]


# ── bundle gap detection ─────────────────────────────────────────────────────


def test_bundle_gap_when_agent_has_error() -> None:
    bundles = [
        _bundle(
            slack=_agent(relevance="medium", error="rate limited"),
        )
    ]
    out = _has_evidence_gap_in_bundles(bundles)
    assert out is not None
    assert "AcmeApp.slack" in out
    assert "rate limited" in out


def test_bundle_skipped_relevance_is_not_a_gap() -> None:
    """A 'skipped' agent (no slack channel configured) is structural,
    not a coverage failure. Don't downgrade."""
    bundles = [
        _bundle(
            slack=_agent(relevance="skipped"),
            code=_agent(relevance="high"),
        )
    ]
    assert _has_evidence_gap_in_bundles(bundles) is None


def test_bundle_gap_from_code_agent_self_reported_gaps() -> None:
    bundles = [
        _bundle(
            code=_agent(
                relevance="medium",
                gaps=["RulesEngine repo not walked this turn"],
            ),
        )
    ]
    out = _has_evidence_gap_in_bundles(bundles)
    assert out is not None
    assert "not walked" in out


def test_bundle_clean_returns_none() -> None:
    bundles = [
        _bundle(
            code=_agent(relevance="high"),
            jira=_agent(relevance="medium"),
        )
    ]
    assert _has_evidence_gap_in_bundles(bundles) is None


# ── full guard integration ───────────────────────────────────────────────────


def test_guard_downgrades_confirmed_to_inferred_on_answer_gap() -> None:
    bundles = [_bundle(code=_agent(relevance="high"))]
    answer = "Headline says confirmed but Source coverage says: Slack search failed."
    out, debug = apply_evidence_consistency_guard(
        base_confidence="confirmed",
        bundles=bundles,
        answer_text=answer,
    )
    assert out == "inferred"
    assert debug["downgraded"] is True
    assert debug["answer_gap"] == "search failed"


def test_guard_downgrades_confirmed_on_bundle_gap_even_without_answer_marker() -> None:
    """Even if the LLM forgot to surface the failure in its answer
    body, a per-source agent error in the bundles still triggers the
    downgrade."""
    bundles = [_bundle(jira=_agent(relevance="medium", error="timeout"))]
    answer = "Clean answer body, no caveats mentioned."
    out, debug = apply_evidence_consistency_guard(
        base_confidence="confirmed",
        bundles=bundles,
        answer_text=answer,
    )
    assert out == "inferred"
    assert debug["downgraded"] is True
    assert debug["bundle_gap"] is not None


def test_guard_does_not_change_inferred_or_uncertain() -> None:
    """The guard only downgrades; it never flips a lower tier upward."""
    bundles = [_bundle(jira=_agent(relevance="medium", error="timeout"))]
    answer = "Source coverage: Jira search failed."
    for base in ("inferred", "uncertain"):
        out, debug = apply_evidence_consistency_guard(
            base_confidence=base,
            bundles=bundles,
            answer_text=answer,
        )
        assert out == base
        assert debug["downgraded"] is False


def test_guard_keeps_confirmed_when_coverage_is_clean() -> None:
    bundles = [_bundle(code=_agent(relevance="high"), jira=_agent(relevance="medium"))]
    answer = "Citations all check out: urn:bifrost:repo/OrderService cited correctly."
    out, debug = apply_evidence_consistency_guard(
        base_confidence="confirmed",
        bundles=bundles,
        answer_text=answer,
    )
    assert out == "confirmed"
    assert debug["downgraded"] is False


# ── failure-mode regression cases ────────────────────────────────────────────


def test_not_routed_sources_downgrade_confirmed() -> None:
    """RetryPolicy/RulesEngine 'not routed' but
    headline was 'confirmed'. The guard must catch this."""
    bundles = [_bundle(code=_agent(relevance="high"))]
    answer = (
        "TL;DR: The retry policy uses predicates with thresholds at retry_backoff_ms=850.\n"
        "### Source coverage\n"
        "- Code: contributed for 2 repos.\n"
        "- RetryPolicy / RulesEngine: not routed this turn — falling back to "
        "Confluence/Jira coverage.\n"
    )
    out, _ = apply_evidence_consistency_guard(
        base_confidence="confirmed",
        bundles=bundles,
        answer_text=answer,
    )
    assert out == "inferred"


def test_unknown_upstream_caller_downgrades_confirmed() -> None:
    """'exact upstream caller unknown' but answered as if known."""
    bundles = [_bundle(code=_agent(relevance="high"))]
    answer = (
        "Dispatcher writes the delivery status row. The exact upstream caller "
        "unknown — this synthesis describes the sink-side handoff."
    )
    out, _ = apply_evidence_consistency_guard(
        base_confidence="confirmed",
        bundles=bundles,
        answer_text=answer,
    )
    assert out == "inferred"
