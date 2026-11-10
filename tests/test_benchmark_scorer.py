"""Unit tests for ``bifrost.benchmarks.scorer``.

Pure-function module — no DB, no event loop. Each test exercises one
scoring concern in isolation so a regression points straight at its
component (recall, precision, specificity, coverage, tier_integrity).
"""

from __future__ import annotations

import pytest

from bifrost.benchmarks.scorer import (
    DEFAULT_WEIGHTS,
    ScoreInput,
    score_answer,
)

# ── recall ───────────────────────────────────────────────────────────────────


def test_recall_full_match() -> None:
    inp = ScoreInput(
        answer_text=("DLVR uses urn:bifrost:repo/Dispatcher and urn:bifrost:repo/SigningKeys."),
        expected_entities=[
            "urn:bifrost:repo/Dispatcher",
            "urn:bifrost:repo/SigningKeys",
        ],
    )
    out = score_answer(inp)
    assert out.recall == 1.0
    assert out.missing_entities == []


def test_recall_partial_miss() -> None:
    inp = ScoreInput(
        answer_text="urn:bifrost:repo/Dispatcher is the main service.",
        expected_entities=[
            "urn:bifrost:repo/Dispatcher",
            "urn:bifrost:repo/SigningKeys",
        ],
    )
    out = score_answer(inp)
    assert out.recall == 0.5
    assert out.missing_entities == ["urn:bifrost:repo/SigningKeys"]


def test_recall_no_expectations_is_vacuous_full() -> None:
    """If the question has no expected_entities, recall is 1.0."""
    inp = ScoreInput(answer_text="anything goes", expected_entities=[])
    out = score_answer(inp)
    assert out.recall == 1.0


# ── precision ────────────────────────────────────────────────────────────────


def test_precision_on_topic_full() -> None:
    inp = ScoreInput(
        answer_text="urn:bifrost:repo/Dispatcher is the answer.",
        expected_entities=["urn:bifrost:repo/Dispatcher"],
    )
    out = score_answer(inp)
    assert out.precision == 1.0


def test_precision_drops_with_off_topic_urn() -> None:
    """An answer that cites both the right URN and an unrelated one
    scores 0.5 precision — half of cited URNs are on-topic."""
    inp = ScoreInput(
        answer_text=("urn:bifrost:service:Dispatcher and urn:bifrost:service:Random."),
        expected_entities=["urn:bifrost:service:Dispatcher"],
    )
    out = score_answer(inp)
    assert out.precision == 0.5


def test_precision_repo_prefix_relaxation() -> None:
    """Citing components of an expected service-URN repo counts as
    on-topic — answers legitimately go deeper than the expected set."""
    inp = ScoreInput(
        answer_text=(
            "Top-level: urn:bifrost:service:Dispatcher. Components: "
            "urn:bifrost:repo/Dispatcher/component/SenderInfo, "
            "urn:bifrost:repo/Dispatcher/component/TierLimit."
        ),
        expected_entities=["urn:bifrost:service:Dispatcher"],
    )
    out = score_answer(inp)
    # All three URNs are Dispatcher-prefixed → on-topic
    assert out.precision == 1.0


def test_precision_repo_prefix_does_not_credit_wrong_repo() -> None:
    """A component in a NOT-expected repo doesn't get credit."""
    inp = ScoreInput(
        answer_text=(
            "urn:bifrost:service:Dispatcher and urn:bifrost:repo/SomeOtherRepo/component/Foo"
        ),
        expected_entities=["urn:bifrost:service:Dispatcher"],
    )
    out = score_answer(inp)
    assert out.precision == 0.5


def test_precision_one_hop_adjacency_credits_neighbour() -> None:
    """A neighbour of the expected entity is treated as on-topic."""
    inp = ScoreInput(
        answer_text=("urn:bifrost:repo/Dispatcher and urn:bifrost:repo/SigningKeys"),
        expected_entities=["urn:bifrost:repo/Dispatcher"],
        one_hop_neighbours={
            "urn:bifrost:repo/Dispatcher": {"urn:bifrost:repo/SigningKeys"},
        },
    )
    out = score_answer(inp)
    assert out.precision == 1.0


