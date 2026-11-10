"""Tests for the /api/v1/ask confidence gate (S9).

The skeptic already downgrades confidence upstream; the gate is the
missing decision point that flags a low-confidence answer as
needs-review instead of returning it as authoritative.
"""

from __future__ import annotations

import pytest

from bifrost.api.routes.ask import _DEFAULT_GATE_FLOOR, _evaluate_gate


@pytest.mark.parametrize(
    "confidence,expected_gated",
    [
        ("unknown", True),
        ("conflicting", True),
        ("uncertain", True),  # == default floor → gated (at-or-below)
        ("stale", False),
        ("inferred", False),
        ("confirmed", False),
    ],
)
def test_default_floor_gates_low_confidence(
    confidence: str,
    expected_gated: bool,
) -> None:
    gated, reason = _evaluate_gate(confidence, None)
    assert gated is expected_gated
    if expected_gated:
        assert reason is not None and "needs-review" in reason
    else:
        assert reason is None


def test_default_floor_is_uncertain() -> None:
    assert _DEFAULT_GATE_FLOOR == "uncertain"


def test_custom_floor_raises_the_bar() -> None:
    # floor=inferred → inferred and everything below it is gated;
    # only confirmed clears the bar.
    assert _evaluate_gate("inferred", "inferred")[0] is True
    assert _evaluate_gate("stale", "inferred")[0] is True
    assert _evaluate_gate("confirmed", "inferred")[0] is False


def test_unrecognized_confidence_is_treated_as_lowest() -> None:
    # A value not in the ranking ranks 0 (lowest) → always gated.
    gated, reason = _evaluate_gate("garbage-value", None)
    assert gated is True
    assert reason is not None


def test_case_insensitive() -> None:
    assert _evaluate_gate("CONFIRMED", None)[0] is False
    assert _evaluate_gate("Unknown", None)[0] is True
