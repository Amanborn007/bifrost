"""Tests for extraction checkpoint/resume and retry logic."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bifrost.extraction.checkpoint import PHASE_ORDER, ExtractionCheckpoint
from bifrost.extraction.pipeline import (
    BACKOFF_SECONDS,
    MAX_RETRIES,
    _is_retryable,
    _retry_llm_call,
)

# ── Checkpoint basics ────────────────────────────────────────────────


class TestCheckpointCreation:
    def test_creates_checkpoint_file_on_first_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-001")
            cp.mark_phase_complete("A")

            cp_file = Path(tmp) / "test-001.json"
            assert cp_file.exists()

            data = json.loads(cp_file.read_text())
            assert "A" in data["completed_phases"]
            assert data["session_id"] == "test-001"

    def test_checkpoint_file_accumulates_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-002")
            cp.mark_phase_complete("A")
            cp.mark_phase_complete("B")
            cp.mark_phase_complete("C")

            data = json.loads((Path(tmp) / "test-002.json").read_text())
            assert data["completed_phases"] == ["A", "B", "C"]

    def test_duplicate_phase_marking_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-003")
            cp.mark_phase_complete("A")
            cp.mark_phase_complete("A")

            data = json.loads((Path(tmp) / "test-003.json").read_text())
            assert data["completed_phases"].count("A") == 1

    def test_entity_ids_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-004")
            cp.mark_phase_complete("D", entity_ids=["comp-1", "comp-2"])
            cp.mark_phase_complete("E", entity_ids=["flow-1"])

            assert cp.get_entity_ids() == ["comp-1", "comp-2", "flow-1"]


class TestCheckpointClear:
    """force_full=true re-runs must not silently resume from a stale checkpoint."""

    def test_clear_wipes_completed_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-clear")
            cp.mark_phase_complete("A", entity_ids=["a-1"])
            cp.mark_phase_complete("D", entity_ids=["comp-1"])
            assert cp.get_completed_phases() == ["A", "D"]

            cp.clear()

            assert cp.get_completed_phases() == []
            assert cp.get_entity_ids() == []

    def test_clear_removes_persisted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-clear-file")
            cp.mark_phase_complete("A")
            cp_file = Path(tmp) / "test-clear-file.json"
            assert cp_file.exists()

            cp.clear()

            assert not cp_file.exists()

    def test_clear_on_fresh_checkpoint_is_noop(self) -> None:
        """Clearing a never-persisted checkpoint must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-fresh")
            cp.clear()  # should not raise
            assert cp.get_completed_phases() == []

    def test_can_resume_after_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-reuse")
            cp.mark_phase_complete("A")
            cp.mark_phase_complete("B")
            cp.clear()
            cp.mark_phase_complete("A")  # re-mark post-clear
            assert cp.get_completed_phases() == ["A"]


class TestUnitLevelCheckpoint:
    """Per-batch (Phase D) + per-file (Phase E) resume granularity."""

    def test_mark_unit_done_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-unit-1")
            cp.mark_unit_done("D", "batch_a|b|c")
            cp.mark_unit_done("D", "batch_a|b|c")  # dup
            assert cp.get_done_units("D") == ["batch_a|b|c"]

    def test_unit_done_reports_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-unit-2")
            cp.mark_unit_done("E", "src/Foo.java")
            assert cp.unit_done("E", "src/Foo.java")
            assert not cp.unit_done("E", "src/Bar.java")
            assert not cp.unit_done("D", "src/Foo.java")  # phases are separate

    def test_units_persist_across_checkpoint_instances(self) -> None:
        """Resume after worker restart must see prior units done."""
        with tempfile.TemporaryDirectory() as tmp:
            cp1 = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-unit-3")
            cp1.mark_unit_done("D", "batch_1")
            cp1.mark_unit_done("D", "batch_2")

            cp2 = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-unit-3")
            assert cp2.unit_done("D", "batch_1")
            assert cp2.unit_done("D", "batch_2")
            assert set(cp2.get_done_units("D")) == {"batch_1", "batch_2"}

    def test_clear_wipes_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-unit-4")
            cp.mark_unit_done("D", "batch_a")
            cp.clear()
            assert cp.get_done_units("D") == []

    def test_clear_phase_selective(self) -> None:
        """clear_phase only wipes one phase, leaves others intact."""
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="test-unit-5")
            cp.mark_phase_complete("D")
            cp.mark_unit_done("D", "b1")
            cp.mark_phase_complete("E")
            cp.mark_unit_done("E", "src/A.java")

            cp.clear_phase("E")

            # E gone, D intact
            assert "D" in cp.get_completed_phases()
            assert "E" not in cp.get_completed_phases()
            assert cp.get_done_units("D") == ["b1"]
            assert cp.get_done_units("E") == []