def test_precision_zero_when_no_citations() -> None:
    """No URNs at all → 0.0 precision (the answer isn't grounded)."""
    inp = ScoreInput(
        answer_text="The thing happens because of code.",
        expected_entities=["urn:bifrost:repo/Dispatcher"],
    )
    out = score_answer(inp)
    assert out.precision == 0.0


# ── specificity ──────────────────────────────────────────────────────────────


def test_specificity_counts_jira_pr_file_refs() -> None:
    """PR is referenced as ``#N`` to avoid ``PR-N`` colliding with the
    Jira regex (which would mark it as a jira ticket too)."""
    text = """
- The flow goes through Dispatcher (`order_router.py:42`).
- See #1234 and DLVR-456 for the original change.
- Mermaid is fine to have but unrelated here.
"""
    inp = ScoreInput(answer_text=text)
    out = score_answer(inp)
    assert 0.0 < out.specificity <= 1.0
    breakdown = out.breakdown["specificity"]
    assert breakdown["jira_refs"] == 1
    assert breakdown["pr_refs"] == 1
    assert breakdown["file_line_refs"] == 1


def test_specificity_zero_for_vague_answer() -> None:
    """An answer with no refs at all scores specificity 0."""
    inp = ScoreInput(
        answer_text=("It does the thing. The thing does the other thing. Sometimes it caches."),
    )
    out = score_answer(inp)
    assert out.specificity == 0.0


# ── coverage ─────────────────────────────────────────────────────────────────


def test_coverage_diagram_required_and_present() -> None:
    inp = ScoreInput(
        answer_text="prelude\n```mermaid\ngraph TD;A-->B\n```\nepilogue",
        expects_diagram=True,
    )
    out = score_answer(inp)
    assert out.coverage == 1.0


def test_coverage_diagram_required_but_missing() -> None:
    inp = ScoreInput(
        answer_text="No diagram here, just prose.",
        expects_diagram=True,
    )
    out = score_answer(inp)
    assert out.coverage == 0.0


def test_coverage_key_facts_partial() -> None:
    inp = ScoreInput(
        answer_text="Dispatcher owns the retry logic and dispatches via delivery-gateway.",
        expected_key_facts=[
            {"needle": "owns the retry", "why": "ownership clarity"},
            {"needle": "fingerprinting", "why": "device fingerprint flow"},
        ],
    )
    out = score_answer(inp)
    # 1 of 2 needles found → coverage = 0.5
    assert out.coverage == pytest.approx(0.5)
    assert out.missing_key_facts == [
        {"needle": "fingerprinting", "why": "device fingerprint flow"},
    ]


def test_coverage_no_requirements_is_full() -> None:
    inp = ScoreInput(answer_text="anything", expects_diagram=False)
    out = score_answer(inp)
    assert out.coverage == 1.0


# ── tier_integrity ───────────────────────────────────────────────────────────


def test_tier_integrity_meets_floor() -> None:
    inp = ScoreInput(
        answer_text="x",
        answer_confidence="confirmed",
        tier_floor=1,
    )
    out = score_answer(inp)
    assert out.tier_integrity == 1.0


def test_tier_integrity_below_floor() -> None:
    """T1-floor question answered ``uncertain`` (T5) → 0.0."""
    inp = ScoreInput(
        answer_text="x",
        answer_confidence="uncertain",
        tier_floor=1,
    )
    out = score_answer(inp)
    assert out.tier_integrity == 0.0


