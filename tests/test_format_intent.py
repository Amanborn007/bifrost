"""Tests for the format-intent detector + synthesis-prompt directive injection.

Regression: user asked for "one-page,
no fluff", got a 1500-word dossier with all the verbose blocks. The
fix has two layers:

  1. ``detect_format_intent`` — regex-classifies the user's question as
     ``concise`` / ``executive`` / ``single_decision`` / ``None``.
  2. ``_build_synthesis_user_message`` — injects a top-of-message
     FORMAT DIRECTIVE block when an intent is detected, overriding the
     default dossier structure for that turn.

Pure functions — no DB, no event loop.
"""

from __future__ import annotations

import pytest

from bifrost.research.coordinator import (
    _FORMAT_DIRECTIVE_BLOCKS,
    _build_synthesis_user_message,
    detect_format_intent,
)

# ── detect_format_intent ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question,expected",
    [
        # concise
        ("Give me a one-page handover for DLVR", "concise"),
        ("Write a 1-page summary, no fluff", "concise"),
        ("TLDR what does OrderService do?", "concise"),
        ("TL;DR: what is DLVR?", "concise"),
        ("quick summary of the EU stack", "concise"),
        ("Cut the fluff and tell me what DLVR owns", "concise"),
        # executive
        ("Executive summary of the EU rollout", "executive"),
        ("Leadership update on DLVR progress", "executive"),
        # single_decision
        ("In one sentence, what does DLVR do?", "single_decision"),
        ("Just the headline — what's broken?", "single_decision"),
        ("Bottom line: should we ship?", "single_decision"),
        # default (None)
        ("How does OrderService integrate with TokenService?", None),
        ("What does the DLVR team do?", None),
        ("Walk me through the entire EU flow with diagrams", None),
        ("", None),
    ],
)
def test_detect_format_intent_classifies_correctly(
    question: str,
    expected: str | None,
) -> None:
    assert detect_format_intent(question) == expected


def test_priority_single_decision_beats_concise() -> None:
    """When both 'TLDR' and 'in one sentence' appear, the more specific
    single_decision intent wins."""
    q = "TLDR — in one sentence, what does OrderService do?"
    assert detect_format_intent(q) == "single_decision"


def test_priority_executive_beats_concise() -> None:
    """If both 'executive summary' and 'one-page' appear, executive wins (tighter)."""
    q = "One-page executive summary of DLVR"
    assert detect_format_intent(q) == "executive"


# ── synthesis-prompt directive injection ─────────────────────────────────────


def test_synthesis_prompt_includes_directive_when_concise_intent() -> None:
    msg = _build_synthesis_user_message(
        question="Write a one-page handover, no fluff",
        bundles=[],
    )
    assert "FORMAT OVERRIDE — CONCISE MODE" in msg
    assert "≤300 words" in msg
    assert "DO NOT EMIT these sections" in msg


def test_synthesis_prompt_no_directive_for_dossier_question() -> None:
    msg = _build_synthesis_user_message(
        question="Walk me through the full EU signing stack with diagrams",
        bundles=[],
    )
    assert "FORMAT OVERRIDE" not in msg
    assert "FORMAT DIRECTIVE" not in msg


def test_synthesis_prompt_includes_executive_directive() -> None:
    msg = _build_synthesis_user_message(
        question="Executive summary of DLVR progress this quarter",
        bundles=[],
    )
    assert "FORMAT DIRECTIVE — EXECUTIVE MODE" in msg
    assert "≤200 words" in msg


def test_synthesis_prompt_includes_single_decision_directive() -> None:
    msg = _build_synthesis_user_message(
        question="In one sentence, who owns OrderService?",
        bundles=[],
    )
    assert "FORMAT DIRECTIVE — SINGLE-DECISION MODE" in msg
    assert "≤100 words" in msg


def test_directive_blocks_cover_all_intents() -> None:
    """Sanity: every detector output (except None) has a directive."""
    expected = {"concise", "executive", "single_decision"}
    assert set(_FORMAT_DIRECTIVE_BLOCKS.keys()) == expected
    for label, block in _FORMAT_DIRECTIVE_BLOCKS.items():
        # Concise uses "FORMAT OVERRIDE" (strengthened wording);
        # executive + single_decision still use "FORMAT DIRECTIVE".
        assert "FORMAT DIRECTIVE" in block or "FORMAT OVERRIDE" in block, (
            f"{label} missing directive header"
        )
        assert "OMIT" in block or "NO" in block or "DO NOT" in block, f"{label} missing exclusions"