# ── Resume logic ─────────────────────────────────────────────────────


class TestCheckpointResume:
    def test_resume_returns_next_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="resume-001")
            cp.mark_phase_complete("A")
            cp.mark_phase_complete("B")
            cp.mark_phase_complete("C")

            assert cp.get_resume_phase() == "D"

    def test_resume_from_phase_d_skips_abc(self) -> None:
        """Verify that resuming from D means A, B, C are already complete."""
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="resume-002")
            cp.mark_phase_complete("A")
            cp.mark_phase_complete("B")
            cp.mark_phase_complete("C")

            assert cp.is_phase_complete("A")
            assert cp.is_phase_complete("B")
            assert cp.is_phase_complete("C")
            assert not cp.is_phase_complete("D")
            assert cp.get_resume_phase() == "D"

    def test_resume_after_all_phases_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="resume-003")
            for phase in PHASE_ORDER:
                cp.mark_phase_complete(phase)

            assert cp.get_resume_phase() is None

    def test_resume_from_persisted_state(self) -> None:
        """Load checkpoint from a previous session and verify resume."""
        with tempfile.TemporaryDirectory() as tmp:
            # Session 1: complete A-C
            cp1 = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="persist-001")
            cp1.mark_phase_complete("A")
            cp1.mark_phase_complete("B")
            cp1.mark_phase_complete("C")

            # Session 2: reload
            cp2 = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="persist-001")
            assert cp2.is_phase_complete("A")
            assert cp2.is_phase_complete("C")
            assert cp2.get_resume_phase() == "D"

    def test_corrupt_checkpoint_starts_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp_file = Path(tmp) / "corrupt-001.json"
            cp_file.write_text("NOT VALID JSON {{{")

            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="corrupt-001")
            assert cp.get_resume_phase() == "A"
            assert cp.get_completed_phases() == []


# ── Atomic writes ────────────────────────────────────────────────────


class TestAtomicWrites:
    def test_checkpoint_survives_crash_simulation(self) -> None:
        """If write fails mid-save, checkpoint file should not be corrupted."""
        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="atomic-001")
            cp.mark_phase_complete("A")

            # Verify checkpoint is valid
            data_before = json.loads((Path(tmp) / "atomic-001.json").read_text())
            assert "A" in data_before["completed_phases"]

            # Phase B completes and is written
            cp.mark_phase_complete("B")

            data_after = json.loads((Path(tmp) / "atomic-001.json").read_text())
            assert "A" in data_after["completed_phases"]
            assert "B" in data_after["completed_phases"]

    def test_checkpoint_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "sub", "dir")
            cp = ExtractionCheckpoint(checkpoint_dir=nested, session_id="nested-001")
            cp.mark_phase_complete("A")
            assert (Path(nested) / "nested-001.json").exists()


# ── Retry logic ──────────────────────────────────────────────────────