def test_tier_integrity_partial_credit_for_honest_downgrade() -> None:
    """When evidence-consistency correctly downgrades confirmed→inferred
    (because the answer admits a coverage gap), tier_integrity should
    NOT be 0 — that would double-penalize the correct fix.
    """
    inp = ScoreInput(
        answer_text="Source coverage: Slack search failed. Inferred answer.",
        answer_confidence="inferred",
        tier_floor=1,  # T1 expected, T3 actual = 2-tier drop
    )
    out = score_answer(inp)
    # 0.5 partial credit because answer admits the gap
    assert out.tier_integrity == 0.5


def test_tier_integrity_no_partial_credit_without_gap_admission() -> None:
    """Same 2-tier drop but answer doesn't admit any coverage gap →
    0.0 (genuine under-confidence, not honest hedging).
    """
    inp = ScoreInput(
        answer_text="Some clean answer with no admissions whatsoever.",
        answer_confidence="inferred",
        tier_floor=1,
    )
    out = score_answer(inp)
    assert out.tier_integrity == 0.0


def test_tier_integrity_unknown_confidence_is_neutral() -> None:
    inp = ScoreInput(
        answer_text="x",
        answer_confidence=None,
        tier_floor=1,
    )
    out = score_answer(inp)
    assert out.tier_integrity == 0.5


# ── total + db row shape ─────────────────────────────────────────────────────


def test_total_is_weighted_blend() -> None:
    """A perfect-recall, perfect-precision, vague answer scores well
    on the entity components but middling on specificity. Total
    reflects the blend by ``DEFAULT_WEIGHTS``."""
    inp = ScoreInput(
        answer_text=(
            "urn:bifrost:repo/Dispatcher does the thing. urn:bifrost:repo/SigningKeys also does."
        ),
        expected_entities=[
            "urn:bifrost:repo/Dispatcher",
            "urn:bifrost:repo/SigningKeys",
        ],
        answer_confidence="confirmed",
        tier_floor=1,
    )
    out = score_answer(inp)
    assert out.recall == 1.0
    assert out.precision == 1.0
    assert out.tier_integrity == 1.0
    # Specificity is non-zero (2 URN refs) but low (only 2 sentences proxy).
    # Coverage is 1.0 (no requirements).
    # Evidence consistency: 1.0 (clean, confirmed-with-no-gaps)
    # Format adherence: 1.0 (no expectation set)
    expected = (
        DEFAULT_WEIGHTS["recall"] * 1.0
        + DEFAULT_WEIGHTS["precision"] * 1.0
        + DEFAULT_WEIGHTS["specificity"] * out.specificity
        + DEFAULT_WEIGHTS["coverage"] * 1.0
        + DEFAULT_WEIGHTS["tier_integrity"] * 1.0
        + DEFAULT_WEIGHTS["evidence_consistency"] * 1.0
        + DEFAULT_WEIGHTS["format_adherence"] * 1.0
    )
    assert out.total == pytest.approx(expected, abs=1e-6)


# ── evidence_consistency ─────────────────────────────────────────────────────


def test_evidence_consistency_clean_confirmed_scores_full() -> None:
    inp = ScoreInput(
        answer_text="Clean answer cites urn:bifrost:repo/Dispatcher. No issues.",
        answer_confidence="confirmed",
    )
    out = score_answer(inp)
    assert out.evidence_consistency == 1.0


def test_evidence_consistency_confirmed_with_gap_marker_scores_zero() -> None:
    """Regression: a retry-rules answer
    headlined 'confirmed' while body said 'not routed'."""
    inp = ScoreInput(
        answer_text=(
            "TL;DR: retry rules at retry_backoff_ms=850.\n"
            "Source coverage: RetryPolicy / RulesEngine: not routed this turn."
        ),
        answer_confidence="confirmed",
    )
    out = score_answer(inp)
    assert out.evidence_consistency == 0.0


def test_evidence_consistency_inferred_with_gap_is_fine() -> None:
    """Lower-tier headlines are vacuously consistent — they're not
    over-claiming."""
    inp = ScoreInput(
        answer_text="Slack search failed but here's what we know.",
        answer_confidence="inferred",
    )
    out = score_answer(inp)
    assert out.evidence_consistency == 1.0


