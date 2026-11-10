"""Build-intent follow-up chips key off generic verbs only."""

from types import SimpleNamespace

import pytest

from bifrost.research.coordinator import _generate_followups
from tests import leak_terms

CHIP = "What tests do I need to write for orders-api?"


def _prompts(question: str) -> list[str]:
    out = _generate_followups(
        answer="",
        question=question,
        useful_bundles=[],
        rankings=[SimpleNamespace(repo_name="orders-api")],
    )
    return [s["prompt"] for s in out]


@pytest.mark.parametrize("word", ["build", "implement", "add", "create", "jira", "ticket"])
def test_generic_build_words_offer_test_guidance(word: str) -> None:
    assert CHIP in _prompts(f"{word} the thing")


def test_no_banned_term_is_a_build_intent_signal() -> None:
    hits = [t for t in leak_terms.terms() if CHIP in _prompts(f"{t} status")]
    assert hits == []
