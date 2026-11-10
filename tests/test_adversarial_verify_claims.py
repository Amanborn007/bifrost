"""``_adversarial_verify_claims`` builds its evidence from real RepoReport
fields and reports verifier failures instead of returning "no corrections"."""

from __future__ import annotations

import inspect

import pytest

from bifrost.cli.config import BifrostSettings
from bifrost.extraction import cli_client
from bifrost.extraction.cli_client import CliCompletion, Usage
from bifrost.research import coordinator
from bifrost.research.coordinator import (
    RepoBundle,
    _adversarial_verify_claims,
)
from bifrost.research.repo_agent import RepoReport

ROLE = "Owns the order router and retry policy"
EVIDENCE = "router.py:12 defines route_order"


def _bundle() -> RepoBundle:
    code = RepoReport(
        repo_id="r1",
        repo_name="svc",
        clone_path="/tmp/svc",
        relevance="high",
        role_in_answer=ROLE,
        evidence=EVIDENCE,
        key_files=["router.py"],
    )
    return RepoBundle(repo_id="r1", repo_name="svc", code=code)


def _settings() -> BifrostSettings:
    return BifrostSettings(llm_cache_enabled=False)


async def test_evidence_line_uses_role_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []

    async def fake_call(self, **kwargs):  # noqa: ANN001
        captured.append(kwargs)
        corr = {
            "claim": "router retries 5 times",
            "probe": "grep retry",
            "reality": "retries 3 times",
            "severity": "high",
            "action": "fix the number",
        }
        return CliCompletion(parsed={"corrections": [corr]}, usage=Usage(0, 0, 0, 0, 0.0), text="")

    monkeypatch.setattr(cli_client.ClaudeLlmClient, "call_with_json_schema", fake_call)

    note, count, error = await _adversarial_verify_claims("answer", [_bundle()], _settings())

    assert error is None
    assert count == 1
    assert "router retries 5 times" in note
    user_msg = captured[0]["user_message"]
    assert ROLE in user_msg
    assert EVIDENCE in user_msg
    assert "router.py" in user_msg


async def test_llm_failure_is_returned_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(self, **kwargs):  # noqa: ANN001
        raise RuntimeError("cli exploded")

    monkeypatch.setattr(cli_client.ClaudeLlmClient, "call_with_json_schema", boom)

    note, count, error = await _adversarial_verify_claims("answer", [_bundle()], _settings())

    assert (note, count) == ("", 0)
    assert error is not None
    assert "cli exploded" in error


def test_failure_becomes_a_visible_status_event() -> None:
    events = coordinator._adversarial_events("", 0, "cli exploded")
    assert len(events) == 1
    assert events[0]["type"] == "status"
    assert "cli exploded" in events[0]["message"]


def test_corrections_event_unchanged_on_success() -> None:
    events = coordinator._adversarial_events("note", 2, None)
    assert [e["type"] for e in events] == ["adversarial_corrections"]
    assert events[0]["count"] == 2
    assert coordinator._adversarial_events("", 0, None) == []


def test_run_research_wires_error_into_done_payload() -> None:
    src = inspect.getsource(coordinator.run_research)
    assert "_adversarial_events(" in src
    assert '"adversarial_error": adversarial_error' in src
