"""``llm_classify_followup`` reads ``CliCompletion.parsed`` and a classifier
failure is recorded in the plan reason instead of vanishing."""

from __future__ import annotations

import pytest

from bifrost.cli import config as cfg
from bifrost.cli.config import BifrostSettings
from bifrost.extraction import cli_client
from bifrost.extraction.cli_client import CliCompletion, Usage
from bifrost.research.bundle_cache import ResearchBundle
from bifrost.research.planner import (
    MODE_EXTEND_BUNDLE,
    MODE_NEW_SCOPE,
    classify_followup,
    llm_classify_followup,
)


def _bundle() -> ResearchBundle:
    return ResearchBundle(
        conversation_id="c",
        last_question="q",
        last_answer="a",
        confidence="low",
        repos_used=[],
        repos_used_names=[],
        scope_rationale="",
        subgraph_entity_urns=[],
        sources=[],
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "get_settings", lambda: BifrostSettings(llm_cache_enabled=False))


async def test_llm_classifier_returns_parsed_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call(self, **kwargs):  # noqa: ANN001
        parsed = {"mode": "new_scope", "reason": "r"}
        return CliCompletion(parsed=parsed, usage=Usage(0, 0, 0, 0, 0.0), text="")

    monkeypatch.setattr(cli_client.ClaudeLlmClient, "call_with_json_schema", fake_call)

    verdict = await llm_classify_followup("new q", _bundle())
    assert verdict == {"mode": "new_scope", "reason": "r"}

    plan = await classify_followup(
        question="new q", prior_bundle=_bundle(), llm_classifier=llm_classify_followup
    )
    assert plan.mode == MODE_NEW_SCOPE
    assert plan.reason.startswith("llm:")


async def test_classifier_failure_is_recorded_in_plan_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(question, prior_bundle):  # noqa: ANN001
        raise RuntimeError("classifier down")

    with caplog.at_level("ERROR"):
        plan = await classify_followup(
            question="new q", prior_bundle=_bundle(), llm_classifier=broken
        )

    assert plan.mode == MODE_EXTEND_BUNDLE
    assert "classifier down" in plan.reason
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)
