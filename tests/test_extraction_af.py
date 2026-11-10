"""Tests for extraction pipeline phases A-F.

Uses MockAdapter fixtures and mocked Anthropic client to test
triage, component/flow/dependency extraction, and content validation
without requiring real LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bifrost.extraction.pipeline import (
    ContentTooLargeError,
    ExtractedComponent,
    ExtractionOutput,
    ExtractionPipeline,
    _validate_content_size,
)
from bifrost.extraction.prompts import (
    MAX_CHARS_PER_CALL,
    build_canary,
    build_component_messages,
    build_dependency_messages,
    build_flow_messages,
    build_triage_messages,
    should_exclude_path,
    validate_canary,
)
from bifrost.extraction.triage import TriagedFile, TriageResult, triage_files

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Helpers for mocking Anthropic responses ─────────────────────────


@dataclass
class MockToolUseBlock:
    type: str = "tool_use"
    id: str = "test-id"
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class MockTextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class MockResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"


def _make_triage_response(classifications: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[
            MockToolUseBlock(
                name="classify_files",
                input={"files": classifications},
            )
        ]
    )


def _make_component_response(components: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[
            MockToolUseBlock(
                name="extract_components",
                input={"components": components},
            )
        ]
    )


def _make_flow_response(flows: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[
            MockToolUseBlock(
                name="extract_flows",
                input={"flows": flows},
            )
        ]
    )


def _make_dependency_response(deps: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[
            MockToolUseBlock(
                name="extract_dependencies",
                input={"dependencies": deps},
            )
        ]
    )


def _mock_client(*responses: MockResponse) -> AsyncMock:
    """Create a mock Anthropic client that returns the given responses in order."""
    client = AsyncMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    return client


# ── Phase A: Triage tests ──────────────────────────────────────────


class TestShouldExcludePath:
    def test_excludes_node_modules(self) -> None:
        assert should_exclude_path("node_modules/react/index.js") is True

    def test_excludes_lock_files(self) -> None:
        assert should_exclude_path("package-lock.json") is False  # not .lock extension
        assert should_exclude_path("yarn.lock") is True

    def test_excludes_pycache(self) -> None:
        assert should_exclude_path("src/__pycache__/main.cpython-312.pyc") is True

    def test_excludes_generated(self) -> None:
        assert should_exclude_path("src/api.generated.ts") is True

    def test_excludes_min_js(self) -> None:
        assert should_exclude_path("dist/bundle.min.js") is True

    def test_allows_normal_files(self) -> None:
        assert should_exclude_path("src/main.py") is False
        assert should_exclude_path("src/services/ShipmentService.ts") is False


class TestTriageFiles:
    @pytest.mark.asyncio
    async def test_triage_pre_filters_excluded(self) -> None:
        client = _mock_client(
            _make_triage_response(
                [
                    {
                        "path": "src/main.py",
                        "category": "component_definition",
                        "likely_key_component": True,
                    },
                ]
            )
        )
        result = await triage_files(
            ["src/main.py", "node_modules/react/index.js", "__pycache__/test.pyc"],
            {"src/main.py": "def main(): pass"},
            client,
        )
        assert result.skipped_count >= 2
        assert "src/main.py" in result.component_files

    @pytest.mark.asyncio
    async def test_triage_classifies_files(self) -> None:
        client = _mock_client(
            _make_triage_response(
                [
                    {
                        "path": "src/handler.py",
                        "category": "component_definition",
                        "likely_key_component": True,
                    },
                    {
                        "path": "requirements.txt",
                        "category": "dependency_manifest",
                        "likely_key_component": False,
                    },
                    {
                        "path": "tests/test_main.py",
                        "category": "test",
                        "likely_key_component": False,
                    },
                    {
                        "path": "README.md",
                        "category": "documentation",
                        "likely_key_component": False,
                    },
                ]
            )
        )
        result = await triage_files(
            ["src/handler.py", "requirements.txt", "tests/test_main.py", "README.md"],
            {},
            client,
        )
        assert "src/handler.py" in result.component_files
        assert "requirements.txt" in result.dependency_manifests
        assert "README.md" in result.documentation_files
        assert result.skipped_count >= 1  # test file

    @pytest.mark.asyncio
    async def test_triage_prioritized_order(self) -> None:
        result = TriageResult(
            files=[
                TriagedFile("src/utils.py", "component_definition", likely_key_component=False),
                TriagedFile(
                    "src/ShipmentHandler.py", "component_definition", likely_key_component=True
                ),
                TriagedFile("src/api.yaml", "interface_definition"),
                TriagedFile("docker-compose.yml", "configuration"),
            ],
            component_files=["src/utils.py", "src/ShipmentHandler.py"],
            interface_files=["src/api.yaml"],
            config_files=["docker-compose.yml"],
        )
        prioritized = result.prioritized_for_extraction()
        # Key components first
        assert prioritized[0] == "src/ShipmentHandler.py"
        assert "src/utils.py" in prioritized
        assert "src/api.yaml" in prioritized


# ── Content size validation ─────────────────────────────────────────


class TestContentValidation:
    def test_rejects_oversized_content(self) -> None:
        with pytest.raises(ContentTooLargeError, match="50K"):
            _validate_content_size("x" * (MAX_CHARS_PER_CALL + 1))

    def test_accepts_normal_content(self) -> None:
        _validate_content_size("def main(): pass")  # Should not raise

    def test_pipeline_validate_content_size(self) -> None:
        """Verify the public alias on ExtractionPipeline works."""
        pipeline = ExtractionPipeline.__new__(ExtractionPipeline)
        with pytest.raises((ContentTooLargeError, ValueError), match="50K|too large"):
            pipeline._validate_content_size("x" * (MAX_CHARS_PER_CALL + 1))


# ── Canary tokens ──────────────────────────────────────────────────


class TestCanary:
    def test_canary_generation(self) -> None:
        c1 = build_canary()
        c2 = build_canary()
        assert c1 != c2
        assert c1.startswith("BIFROST-CANARY-")

    def test_canary_validation_safe(self) -> None:
        canary = build_canary()
        assert validate_canary("Normal response text", canary) is True

    def test_canary_validation_echoed(self) -> None:
        canary = build_canary()
        assert validate_canary(f"You said: {canary}", canary) is False


# ── Prompt building ─────────────────────────────────────────────────


class TestPromptBuilding:
    def test_triage_messages(self) -> None:
        canary = build_canary()
        msgs = build_triage_messages(
            [{"path": "src/main.py", "preview": "def main(): pass"}],
            canary,
        )
        assert len(msgs) == 2
        assert "src/main.py" in msgs[0]["content"]
        assert canary in msgs[1]["content"]

    def test_component_messages(self) -> None:
        canary = build_canary()
        msgs = build_component_messages("src/main.py", "def main(): pass", canary)
        assert len(msgs) == 1
        assert "src/main.py" in msgs[0]["content"]
        assert canary in msgs[0]["content"]

    def test_flow_messages(self) -> None:
        canary = build_canary()
        summaries = [{"name": "ShipmentService", "type": "service", "purpose": "Processes orders"}]
        msgs = build_flow_messages("src/handler.py", "async def handle():", summaries, canary)
        assert len(msgs) == 1
        assert "ShipmentService" in msgs[0]["content"]
        assert canary in msgs[0]["content"]

    def test_dependency_messages(self) -> None:
        canary = build_canary()
        msgs = build_dependency_messages(
            "flask==2.0\nrequests==2.28",
            [{"path": "src/main.py", "content": "import requests"}],
            canary,
        )
        assert len(msgs) == 1
        assert "flask" in msgs[0]["content"]
        assert canary in msgs[0]["content"]


# ── Phase D: Component extraction ──────────────────────────────────


class TestComponentExtraction:
    @pytest.mark.asyncio
    async def test_extracts_components_from_fixture(self) -> None:
        fixture_content = (FIXTURES_DIR / "sample_module.py").read_text()

        client = _mock_client(
            # Triage response
            _make_triage_response(
                [
                    {
                        "path": "sample_module.py",
                        "category": "component_definition",
                        "likely_key_component": True,
                    },
                ]
            ),
            # Component extraction response
            _make_component_response(
                [
                    {
                        "component_name": "ShipmentService",
                        "component_type": "service",
                        "purpose": "Processes orders through a gateway",
                        "source_file": "sample_module.py",
                        "exported_interfaces": ["create_shipment", "cancel_shipment"],
                        "internal_dependencies": ["CarrierGateway"],
                        "known_gaps": [],
                    },
                ]
            ),
            # Flow extraction response
            _make_flow_response(
                [
                    {
                        "flow_name": "Shipment Creation Flow",
                        "flow_type": "request-response",
                        "segments": [
                            {
                                "component_name": "ShipmentService",
                                "segment_type": "sync-call",
                                "description": "Book carrier label",
                            },
                            {
                                "component_name": "ShipmentService",
                                "segment_type": "db-write",
                                "description": "Store result",
                            },
                        ],
                        "known_gaps": [],
                    },
                ]
            ),
            # Dependency extraction response
            _make_dependency_response(
                [
                    {
                        "source_name": "ShipmentService",
                        "target_name": "CarrierGateway",
                        "dep_type": "api",
                        "discovery_method": "static-analysis",
                        "description": "ShipmentService calls CarrierGateway.book()()",
                    },
                ]
            ),
        )

        pipeline = ExtractionPipeline(
            client=client,
            triage_model="claude-opus-4-8",
            extraction_model="claude-sonnet-4-6-20250514",
        )

        async def read_file(path: str) -> str:
            return fixture_content

        output = await pipeline.run(
            file_paths=["sample_module.py"],
            read_file=read_file,
            file_previews={"sample_module.py": fixture_content[:200]},
        )

        assert isinstance(output, ExtractionOutput)
        assert len(output.components) == 1
        assert output.components[0].component_name == "ShipmentService"
        assert output.components[0].component_type == "service"
        assert "create_shipment" in output.components[0].exported_interfaces

    @pytest.mark.asyncio
    async def test_component_validates_pydantic_fields(self) -> None:
        comp = ExtractedComponent(
            component_name="TestModule",
            component_type="module",
            purpose="Testing",
            source_file="test.py",
        )
        assert comp.component_name == "TestModule"
        assert comp.exported_interfaces == []
        assert comp.known_gaps == []


# ── Phase E: Flow extraction ───────────────────────────────────────


class TestFlowExtraction:
    @pytest.mark.asyncio
    async def test_extracts_flows(self) -> None:
        client = _mock_client(
            # Triage
            _make_triage_response(
                [
                    {
                        "path": "handler.py",
                        "category": "component_definition",
                        "likely_key_component": True,
                    },
                ]
            ),
            # Components
            _make_component_response(
                [
                    {
                        "component_name": "OrderHandler",
                        "component_type": "service",
                        "purpose": "Handles HTTP requests",
                        "source_file": "handler.py",
                    },
                ]
            ),
            # Flows
            _make_flow_response(
                [
                    {
                        "flow_name": "Order Creation",
                        "flow_type": "request-response",
                        "segments": [
                            {
                                "component_name": "OrderHandler",
                                "segment_type": "sync-call",
                                "description": "Receive request",
                            },
                            {
                                "component_name": "OrderHandler",
                                "segment_type": "db-write",
                                "description": "Save order",
                            },
                        ],
                    },
                ]
            ),
            # Deps
            _make_dependency_response([]),
        )

        pipeline = ExtractionPipeline(client=client)

        output = await pipeline.run(
            file_paths=["handler.py"],
            read_file=AsyncMock(return_value="async def handle(): pass"),
        )

        assert len(output.flows) == 1
        assert output.flows[0].flow_name == "Order Creation"
        assert output.flows[0].flow_type == "request-response"
        assert len(output.flows[0].segments) == 2

    def test_flow_segment_fields(self) -> None:
        from bifrost.extraction.pipeline import ExtractedFlowSegment

        seg = ExtractedFlowSegment(
            component_name="Svc",
            segment_type="sync-call",
            description="Test",
            order=0,
        )
        assert seg.component_name == "Svc"


# ── Phase F: Dependency extraction ─────────────────────────────────


class TestDependencyExtraction:
    @pytest.mark.asyncio
    async def test_extracts_dependencies(self) -> None:
        client = _mock_client(
            # Triage
            _make_triage_response(
                [
                    {
                        "path": "main.py",
                        "category": "component_definition",
                        "likely_key_component": False,
                    },
                    {
                        "path": "requirements.txt",
                        "category": "dependency_manifest",
                        "likely_key_component": False,
                    },
                ]
            ),
            # Components
            _make_component_response(
                [
                    {
                        "component_name": "App",
                        "component_type": "module",
                        "purpose": "Main app",
                        "source_file": "main.py",
                    },
                ]
            ),
            # Flows (none)
            _make_flow_response([]),
            # Dependencies
            _make_dependency_response(
                [
                    {
                        "source_name": "App",
                        "target_name": "flask",
                        "dep_type": "code",
                        "discovery_method": "static-analysis",
                        "description": "App imports flask",
                    },
                    {
                        "source_name": "App",
                        "target_name": "requests",
                        "dep_type": "code",
                        "discovery_method": "static-analysis",
                        "description": "App imports requests",
                    },
                ]
            ),
        )

        pipeline = ExtractionPipeline(client=client)

        files = {
            "main.py": "import flask\nimport requests",
            "requirements.txt": "flask==2.0\nrequests==2.28",
        }

        async def read_file(path: str) -> str:
            return files[path]

        output = await pipeline.run(
            file_paths=["main.py", "requirements.txt"],
            read_file=read_file,
        )

        assert len(output.dependencies) == 2
        assert output.dependencies[0].target_name == "flask"
        assert output.dependencies[0].dep_type == "code"
        assert output.dependencies[1].discovery_method == "static-analysis"


# ── Full pipeline integration ──────────────────────────────────────


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_oversized_content_skipped(self) -> None:
        """Files > 50K tokens should be skipped with error, not crash the pipeline."""
        client = _mock_client(
            # Triage
            _make_triage_response(
                [
                    {
                        "path": "huge.py",
                        "category": "component_definition",
                        "likely_key_component": False,
                    },
                ]
            ),
            # Dependencies (components won't be extracted for huge files)
            _make_dependency_response([]),
        )

        pipeline = ExtractionPipeline(client=client)

        async def read_file(path: str) -> str:
            return "x" * (MAX_CHARS_PER_CALL + 100)

        output = await pipeline.run(
            file_paths=["huge.py"],
            read_file=read_file,
        )

        assert output.files_analyzed == 0
        assert any("too large" in e.lower() or "Content too large" in e for e in output.errors)
        assert output.partial is True

    @pytest.mark.asyncio
    async def test_llm_failure_does_not_crash(self) -> None:
        """If the LLM returns an error, pipeline should log and continue."""
        client = AsyncMock()
        client.messages.create = AsyncMock(
            side_effect=[
                # Triage succeeds
                _make_triage_response(
                    [
                        {
                            "path": "main.py",
                            "category": "component_definition",
                            "likely_key_component": False,
                        },
                    ]
                ),
                # Component extraction fails
                Exception("API error"),
                # Dep extraction succeeds
                _make_dependency_response([]),
            ]
        )

        pipeline = ExtractionPipeline(client=client)
        output = await pipeline.run(
            file_paths=["main.py"],
            read_file=AsyncMock(return_value="def main(): pass"),
        )

        assert len(output.components) == 0
        assert any("failed" in e.lower() for e in output.errors)

    @pytest.mark.asyncio
    async def test_empty_file_list(self) -> None:
        """Pipeline should handle empty file list gracefully."""
        client = _mock_client(
            _make_triage_response([]),
        )
        pipeline = ExtractionPipeline(client=client)
        output = await pipeline.run(
            file_paths=[],
            read_file=AsyncMock(),
        )
        assert output.files_analyzed == 0
        assert len(output.components) == 0


# ── Triage pattern classification ───────────────────────────────────


class TestFastClassify:
    """Guard against the Phase-E-hangs-on-enums bug.

    Root cause of the bug: fast_classify used to check path segments like
    'processors/' as signals for likely_key_component — so every enum inside
    processors/ would get flagged, sent to Phase E, and hang the LLM for
    600s × 3 retries = 30 min wasted wall clock per enum.

    These tests pin the filename-only behavior so the regression can't return.
    """

    def test_service_class_is_key_component(self) -> None:
        from bifrost.extraction.prompts import fast_classify

        cat, is_key = fast_classify("src/main/java/com/x/SigningService.java")
        assert cat == "component_definition"
        assert is_key is True

    def test_handler_class_is_key_component(self) -> None:
        from bifrost.extraction.prompts import fast_classify

        cat, is_key = fast_classify("src/main/java/com/x/ShipmentHandler.java")
        assert cat == "component_definition"
        assert is_key is True

    def test_enum_inside_processors_dir_is_not_key_component(self) -> None:
        """The file that triggered the production incident."""
        from bifrost.extraction.prompts import fast_classify

        cat, is_key = fast_classify(
            "src/main/java/com/acmeco/delivery/processors/http/DeliveryMatchResult.java"
        )
        assert cat == "component_definition"
        # Must be FALSE — filename has no key-component tokens
        assert is_key is False

    def test_pojo_inside_services_dir_is_not_key_component(self) -> None:
        from bifrost.extraction.prompts import fast_classify

        cat, is_key = fast_classify("src/main/java/com/x/services/dto/OrderItem.java")
        assert cat == "component_definition"
        assert is_key is False

    def test_snake_case_service_file_is_key_component(self) -> None:
        from bifrost.extraction.prompts import fast_classify

        cat, is_key = fast_classify("src/app/shipment_service.py")
        assert cat == "component_definition"
        assert is_key is True


class TestHasFlowsWorthExtracting:
    """Belt-and-suspenders: even if triage marks an enum as key, the content
    heuristic must reject it before spawning the LLM."""

    def test_enum_only_returns_false(self) -> None:
        from bifrost.extraction.pipeline import _has_flows_worth_extracting

        content = """\
