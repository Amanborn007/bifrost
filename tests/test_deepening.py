"""Unit tests for reasoning-driven scope deepening (select_repos_to_deepen).

Pure reasoning step — mock the LLM client; no DB, no real `claude -p`.
"""

from __future__ import annotations

import pytest

from bifrost.extraction.cli_client import CliCompletion, Usage
from bifrost.research.deepening import select_repos_to_deepen, suggest_followups


class _StubClient:
    def __init__(self, parsed: dict | None = None, raise_exc: Exception | None = None):
        self._parsed = parsed
        self._raise = raise_exc
        self.calls = 0

    async def call_with_json_schema(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self._raise:
            raise self._raise
        return CliCompletion(parsed=self._parsed or {}, usage=Usage(0, 0, 0, 0, 0.0), text="")


_CANDS = [
    {"name": "stateful-webhook-api", "description": "stateful orchestrator", "why": "neighbour"},
    {"name": "DeliveryGateway", "description": "delivery", "why": "neighbour"},
]
_FINDINGS = [
    {
        "repo_name": "webhook-api",
        "relevance": "high",
        "role_in_answer": "maps timeout to UNKNOWN but the gate decision is upstream",
        "gaps": ["fail-open/closed decided by upstream caller, not code-verified"],
    },
]


@pytest.mark.asyncio
async def test_selects_upstream_repo_when_gap_is_upstream() -> None:
    client = _StubClient(
        {
            "needs_more_repos": True,
            "selected_repos": ["stateful-webhook-api"],
            "reasoning": "the fail-open/closed gate lives in the stateful orchestrator",
        }
    )
    d = await select_repos_to_deepen(
        question="does timeout fail open?",
        wave_findings=_FINDINGS,
        candidates=_CANDS,
        client=client,
    )
    assert d.needs_more_repos is True
    assert d.selected_repos == ["stateful-webhook-api"]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_no_candidates_short_circuits_without_llm() -> None:
    client = _StubClient({"needs_more_repos": True, "selected_repos": ["x"]})
    d = await select_repos_to_deepen(
        question="q",
        wave_findings=_FINDINGS,
        candidates=[],
        client=client,
    )
    assert d.needs_more_repos is False
    assert client.calls == 0  # never called the model


@pytest.mark.asyncio
async def test_hallucinated_repo_name_is_filtered_out() -> None:
    # Model picks a repo NOT in the candidate list → dropped → no widening.
    client = _StubClient(
        {
            "needs_more_repos": True,
            "selected_repos": ["not-a-real-candidate"],
            "reasoning": "...",
        }
    )
    d = await select_repos_to_deepen(
        question="q",
        wave_findings=_FINDINGS,
        candidates=_CANDS,
        client=client,
    )
    assert d.needs_more_repos is False
    assert d.selected_repos == []


@pytest.mark.asyncio
async def test_complete_answer_does_not_widen() -> None:
    client = _StubClient(
        {
            "needs_more_repos": False,
            "selected_repos": [],
            "reasoning": "already code-grounded and complete",
        }
    )
    d = await select_repos_to_deepen(
        question="q",
        wave_findings=_FINDINGS,
        candidates=_CANDS,
        client=client,
    )
    assert d.needs_more_repos is False


@pytest.mark.asyncio
async def test_llm_failure_is_graceful() -> None:
    from bifrost.extraction.cli_client import ClaudeCliTransientError

    client = _StubClient(raise_exc=ClaudeCliTransientError("boom"))
    d = await select_repos_to_deepen(
        question="q",
        wave_findings=_FINDINGS,
        candidates=_CANDS,
        client=client,
    )
    assert d.needs_more_repos is False  # falls through to synthesis, no crash


# ── Follow-up suggestion generation ──────────────────────────────


@pytest.mark.asyncio
async def test_suggest_followups_returns_actionable_prompts() -> None:
    client = _StubClient(
        {
            "suggestions": [
                {
                    "prompt": "Extract and walk signing-keys to confirm "
                    "the VALID-gate allows txn creation.",
                    "rationale": "code-verify the caller's gate",
                    "repos_to_index": ["signing-keys"],
                },
            ]
        }
    )
    out = await suggest_followups(
        question="does timeout fail open?",
        answer_excerpt="webhook-api ...",
        open_gaps=["caller gate not code-verified"],
        unwalked_repos=["signing-keys"],
        stale_repos=[],
        failed_extractions=[],
        client=client,
    )
    assert len(out) == 1
    assert "signing-keys" in out[0].repos_to_index
    assert out[0].prompt.startswith("Extract and walk")


@pytest.mark.asyncio
async def test_suggest_followups_short_circuits_with_no_signals() -> None:
    client = _StubClient({"suggestions": [{"prompt": "x"}]})
    out = await suggest_followups(
        question="q",
        answer_excerpt="a",
        open_gaps=[],
        unwalked_repos=[],
        stale_repos=[],
        failed_extractions=[],
        client=client,
    )
    assert out == [] and client.calls == 0  # nothing to suggest → no LLM call


@pytest.mark.asyncio
async def test_suggest_followups_graceful_on_error() -> None:
    from bifrost.extraction.cli_client import ClaudeCliTransientError

    client = _StubClient(raise_exc=ClaudeCliTransientError("boom"))
    out = await suggest_followups(
        question="q",
        answer_excerpt="a",
        open_gaps=["g"],
        unwalked_repos=[],
        stale_repos=[],
        failed_extractions=[],
        client=client,
    )
    assert out == []