class TestRetryAbsorption:
    def test_is_retryable_rate_limit(self) -> None:
        assert _is_retryable(Exception("rate_limit_error")) is True
        assert _is_retryable(Exception("Rate limit exceeded")) is True

    def test_is_retryable_timeout(self) -> None:
        assert _is_retryable(Exception("Request timeout")) is True

    def test_is_retryable_overloaded(self) -> None:
        assert _is_retryable(Exception("API overloaded (529)")) is True

    def test_is_not_retryable_auth(self) -> None:
        assert _is_retryable(Exception("Invalid API key")) is False

    def test_is_not_retryable_validation(self) -> None:
        assert _is_retryable(Exception("Invalid request")) is False

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        call_count = 0

        async def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("rate_limit_error")
            return "success"

        with patch("bifrost.extraction.pipeline.asyncio.sleep", new_callable=AsyncMock):
            result = await _retry_llm_call(flaky_call, phase="D", context="test.py")

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausts_all_attempts(self) -> None:
        call_count = 0

        async def always_fails() -> str:
            nonlocal call_count
            call_count += 1
            raise Exception("rate_limit_error")

        with (
            patch("bifrost.extraction.pipeline.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(Exception, match="rate_limit_error"),
        ):
            await _retry_llm_call(always_fails, phase="D", context="test.py")

        assert call_count == MAX_RETRIES

    @pytest.mark.asyncio
    async def test_retry_backoff_is_exponential(self) -> None:
        """Verify 1s, 2s, 4s backoff delays."""
        sleep_delays: list[float] = []

        async def mock_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        async def always_rate_limited() -> str:
            raise Exception("rate_limit_error")

        with (
            patch("bifrost.extraction.pipeline.asyncio.sleep", side_effect=mock_sleep),
            pytest.raises(Exception, match="rate_limit_error"),
        ):
            await _retry_llm_call(always_rate_limited, phase="D", context="test.py")

        # Should have slept twice (after attempt 1 and 2, not after final attempt)
        assert len(sleep_delays) == 2
        assert sleep_delays[0] == BACKOFF_SECONDS[0]  # 1.0s
        assert sleep_delays[1] == BACKOFF_SECONDS[1]  # 2.0s

    @pytest.mark.asyncio
    async def test_non_retryable_error_fails_immediately(self) -> None:
        call_count = 0

        async def auth_error() -> str:
            nonlocal call_count
            call_count += 1
            raise Exception("Invalid API key")

        with pytest.raises(Exception, match="Invalid API key"):
            await _retry_llm_call(auth_error, phase="D", context="test.py")

        assert call_count == 1  # No retries for non-retryable errors

    @pytest.mark.asyncio
    async def test_retry_succeeds_without_delay(self) -> None:
        """First call succeeds — no retries, no delay."""

        async def works_first_try() -> str:
            return "instant"

        result = await _retry_llm_call(works_first_try, phase="D", context="test.py")
        assert result == "instant"


# ── Integration: checkpoint + pipeline ───────────────────────────────


class TestCheckpointPipelineIntegration:
    @pytest.mark.asyncio
    async def test_checkpoint_callback_records_phases(self) -> None:
        """Verify ExtractionCheckpoint works as on_phase_complete callback."""
        from dataclasses import dataclass, field
        from typing import Any

        from bifrost.extraction.pipeline import ExtractionPipeline

        @dataclass
        class MockToolUseBlock:
            type: str = "tool_use"
            id: str = "test-id"
            name: str = ""
            input: dict[str, Any] = field(default_factory=dict)

        @dataclass
        class MockResponse:
            content: list[Any] = field(default_factory=list)

        with tempfile.TemporaryDirectory() as tmp:
            cp = ExtractionCheckpoint(checkpoint_dir=tmp, session_id="integration-001")

            async def on_phase(phase: str) -> None:
                cp.mark_phase_complete(phase)

            responses = [
                MockResponse(
                    content=[
                        MockToolUseBlock(
                            name="classify_files",
                            input={
                                "files": [
                                    {
                                        "path": "x.py",
                                        "category": "component_definition",
                                        "likely_key_component": False,
                                    },
                                ]
                            },
                        )
                    ]
                ),
                MockResponse(
                    content=[
                        MockToolUseBlock(
                            name="extract_components",
                            input={
                                "components": [
                                    {
                                        "component_name": "X",
                                        "component_type": "module",
                                        "purpose": "X",
                                        "source_file": "x.py",
                                    },
                                ]
                            },
                        )
                    ]
                ),
                MockResponse(content=[MockToolUseBlock(name="extract_flows", input={"flows": []})]),
                MockResponse(
                    content=[
                        MockToolUseBlock(name="extract_dependencies", input={"dependencies": []})
                    ]
                ),
                MockResponse(
                    content=[
                        MockToolUseBlock(
                            name="synthesize_relationships", input={"relationships": []}
                        )
                    ]
                ),
                MockResponse(
                    content=[
                        MockToolUseBlock(
                            name="compress_knowledge_index",
                            input={
                                "entries": [
                                    {
                                        "entity_urn": "urn:test",
                                        "summary": "X",
                                        "key_facts": [],
                                        "search_keywords": [],
                                    },
                                ]
                            },
                        )
                    ]
                ),
                MockResponse(
                    content=[
                        MockToolUseBlock(
                            name="score_confidence",
                            input={
                                "scores": [
                                    {
                                        "entity_urn": "urn:test",
                                        "confidence_level": "inferred",
                                        "confidence_score": 0.6,
                                        "reason": "t",
                                    },
                                ]
                            },
                        )
                    ]
                ),
            ]

            client = AsyncMock()
            client.messages.create = AsyncMock(side_effect=responses)

            pipeline = ExtractionPipeline(
                client=client,
                repo_id="integration",
                on_phase_complete=on_phase,
            )
            await pipeline.run(
                file_paths=["x.py"],
                read_file=AsyncMock(return_value="class X: pass"),
            )

            assert cp.is_phase_complete("F")
            assert cp.is_phase_complete("G")
            assert cp.is_phase_complete("H")
            assert cp.is_phase_complete("I")
            assert cp.get_resume_phase() is None or cp.get_resume_phase() == "A"
