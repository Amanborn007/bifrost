"""CLARIFY shortcut — run_clarify_synthesis() generator + safety guard.

The generator streams Haiku output and yields SSE-shaped events that
mirror the main research pipeline so the SSE consumer can swap one
for the other. These tests stub the LLM client to exercise the event
sequence + safety guard without spinning up a claude subprocess.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from bifrost.extraction.cli_client import ClaudeCliError, Usage
from bifrost.research.bundle_cache import BundleSource, ResearchBundle
from bifrost.research.clarify import (
    CLARIFY_MIN_PRIOR_ANSWER_CHARS,
    CLARIFY_SIMILARITY_FLOOR,
    _build_user_message,
    _bundle_sources_for_done_event,
    _is_eligible,
    run_clarify_synthesis,
)


@dataclass
class _FakeSettings:
    """Stand-in for BifrostSettings — only reasoning_model is read.

    (clarify uses reasoning_model along with the other
    final-answer-shaping steps.)
    """

    triage_model: str = "claude-opus-4-8"
    reasoning_model: str = "claude-opus-4-8"


def _make_bundle(
    *,
    answer: str = (
        "It uses an exponential-backoff queue with jitter, scoped to a "
        "RetryQueue component that wraps the carrier-gateway client."
    ),
    sources: list[BundleSource] | None = None,
    repos: list[str] | None = None,
    confidence: str = "confirmed",
) -> ResearchBundle:
    return ResearchBundle(
        conversation_id="conv-1",
        last_question="how does orders retry work?",
        last_answer=answer,
        confidence=confidence,
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=repos or ["orders"],
        scope_rationale="single repo, deep-indexed",
        subgraph_entity_urns=["bifrost://repo/orders/component/RetryQueue"],
        sources=sources or [BundleSource(kind="jira", ref="SHIP-1", title="retry bug")],
        last_question_embedding=[0.1, 0.2, 0.3],
    )


class _ScriptedClient:
    """Fake ClaudeLlmClient that yields a pre-programmed event stream."""

    def __init__(
        self, deltas: list[str], *, usage: Usage | None = None, raise_at: int | None = None
    ):
        self._deltas = deltas
        self._usage = usage or Usage(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            total_cost_usd=0.0001,
        )
        self._raise_at = raise_at
        self.calls: list[dict[str, Any]] = []

    async def stream_text(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_turns: int = 1,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "model": model,
            }
        )
        for i, d in enumerate(self._deltas):
            if self._raise_at == i:
                raise ClaudeCliError("simulated transient failure")
            yield {"type": "delta", "text": d}
        yield {"type": "done", "usage": self._usage, "final_text": "".join(self._deltas)}


# ── _is_eligible ────────────────────────────────────────────────────


def test_is_eligible_requires_bundle_and_high_similarity() -> None:
    bundle = _make_bundle()
    assert _is_eligible(bundle, 0.95) is True
    assert _is_eligible(bundle, CLARIFY_SIMILARITY_FLOOR) is True


def test_is_eligible_rejects_low_similarity() -> None:
    assert _is_eligible(_make_bundle(), 0.85) is False
    assert _is_eligible(_make_bundle(), None) is False


def test_is_eligible_rejects_when_no_bundle() -> None:
    assert _is_eligible(None, 0.99) is False


def test_is_eligible_rejects_trivial_prior_answer() -> None:
    short = _make_bundle(answer="ok.")
    assert len(short.last_answer) < CLARIFY_MIN_PRIOR_ANSWER_CHARS
    assert _is_eligible(short, 0.99) is False


# ── _build_user_message + _bundle_sources_for_done_event ────────────


def test_build_user_message_wraps_in_xml_tags_and_includes_inputs() -> None:
    bundle = _make_bundle()
    msg = _build_user_message(
        new_question="What does retry queue mean exactly?",
        prior_bundle=bundle,
        prior_turns_text="User: x\nAssistant: y",
    )
    assert "<prior_question>" in msg
    assert "<prior_answer>" in msg
    assert "<new_question>" in msg
    assert "<recent_chat_history>" in msg
    assert "What does retry queue mean exactly?" in msg
    assert "exponential-backoff queue" in msg
    assert "SHIP-1" in msg  # source ref appears
    assert "orders" in msg  # repo name appears


def test_build_user_message_omits_chat_history_when_empty() -> None:
    msg = _build_user_message(
        new_question="q",
        prior_bundle=_make_bundle(),
        prior_turns_text="",
    )
    assert "<recent_chat_history>" not in msg


def test_bundle_sources_for_done_event_shape() -> None:
    bundle = _make_bundle(
        sources=[
            BundleSource(kind="jira", ref="SHIP-1", title="retry bug"),
            BundleSource(kind="confluence", ref="page-123", title="Retry RFC"),
        ]
    )
    out = _bundle_sources_for_done_event(bundle)
    assert out[0]["urn"] == "SHIP-1"
    assert out[0]["entity_type"] == "jira"
    assert out[0]["name"] == "retry bug"
    assert out[1]["entity_type"] == "confluence"


# ── run_clarify_synthesis happy path + failures ─────────────────────


@pytest.mark.asyncio
async def test_happy_path_yields_status_deltas_done() -> None:
    bundle = _make_bundle()
    client = _ScriptedClient(
        deltas=[
            "It's the queue ",
            "that retries failed ",
            "delivery attempts.",
        ]
    )
    events = []
    async for evt in run_clarify_synthesis(
        question="What does retry queue mean exactly?",
        prior_bundle=bundle,
        prior_turns_text="",
        settings=_FakeSettings(),
        client=client,
    ):
        events.append(evt)

    types = [e["type"] for e in events]
    assert types[0] == "status"
    assert types.count("synthesis_delta") == 3
    assert types[-1] == "done"

    done = events[-1]
    assert done["answer"] == "It's the queue that retries failed delivery attempts."
    assert done["confidence"] == "confirmed"
    assert done["shortcut"] == "clarify_from_bundle"
    assert done["sources"][0]["urn"] == "SHIP-1"
    assert done["usage"]["calls"] == 1
    assert done["reports"] == []


@pytest.mark.asyncio
async def test_uses_reasoning_model_from_settings() -> None:
    """clarify reads ``settings.reasoning_model`` (premium tier);
    it produces the final user-visible answer when no
    research is needed, so it deserves the strongest model."""
    client = _ScriptedClient(deltas=["x"])
    async for _ in run_clarify_synthesis(
        question="q",
        prior_bundle=_make_bundle(),
        prior_turns_text="",
        settings=_FakeSettings(reasoning_model="my-opus"),
        client=client,
    ):
        pass
    assert client.calls[0]["model"] == "my-opus"


@pytest.mark.asyncio
async def test_haiku_failure_yields_error_event() -> None:
    client = _ScriptedClient(deltas=["partial"], raise_at=0)
    events = []
    async for evt in run_clarify_synthesis(
        question="q",
        prior_bundle=_make_bundle(),
        prior_turns_text="",
        settings=_FakeSettings(),
        client=client,
    ):
        events.append(evt)
    assert any(e["type"] == "error" and "Clarify failed" in e["error"] for e in events)
    # No spurious done event when Haiku errored.
    assert all(e["type"] != "done" for e in events)


@pytest.mark.asyncio
async def test_empty_haiku_output_yields_error_for_fallback() -> None:
    client = _ScriptedClient(deltas=[])  # no deltas at all
    events = []
    async for evt in run_clarify_synthesis(
        question="q",
        prior_bundle=_make_bundle(),
        prior_turns_text="",
        settings=_FakeSettings(),
        client=client,
    ):
        events.append(evt)
    types = [e["type"] for e in events]
    assert types[-1] == "error"
    assert "no output" in events[-1]["error"].lower()


@pytest.mark.asyncio
async def test_status_message_mentions_repo_count() -> None:
    bundle = _make_bundle(repos=["orders", "auth", "batch-orders"])
    client = _ScriptedClient(deltas=["ok"])
    events = []
    async for evt in run_clarify_synthesis(
        question="q",
        prior_bundle=bundle,
        prior_turns_text="",
        settings=_FakeSettings(),
        client=client,
    ):
        events.append(evt)
    status = events[0]
    assert status["type"] == "status"
    assert "3 prior repo" in status["message"]


@pytest.mark.asyncio
async def test_falls_back_to_final_text_when_no_deltas_streamed() -> None:
    """If the API delivers final_text instead of streaming deltas, we
    should still surface it in the done event."""

    class _NoDeltaClient:
        async def stream_text(self, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "done",
                "usage": Usage(
                    input_tokens=1,
                    output_tokens=2,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    total_cost_usd=0.00001,
                ),
                "final_text": "all-at-once response",
            }

    events = []
    async for evt in run_clarify_synthesis(
        question="q",
        prior_bundle=_make_bundle(),
        prior_turns_text="",
        settings=_FakeSettings(),
        client=_NoDeltaClient(),  # type: ignore[arg-type]
    ):
        events.append(evt)
    done = next(e for e in events if e["type"] == "done")
    assert done["answer"] == "all-at-once response"
