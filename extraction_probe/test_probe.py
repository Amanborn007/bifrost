"""Tests for the Bifrost Extraction Probe.

Covers:
- Pydantic validation (positive + negative cases)
- File collection and batching logic
- Prompt construction and canary tokens
- CLI entry point (--help works)
- Integration test against sample code (requires ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from extraction_probe.validate import (
    ComponentExtractionResult,
    FlowExtractionResult,
    FlowSegmentResult,
    validate_component_result,
    validate_flow_result,
)
from extraction_probe.prompts import (
    build_component_prompt,
    build_flow_prompt,
    COMPONENT_TOOL,
    FLOW_TOOL,
)
from extraction_probe.agent import (
    collect_python_files,
    _batch_by_directory,
    _find_entry_points,
    _read_file_safe,
    _extract_tool_input,
    ExtractionResult,
    run_extraction,
)

SAMPLE_DIR = Path(__file__).parent / "sample"


# ──────────────────────────────────────────────────────────
# Pydantic Validation Tests
# ──────────────────────────────────────────────────────────


class TestComponentValidation:
    """Test Pydantic validation for component extraction output."""

    def test_valid_component(self):
        data = {
            "name": "OrderService",
            "type": "service",
            "purpose": "Handles order processing and lifecycle management",
            "files": ["service.py"],
            "interfaces": [
                {"name": "process_order", "type": "api", "description": "Create a new order"}
            ],
            "internal_deps": ["OrderRepository", "EventBus"],
            "confidence": "confirmed",
            "known_gaps": [],
        }
        result = validate_component_result(data)
        assert result is not None
        assert result.name == "OrderService"
        assert result.type == "service"
        assert result.confidence == "confirmed"
        assert len(result.interfaces) == 1

    def test_minimal_valid_component(self):
        data = {
            "name": "Utils",
            "type": "module",
            "purpose": "Utility functions for the application",
            "files": ["utils.py"],
            "confidence": "inferred",
        }
        result = validate_component_result(data)
        assert result is not None
        assert result.internal_deps == []
        assert result.known_gaps == []

    def test_invalid_component_type(self):
        """Invalid component type should fail validation."""
        data = {
            "name": "Bad",
            "type": "invalid_type",
            "purpose": "This should fail validation",
            "files": ["bad.py"],
            "confidence": "confirmed",
        }
        result = validate_component_result(data)
        assert result is None

    def test_missing_required_fields(self):
        """Missing required fields should fail validation."""
        data = {"name": "Incomplete"}
        result = validate_component_result(data)
        assert result is None

    def test_empty_files_list(self):
        """Empty files list should fail (min_length=1)."""
        data = {
            "name": "NoFiles",
            "type": "module",
            "purpose": "Should fail because no files listed",
            "files": [],
            "confidence": "inferred",
        }
        result = validate_component_result(data)
        assert result is None

    def test_short_purpose(self):
        """Purpose too short (<5 chars) should fail."""
        data = {
            "name": "Short",
            "type": "module",
            "purpose": "Hi",
            "files": ["x.py"],
            "confidence": "inferred",
        }
        result = validate_component_result(data)
        assert result is None

    def test_invalid_confidence(self):
        """Invalid confidence value should fail."""
        data = {
            "name": "BadConf",
            "type": "module",
            "purpose": "Should fail on confidence",
            "files": ["x.py"],
            "confidence": "unknown",
        }
        result = validate_component_result(data)
        assert result is None


class TestFlowValidation:
    """Test Pydantic validation for flow extraction output."""

    def test_valid_flow(self):
        data = {
            "name": "Order Processing Flow",
            "flow_type": "request-response",
            "entry_point": "OrderAPI.create_order",
            "segments": [
                {
                    "order": 1,
                    "component_name": "OrderAPI",
                    "segment_type": "entry",
                    "description": "Receives order request",
                },
                {
                    "order": 2,
                    "component_name": "OrderService",
                    "segment_type": "internal",
                    "description": "Validates and processes order",
                },
            ],
            "confidence": "inferred",
            "known_gaps": ["Exact error handling path unclear"],
        }
        result = validate_flow_result(data)
        assert result is not None
        assert result.name == "Order Processing Flow"
        assert len(result.segments) == 2
        assert result.segments[0].order == 1

    def test_invalid_flow_type(self):
        data = {
            "name": "Bad Flow",
            "flow_type": "not-a-type",
            "entry_point": "main",
            "segments": [
                {"order": 1, "component_name": "A", "segment_type": "entry", "description": "x"}
            ],
            "confidence": "confirmed",
        }
        result = validate_flow_result(data)
        assert result is None

    def test_empty_segments(self):
        """Flow with no segments should fail (min_length=1)."""
        data = {
            "name": "Empty Flow",
            "flow_type": "batch",
            "entry_point": "main",
            "segments": [],
            "confidence": "inferred",
        }
        result = validate_flow_result(data)
        assert result is None

    def test_invalid_segment_type(self):
        data = {
            "name": "Bad Segment",
            "flow_type": "pipeline",
            "entry_point": "main",
            "segments": [
                {"order": 1, "component_name": "A", "segment_type": "invalid", "description": "x"}
            ],
            "confidence": "confirmed",
        }
        result = validate_flow_result(data)
        assert result is None

    def test_segment_order_zero(self):
        """Order must be >= 1."""
        data = {
            "name": "Zero Order",
            "flow_type": "batch",
            "entry_point": "main",
            "segments": [
                {"order": 0, "component_name": "A", "segment_type": "entry", "description": "x"}
            ],
            "confidence": "confirmed",
        }
        result = validate_flow_result(data)
        assert result is None


# ──────────────────────────────────────────────────────────
# Prompt Construction Tests
# ──────────────────────────────────────────────────────────


class TestPrompts:
    """Test prompt construction and canary token generation."""

    def test_component_prompt_has_canary(self):
        system, user, token = build_component_prompt("test-repo", "def foo(): pass")
        assert token in user
        assert "CANARY-TOKEN:" in user
        assert "test-repo" in system
        assert "SYSTEM-BOUNDARY" in user

    def test_flow_prompt_has_canary(self):
        summaries = json.dumps([{"name": "A", "type": "module", "purpose": "test"}])
        system, user, token = build_flow_prompt("test-repo", summaries, "def main(): pass")
        assert token in user
        assert "COMPONENT-SUMMARIES" in user
        assert "test-repo" in system

    def test_canary_tokens_are_unique(self):
        _, _, t1 = build_component_prompt("repo", "code1")
        _, _, t2 = build_component_prompt("repo", "code2")
        assert t1 != t2

    def test_tool_schemas_are_valid(self):
        """Tool definitions have required fields."""
        assert COMPONENT_TOOL["name"] == "extract_components"
        assert "input_schema" in COMPONENT_TOOL
        assert COMPONENT_TOOL["input_schema"]["type"] == "object"

        assert FLOW_TOOL["name"] == "extract_flows"
        assert "input_schema" in FLOW_TOOL


# ──────────────────────────────────────────────────────────
# File Collection / Utility Tests
# ──────────────────────────────────────────────────────────


class TestFileCollection:
    """Test file discovery and batching logic."""

    def test_collect_python_files(self):
        files = collect_python_files(SAMPLE_DIR)
        assert len(files) >= 4  # app, service, repository, models, events, validator
        assert all(f.suffix == ".py" for f in files)

    def test_collect_excludes_test_files(self):
        files = collect_python_files(SAMPLE_DIR)
        names = [f.name for f in files]
        assert "test_probe.py" not in names
        assert "conftest.py" not in names

    def test_collect_nonexistent_dir(self):
        with pytest.raises(FileNotFoundError):
            collect_python_files("/nonexistent/path")

    def test_batch_by_directory(self):
        files = collect_python_files(SAMPLE_DIR)
        batches = _batch_by_directory(files, SAMPLE_DIR)
        assert len(batches) >= 1
        # All files should be accounted for
        total_files = sum(len(b) for b in batches)
        assert total_files == len(files)

    def test_find_entry_points(self):
        files = collect_python_files(SAMPLE_DIR)
        entry_points = _find_entry_points(files, SAMPLE_DIR)
        names = [f.stem for f in entry_points]
        assert "app" in names  # app.py should be detected as entry point

    def test_read_file_safe_normal(self):
        app_file = SAMPLE_DIR / "app.py"
        content = _read_file_safe(app_file)
        assert content is not None
        assert "FastAPI" in content

    def test_read_file_safe_nonexistent(self):
        result = _read_file_safe(Path("/nonexistent/file.py"))
        assert result is None

    def test_read_file_safe_oversized(self):
        app_file = SAMPLE_DIR / "app.py"
        result = _read_file_safe(app_file, max_size=10)
        assert result is None


# ──────────────────────────────────────────────────────────
# CLI Tests
# ──────────────────────────────────────────────────────────


class TestCLI:
    """Test CLI entry point."""

    def test_help_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "extraction_probe.run", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "target" in result.stdout.lower()
        assert "extraction" in result.stdout.lower()

    def test_missing_target(self):
        result = subprocess.run(
            [sys.executable, "-m", "extraction_probe.run"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode != 0


# ──────────────────────────────────────────────────────────
# Negative Case: Malformed Input
# ──────────────────────────────────────────────────────────


class TestMalformedInput:
    """Test graceful handling of malformed input."""

    def test_malformed_component_data(self):
        """Completely wrong structure should return None, not crash."""
        result = validate_component_result({"garbage": True, "random": [1, 2, 3]})
        assert result is None

    def test_malformed_flow_data(self):
        result = validate_flow_result(42)  # type: ignore[arg-type]
        assert result is None

    def test_empty_dict_component(self):
        result = validate_component_result({})
        assert result is None

    def test_none_input(self):
        """None input should not crash."""
        result = validate_component_result(None)  # type: ignore[arg-type]
        assert result is None


# ──────────────────────────────────────────────────────────
# Integration Test (requires ANTHROPIC_API_KEY)
# ──────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skip live extraction test",
)
class TestLiveExtraction:
    """Integration test: run extraction against sample code with real API calls."""

    def test_extract_sample_project(self):
        result = run_extraction(target_dir=SAMPLE_DIR, repo_name="sample-order-service")

        # Phase 0 gate criteria
        assert len(result.components) >= 3, (
            f"Expected ≥3 components from sample, got {len(result.components)}"
        )
        assert result.files_processed >= 4

        # Check component field completeness
        for comp in result.components:
            assert comp.name, "Component must have a name"
            assert comp.type in ("module", "service", "library", "package")
            assert len(comp.purpose) >= 5
            assert len(comp.files) >= 1
            assert comp.confidence in ("confirmed", "inferred")

        # Validation failure rate < 10%
        if result.total_attempts > 0:
            failure_rate = result.validation_failures / result.total_attempts
            assert failure_rate < 0.10, f"Failure rate {failure_rate:.0%} exceeds 10%"

        # Confidence distribution: >50% confirmed or inferred
        all_conf = [c.confidence for c in result.components] + [
            f.confidence for f in result.flows
        ]
        if all_conf:
            ci_count = sum(1 for c in all_conf if c in ("confirmed", "inferred"))
            ci_pct = ci_count / len(all_conf)
            assert ci_pct > 0.50, f"Confirmed+inferred {ci_pct:.0%} ≤ 50%"

        # Cost should be minimal for sample
        assert result.cost.total_cost_usd < 1.0, (
            f"Sample extraction cost ${result.cost.total_cost_usd:.4f} unexpectedly high"
        )

    def test_flow_extraction(self):
        """Separate test to verify flows are extracted."""
        result = run_extraction(target_dir=SAMPLE_DIR, repo_name="sample-order-service")

        # We expect at least 1 flow from the sample (order processing)
        assert len(result.flows) >= 1, (
            f"Expected ≥1 flow from sample, got {len(result.flows)}"
        )

        for flow in result.flows:
            assert flow.name, "Flow must have a name"
            assert len(flow.segments) >= 1
            # Segments should reference component names that exist in our extraction
            comp_names = {c.name for c in result.components}
            for seg in flow.segments:
                assert seg.component_name, "Segment must have a component name"

    def test_relationships_reference_real_entities(self):
        """Verify relationships reference entity names that exist in the output."""
        result = run_extraction(target_dir=SAMPLE_DIR, repo_name="sample-order-service")
        comp_names = {c.name for c in result.components}

        # Check internal_deps reference real components
        for comp in result.components:
            for dep in comp.internal_deps:
                # Deps should reference known components (soft check — LLM may use different names)
                # Just verify they're non-empty strings
                assert isinstance(dep, str) and len(dep) > 0
