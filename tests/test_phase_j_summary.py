"""Phase J (repo summary) reads the real dataclass fields and fails loudly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bifrost.extraction.checkpoint import ExtractionCheckpoint
from bifrost.extraction.cli_client import CliCompletion, Usage
from bifrost.extraction.cost import CostTracker
from bifrost.extraction.pipeline import (
    ExtractedComponent,
    ExtractedDependency,
    ExtractedFlow,
    ExtractionOutput,
    ExtractionPipeline,
)
from bifrost.extraction.triage import TriagedFile, TriageResult


class _RecordingClient:
    def __init__(self, summary: str = "A orders service.") -> None:
        self.calls: list[dict[str, Any]] = []
        self._summary = summary

    async def call_with_json_schema(self, **kwargs: Any) -> CliCompletion:
        self.calls.append(kwargs)
        return CliCompletion(
            parsed={"summary": self._summary},
            usage=Usage(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                total_cost_usd=0.01,
            ),
            text="",
        )


class _RaisingClient:
    async def call_with_json_schema(self, **kwargs: Any) -> CliCompletion:
        raise RuntimeError("summary backend down")


def _output() -> ExtractionOutput:
    return ExtractionOutput(
        components=[
            ExtractedComponent(
                component_name="InventoryWriter",
                component_type="service",
                purpose="writes inventory rows",
                source_file="inventory.py",
            )
        ],
        flows=[ExtractedFlow(flow_name="FulfillmentFlow", flow_type="batch")],
        dependencies=[
            ExtractedDependency(
                source_name="InventoryWriter",
                target_name="PostgresDb",
                dep_type="data",
                discovery_method="code",
            )
        ],
    )


async def test_summary_prompt_uses_real_field_names_and_records_cost() -> None:
    client = _RecordingClient()
    tracker = CostTracker(task_id="t")
    pipeline = ExtractionPipeline(
        client,  # type: ignore[arg-type]
        extraction_model="my-extract-model",
        cost_tracker=tracker,
    )

    summary = await pipeline._generate_repo_summary(_output())

    assert summary == "A orders service."
    (call,) = client.calls
    msg = call["user_message"]
    assert "InventoryWriter" in msg
    assert "FulfillmentFlow" in msg
    assert "InventoryWriter→PostgresDb" in msg
    assert call["model"] == "my-extract-model"
    assert [r.phase for r in tracker.records] == ["J"]


async def test_phase_j_failure_is_recorded_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = ExtractionPipeline(_RaisingClient())  # type: ignore[arg-type]

    async def fake_triage(*args: Any, **kwargs: Any) -> TriageResult:
        tr = TriageResult(
            files=[
                TriagedFile(
                    path="inventory.py",
                    category="component_definition",
                    likely_key_component=True,
                )
            ]
        )
        return tr

    async def fake_components(self: Any, path: str, content: str) -> list[ExtractedComponent]:
        return [
            ExtractedComponent(
                component_name="InventoryWriter",
                component_type="service",
                purpose="p",
                source_file=path,
            )
        ]

    async def read_file(path: str) -> str:
        return "x = 1"

    monkeypatch.setattr("bifrost.extraction.pipeline.triage_files", fake_triage)
    monkeypatch.setattr(ExtractionPipeline, "_extract_components", fake_components)

    cp = ExtractionCheckpoint(checkpoint_dir=str(tmp_path), session_id="j")
    for phase in ("E", "F", "G", "H", "I"):
        cp.mark_phase_complete(phase)

    output = await pipeline.run(["inventory.py"], read_file, resume_checkpoint=cp)

    assert output.repo_summary is None
    assert any("Phase J" in e for e in output.errors), output.errors
    assert output.partial is True