package com.x;

public enum ShipmentMatchResult {
    MATCH,
    QUANTITY_MISMATCH,
    TRACKING_MISMATCH;
}
"""
        assert _has_flows_worth_extracting(content) is False

    def test_service_with_if_returns_true(self) -> None:
        from bifrost.extraction.pipeline import _has_flows_worth_extracting

        content = """\
public class SigningService {
    public Result process(Request r) {
        if (r.isValid()) {
            return handle(r);
        }
        return Result.reject();
    }
}
"""
        assert _has_flows_worth_extracting(content) is True

    def test_python_record_with_behavior_returns_true(self) -> None:
        from bifrost.extraction.pipeline import _has_flows_worth_extracting

        content = """\
async def send_order(carrier, amount):
    try:
        response = await carrier.post({"amount": amount})
    except TimeoutError:
        return None
    return response
"""
        assert _has_flows_worth_extracting(content) is True

    def test_java_record_returns_false(self) -> None:
        from bifrost.extraction.pipeline import _has_flows_worth_extracting

        content = "public record Dimensions(long length, String unit) {}"
        assert _has_flows_worth_extracting(content) is False


# ── Fixture file readability ────────────────────────────────────────


class TestFixtures:
    def test_python_fixture_readable(self) -> None:
        content = (FIXTURES_DIR / "sample_module.py").read_text()
        assert "ShipmentService" in content
        assert "create_shipment" in content

    def test_typescript_fixture_readable(self) -> None:
        content = (FIXTURES_DIR / "sample_service.ts").read_text()
        assert "OrderController" in content
        assert "createOrder" in content
