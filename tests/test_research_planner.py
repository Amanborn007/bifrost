"""Follow-up planner — classification logic.

Tests the embedding short-circuit + LLM-fallback decision tree
without spinning up a real claude subprocess. The LLM classifier is
injected as a stub callable.
"""

from __future__ import annotations

import math

import pytest

from bifrost.research.bundle_cache import BundleSource, ResearchBundle
from bifrost.research.planner import (
    MODE_CLARIFY,
    MODE_EXTEND_BUNDLE,
    MODE_NEW_SCOPE,
    classify_followup,
    cosine_similarity,
    find_new_repo_mentions,
)


def _make_bundle(
    *,
    last_question: str = "how does orders retry?",
    last_question_embedding: list[float] | None = None,
    repos_used_names: list[str] | None = None,
) -> ResearchBundle:
    return ResearchBundle(
        conversation_id="conv-1",
        last_question=last_question,
        last_answer="answer body",
        confidence="confirmed",
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=repos_used_names or ["orders"],
        scope_rationale="x",
        subgraph_entity_urns=["bifrost://repo/orders/component/X"],
        sources=[BundleSource(kind="jira", ref="SHIP-1")],
        last_question_embedding=last_question_embedding,
    )


def _vec(*xs: float) -> list[float]:
    return list(xs)


def test_cosine_similarity_basic() -> None:
    assert cosine_similarity(_vec(1, 0, 0), _vec(1, 0, 0)) == pytest.approx(1.0)
    assert cosine_similarity(_vec(1, 0), _vec(0, 1)) == pytest.approx(0.0)
    val = cosine_similarity(_vec(1, 1), _vec(1, 0))
    assert val is not None and math.isclose(val, 1 / math.sqrt(2), rel_tol=1e-6)


def test_cosine_similarity_returns_none_for_invalid_inputs() -> None:
    assert cosine_similarity(None, _vec(1.0)) is None
    assert cosine_similarity(_vec(1.0), None) is None
    assert cosine_similarity([], _vec(1.0)) is None
    assert cosine_similarity(_vec(0, 0), _vec(0, 0)) is None
    assert cosine_similarity(_vec(1, 0), _vec(1, 0, 0)) is None


def test_find_new_repo_mentions_returns_only_known_repos() -> None:
    out = find_new_repo_mentions(
        "Does batch-orders share retry logic with notifications?",
        prior_repo_names=["orders"],
        available_repo_names=["orders", "batch-orders", "notifications", "auth"],
    )
    assert sorted(out) == ["batch-orders", "notifications"]


def test_find_new_repo_mentions_ignores_prior_repos() -> None:
    out = find_new_repo_mentions(
        "How does orders handle retries?",
        prior_repo_names=["orders"],
        available_repo_names=["orders", "auth"],
    )
    assert out == []


@pytest.mark.asyncio
async def test_no_prior_bundle_yields_new_scope() -> None:
    plan = await classify_followup(question="hi", prior_bundle=None)
    assert plan.mode == MODE_NEW_SCOPE
    assert "no prior bundle" in plan.reason


@pytest.mark.asyncio
async def test_high_similarity_no_new_repos_yields_clarify() -> None:
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))
    plan = await classify_followup(
        question="What did you mean by retry queue?",
        prior_bundle=bundle,
        question_embedding=_vec(1, 0, 0),  # identical
        available_repo_names=["orders"],
    )
    assert plan.mode == MODE_CLARIFY
    assert plan.answer_from_bundle is True
    assert plan.reused_bundle is bundle


@pytest.mark.asyncio
async def test_high_similarity_with_new_repo_yields_extend() -> None:
    bundle = _make_bundle(
        last_question_embedding=_vec(1, 0, 0),
        repos_used_names=["orders"],
    )
    plan = await classify_followup(
        question="What about batch-orders — does it use the same retry?",
        prior_bundle=bundle,
        question_embedding=_vec(1, 0, 0),  # high sim
        available_repo_names=["orders", "batch-orders", "auth"],
        available_repo_id_by_name={
            "orders": "id-orders",
            "batch-orders": "id-ach",
            "auth": "id-auth",
        },
    )
    assert plan.mode == MODE_EXTEND_BUNDLE
    assert plan.extension_repo_names == ["batch-orders"]
    assert plan.extension_repos == ["id-ach"]
    assert plan.reused_bundle is bundle


@pytest.mark.asyncio
async def test_low_similarity_yields_new_scope() -> None:
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))
    plan = await classify_followup(
        question="completely unrelated question",
        prior_bundle=bundle,
        question_embedding=_vec(0, 0, 1),  # orthogonal → cosine = 0.0, well below 0.35
        available_repo_names=["orders"],
    )
    assert plan.mode == MODE_NEW_SCOPE
    assert "low similarity" in plan.reason