def test_evidence_consistency_bundle_gap_overrides_clean_body() -> None:
    """Even if the LLM scrubbed the failure from the body, a per-source
    agent error in the bundle still penalizes a 'confirmed' headline."""
    inp = ScoreInput(
        answer_text="Confident clean body, no admissions.",
        answer_confidence="confirmed",
        bundle_has_gap=True,
    )
    out = score_answer(inp)
    assert out.evidence_consistency == 0.0


# ── format_adherence ─────────────────────────────────────────────────────────


def test_format_adherence_dossier_default_is_full() -> None:
    """No expectation set → score 1.0 regardless of length."""
    inp = ScoreInput(
        answer_text="X" * 5000,
        expected_format=None,
    )
    out = score_answer(inp)
    assert out.format_adherence == 1.0


def test_format_adherence_concise_within_cap() -> None:
    short = " ".join(["word"] * 200)
    inp = ScoreInput(
        answer_text=short,
        expected_format="concise",
    )
    out = score_answer(inp)
    assert out.format_adherence == 1.0


def test_format_adherence_concise_over_cap_decays_linearly() -> None:
    over = " ".join(["word"] * 450)  # cap=300, 1.5×cap → 0.5
    inp = ScoreInput(
        answer_text=over,
        expected_format="concise",
    )
    out = score_answer(inp)
    assert 0.4 <= out.format_adherence <= 0.6


def test_format_adherence_concise_at_2x_cap_zero() -> None:
    over = " ".join(["word"] * 600)  # cap=300, 2×cap → 0.0
    inp = ScoreInput(
        answer_text=over,
        expected_format="concise",
    )
    out = score_answer(inp)
    assert out.format_adherence == 0.0


def test_format_adherence_dossier_sections_in_concise_get_penalty() -> None:
    """Regression: user asked for
    'one-page handover, no fluff' and got the full dossier with all
    the verbose blocks."""
    body = " ".join(["word"] * 200) + (
        "\n### Source coverage\n- Slack: ok\n"
        "### Caveats & staleness\n- none\n"
        "### Cross-source alignment\n- consistent\n"
    )
    inp = ScoreInput(
        answer_text=body,
        expected_format="concise",
    )
    out = score_answer(inp)
    assert out.format_adherence < 0.5


def test_format_adherence_executive_uses_tighter_cap() -> None:
    """Executive caps at 200 words by default; 250 words is over cap
    so adherence should drop below 1.0."""
    over = " ".join(["w"] * 250)
    inp = ScoreInput(
        answer_text=over,
        expected_format="executive",
    )
    out = score_answer(inp)
    assert out.format_adherence < 1.0


def test_format_adherence_explicit_max_words_overrides_default() -> None:
    text = " ".join(["w"] * 250)
    inp = ScoreInput(
        answer_text=text,
        expected_format="concise",
        max_words=500,  # override default 300
    )
    out = score_answer(inp)
    assert out.format_adherence == 1.0


def test_as_db_row_returns_serializable_shape() -> None:
    inp = ScoreInput(
        answer_text="urn:bifrost:repo/Dispatcher handles DLVR-42.",
        expected_entities=["urn:bifrost:repo/Dispatcher"],
        expected_jira_refs=["DLVR-42"],
    )
    row = score_answer(inp).as_db_row()
    # All expected DB-row keys present
    for k in (
        "score_recall",
        "score_precision",
        "score_specificity",
        "score_coverage",
        "score_tier_integrity",
        "score_evidence_consistency",
        "score_format_adherence",
        "score_total",
        "cited_entity_urns",
        "cited_jira_refs",
        "cited_pr_refs",
        "missing_entities",
        "missing_key_facts",
        "raw_score_breakdown",
    ):
        assert k in row, f"missing key: {k}"
    assert "DLVR-42" in row["cited_jira_refs"]
    assert "urn:bifrost:repo/Dispatcher" in row["cited_entity_urns"]