@pytest.mark.asyncio
async def test_similarity_between_035_and_050_is_ambiguous_not_new_scope() -> None:
    """Regression: prior _LOW_SIMILARITY=0.50 mis-classified same-topic
    follow-ups (cosine ~0.40) as NEW_SCOPE, causing a full re-run.
    Since lowering to 0.35 these fall into the ambiguous band and are
    handled by heuristic-extend (or the LLM classifier when available)."""
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))
    # Build a vector with cosine ~0.40 relative to (1, 0, 0).
    # cos(θ) = dot/|a||b|; using (0.4, 0.917, 0) → cosine ≈ 0.40.
    plan = await classify_followup(
        question="what about the backoff strategy?",
        prior_bundle=bundle,
        question_embedding=_vec(0.4, 0.917, 0),
        available_repo_names=["orders"],
        llm_classifier=None,  # no LLM → heuristic-extend
    )
    # Must NOT be NEW_SCOPE — same-topic follow-ups should extend, not re-run.
    assert plan.mode == MODE_EXTEND_BUNDLE, (
        f"expected EXTEND_BUNDLE for a same-topic follow-up at ~0.40 cosine, "
        f"got {plan.mode!r} ({plan.reason})"
    )


@pytest.mark.asyncio
async def test_ambiguous_calls_llm_classifier() -> None:
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))
    seen: dict[str, object] = {}

    async def _stub(question, prior):
        seen["called"] = True
        seen["question"] = question
        return {"mode": MODE_CLARIFY, "reason": "stub-said-clarify"}

    plan = await classify_followup(
        question="middling similarity",
        prior_bundle=bundle,
        question_embedding=_vec(0.7, 0.7, 0),  # cosine ~0.70 → ambiguous
        available_repo_names=["orders"],
        llm_classifier=_stub,
    )

    assert seen.get("called") is True
    assert plan.mode == MODE_CLARIFY
    assert plan.answer_from_bundle is True
    assert "stub-said-clarify" in plan.reason


@pytest.mark.asyncio
async def test_ambiguous_llm_says_extend_with_known_repo() -> None:
    bundle = _make_bundle(
        last_question_embedding=_vec(1, 0, 0),
        repos_used_names=["orders"],
    )

    async def _stub(question, prior):
        return {"mode": MODE_EXTEND_BUNDLE, "reason": "needs more depth"}

    plan = await classify_followup(
        question="and what about auth integration?",
        prior_bundle=bundle,
        question_embedding=_vec(0.7, 0.7, 0),
        available_repo_names=["orders", "auth"],
        available_repo_id_by_name={"orders": "p", "auth": "a"},
        llm_classifier=_stub,
    )

    assert plan.mode == MODE_EXTEND_BUNDLE
    assert plan.extension_repo_names == ["auth"]
    assert plan.extension_repos == ["a"]


@pytest.mark.asyncio
async def test_ambiguous_no_llm_falls_back_to_extend() -> None:
    """Heuristic when no LLM classifier is provided: extend (cheapest
    safe option, won't hallucinate the answer)."""
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))
    plan = await classify_followup(
        question="and what about it",
        prior_bundle=bundle,
        question_embedding=_vec(0.7, 0.7, 0),
        available_repo_names=["orders"],
    )
    assert plan.mode == MODE_EXTEND_BUNDLE
    assert "heuristic extend" in plan.reason


@pytest.mark.asyncio
async def test_no_embedding_falls_through_to_llm_or_heuristic() -> None:
    """Without an embedding the planner can't bucket high/low — it
    asks the LLM (or heuristic-extends if absent)."""
    bundle = _make_bundle(last_question_embedding=None)
    plan = await classify_followup(
        question="ok and now follow up",
        prior_bundle=bundle,
        question_embedding=None,
        available_repo_names=["orders"],
    )
    assert plan.mode == MODE_EXTEND_BUNDLE


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_heuristic_extend() -> None:
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))

    async def _broken(question, prior):
        raise RuntimeError("haiku unavailable")

    plan = await classify_followup(
        question="middling",
        prior_bundle=bundle,
        question_embedding=_vec(0.7, 0.7, 0),
        available_repo_names=["orders"],
        llm_classifier=_broken,
    )
    assert plan.mode == MODE_EXTEND_BUNDLE


@pytest.mark.asyncio
async def test_llm_garbage_response_falls_back_to_heuristic() -> None:
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))

    async def _garbage(question, prior):
        return {"mode": "no-such-mode", "reason": "lol"}

    plan = await classify_followup(
        question="middling",
        prior_bundle=bundle,
        question_embedding=_vec(0.7, 0.7, 0),
        available_repo_names=["orders"],
        llm_classifier=_garbage,
    )
    # Falls through past the LLM branch (mode not in valid set) → heuristic
    assert plan.mode == MODE_EXTEND_BUNDLE


@pytest.mark.asyncio
async def test_to_dict_drops_recursive_bundle() -> None:
    bundle = _make_bundle(last_question_embedding=_vec(1, 0, 0))
    plan = await classify_followup(
        question="What did you mean?",
        prior_bundle=bundle,
        question_embedding=_vec(1, 0, 0),
        available_repo_names=["orders"],
    )
    raw = plan.to_dict()
    assert "reused_bundle" not in raw
    assert raw["reused_bundle_conversation_id"] == bundle.conversation_id
