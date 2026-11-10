"""Tests for extraction pipeline phases G-I (relationship synthesis,
knowledge index compression, confidence scoring).

Uses MockAdapter fixtures and mocked Anthropic client to test the full
A-I pipeline without requiring real LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bifrost.extraction.pipeline import (
    ConfidenceScore,
    ExtractedRelationship,
    ExtractionPipeline,
    KnowledgeIndexEntry,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Mock helpers (reused pattern from test_extraction_af.py) ─────────


@dataclass
class MockToolUseBlock:
    type: str = "tool_use"
    id: str = "test-id"
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class MockResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"


def _make_triage_response(classifications: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[MockToolUseBlock(name="classify_files", input={"files": classifications})]
    )


def _make_component_response(components: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[MockToolUseBlock(name="extract_components", input={"components": components})]
    )


def _make_flow_response(flows: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(content=[MockToolUseBlock(name="extract_flows", input={"flows": flows})])


def _make_dependency_response(deps: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[MockToolUseBlock(name="extract_dependencies", input={"dependencies": deps})]
    )


def _make_relationship_response(rels: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[MockToolUseBlock(name="synthesize_relationships", input={"relationships": rels})]
    )


def _make_knowledge_index_response(entries: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[MockToolUseBlock(name="compress_knowledge_index", input={"entries": entries})]
    )


def _make_confidence_response(scores: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(
        content=[MockToolUseBlock(name="score_confidence", input={"scores": scores})]
    )


def _mock_client(*responses: MockResponse) -> AsyncMock:
    """Create a mock Anthropic client that returns the given responses in order."""
    client = AsyncMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    return client


# ── Full A-I pipeline test ───────────────────────────────────────────


class TestFullAIPipeline:
    @pytest.mark.asyncio
    async def test_full_pipeline_produces_all_outputs(self) -> None:
        """Run full A-I pipeline and verify all output types are populated."""
        fixture_content = (FIXTURES_DIR / "sample_module.py").read_text()

        client = _mock_client(
            # Phase A: Triage
            _make_triage_response(
                [
                    {
                        "path": "sample_module.py",
                        "category": "component_definition",
                        "likely_key_component": True,
                    },
                ]
            ),
            # Phase D: Components
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
                    {
                        "component_name": "CarrierGateway",
                        "component_type": "library",
                        "purpose": "External carrier provider client",
                        "source_file": "sample_module.py",
                        "exported_interfaces": ["book"],
                        "internal_dependencies": [],
                        "known_gaps": ["Rate limiting not visible"],
                    },
                ]
            ),
            # Phase E: Flows
            _make_flow_response(
                [
                    {
                        "flow_name": "Shipment Creation Flow",
                        "flow_type": "request-response",
                        "segments": [
                            {
                                "component_name": "ShipmentService",
                                "segment_type": "sync-call",
                                "description": "Receive shipment request",
                            },
                            {
                                "component_name": "CarrierGateway",
                                "segment_type": "external-api",
                                "description": "Book via gateway",
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
            # Phase F: Dependencies
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
            # Phase G: Relationships
            _make_relationship_response(
                [
                    {
                        "source_urn": "urn:bifrost:repo/test-repo/component/ShipmentService",
                        "target_urn": "urn:bifrost:repo/test-repo/component/CarrierGateway",
                        "relationship_type": "calls",
                        "description": "ShipmentService calls CarrierGateway for shipment creation",
                        "evidence": "sample_module.py: ShipmentService.create_shipment calls gateway.book()()",
                    },
                ]
            ),
            # Phase H: Knowledge index
            _make_knowledge_index_response(
                [
                    {
                        "entity_urn": "urn:bifrost:repo/test-repo/component/ShipmentService",
                        "summary": "ShipmentService processes orders through a gateway. It exposes create_shipment and cancel_shipment interfaces.",
                        "key_facts": [
                            "Processes orders",
                            "Uses CarrierGateway",
                            "Stores results in DB",
                        ],
                        "search_keywords": ["shipment", "book", "cancel", "gateway"],
                    },
                    {
                        "entity_urn": "urn:bifrost:repo/test-repo/component/CarrierGateway",
                        "summary": "CarrierGateway is an external carrier provider client that exposes a book interface.",
                        "key_facts": ["External API client", "Booking capability"],
                        "search_keywords": ["gateway", "external", "carrier provider"],
                    },
                    {
                        "entity_urn": "urn:bifrost:repo/test-repo/flow/Shipment Creation Flow",
                        "summary": "Request-response flow: ShipmentService receives shipment, calls CarrierGateway, stores result.",
                        "key_facts": ["3 steps", "Involves external API call", "Writes to DB"],
                        "search_keywords": ["shipment flow", "processing", "request-response"],
                    },
                ]
            ),
            # Phase I: Confidence scores
            _make_confidence_response(
                [
                    {
                        "entity_urn": "urn:bifrost:repo/test-repo/component/ShipmentService",
                        "confidence_level": "confirmed",
                        "confidence_score": 0.92,
                        "reason": "Clear class definition with explicit interfaces, confirmed by flow and dependency data",
                    },
                    {
                        "entity_urn": "urn:bifrost:repo/test-repo/component/CarrierGateway",
                        "confidence_level": "inferred",
                        "confidence_score": 0.70,
                        "reason": "Referenced by ShipmentService but implementation details not fully visible",
                    },
                    {
                        "entity_urn": "urn:bifrost:repo/test-repo/flow/Shipment Creation Flow",
                        "confidence_level": "inferred",
                        "confidence_score": 0.75,
                        "reason": "Flow reconstructed from component analysis, not directly observed",
                    },
                ]
            ),
        )

        pipeline = ExtractionPipeline(
            client=client,
            repo_id="test-repo",
        )

        async def read_file(path: str) -> str:
            return fixture_content

        output = await pipeline.run(
            file_paths=["sample_module.py"],
            read_file=read_file,
            file_previews={"sample_module.py": fixture_content[:200]},
        )

        # Verify A-F outputs (sanity check)
        assert len(output.components) == 2
        assert len(output.flows) == 1
        assert len(output.dependencies) == 1

        # Verify Phase G: Relationships
        assert len(output.relationships) >= 1
        rel = output.relationships[0]
        assert isinstance(rel, ExtractedRelationship)
        assert "urn:bifrost:repo/test-repo/component/" in rel.source_urn
        assert "urn:bifrost:repo/test-repo/component/" in rel.target_urn
        assert rel.relationship_type == "calls"

        # Verify Phase H: Knowledge index
        assert len(output.knowledge_index) == 3
        for entry in output.knowledge_index:
            assert isinstance(entry, KnowledgeIndexEntry)
            assert entry.entity_urn.startswith("urn:bifrost:")
            # 5K token limit ~ 20K chars
            assert len(entry.summary) <= 20000
            assert len(entry.key_facts) <= 10

        # Verify Phase I: Confidence scores
        assert len(output.confidence_scores) == 3
        valid_levels = {"confirmed", "inferred", "stale", "uncertain", "unknown", "conflicting"}
        for score in output.confidence_scores:
            assert isinstance(score, ConfidenceScore)
            assert score.confidence_level in valid_levels
            assert 0.0 <= score.confidence_score <= 1.0
            assert score.reason  # non-empty reason

    @pytest.mark.asyncio
    async def test_pipeline_handles_phase_g_failure(self) -> None:
        """If Phase G fails, pipeline should continue with H and I."""
        client = AsyncMock()
        client.messages.create = AsyncMock(
            side_effect=[
                # Triage
                _make_triage_response(
                    [
                        {
                            "path": "main.py",
                            "category": "component_definition",
                            "likely_key_component": True,
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
                # Flows
                _make_flow_response([]),
                # Deps
                _make_dependency_response([]),
                # Phase G: FAILS
                Exception("Rate limit exceeded"),
                Exception("Rate limit exceeded"),
                Exception("Rate limit exceeded"),
                # Phase H: succeeds
                _make_knowledge_index_response(
                    [
                        {
                            "entity_urn": "urn:bifrost:repo/test/component/App",
                            "summary": "Main application module",
                            "key_facts": ["Entry point"],
                            "search_keywords": ["app", "main"],
                        },
                    ]
                ),
                # Phase I: succeeds
                _make_confidence_response(
                    [
                        {
                            "entity_urn": "urn:bifrost:repo/test/component/App",
                            "confidence_level": "inferred",
                            "confidence_score": 0.65,
                            "reason": "Single source file extraction",
                        },
                    ]
                ),
            ]
        )

        pipeline = ExtractionPipeline(client=client, repo_id="test")
        output = await pipeline.run(
            file_paths=["main.py"],
            read_file=AsyncMock(return_value="def main(): pass"),
        )

        assert len(output.relationships) == 0
        assert any("Relationship synthesis failed" in e for e in output.errors)
        assert len(output.knowledge_index) == 1
        assert len(output.confidence_scores) == 1
        assert output.partial is True

    @pytest.mark.asyncio
    async def test_empty_extraction_skips_ghi(self) -> None:
        """If no entities extracted, phases G-I should be skipped."""
        client = _mock_client(
            _make_triage_response([]),
        )
        pipeline = ExtractionPipeline(client=client, repo_id="empty")
        output = await pipeline.run(
            file_paths=[],
            read_file=AsyncMock(),
        )
        assert len(output.relationships) == 0
        assert len(output.knowledge_index) == 0
        assert len(output.confidence_scores) == 0


# ── Phase G: Relationship synthesis ──────────────────────────────────


class TestRelationshipSynthesis:
    @pytest.mark.asyncio
    async def test_relationships_have_valid_urns(self) -> None:
        """All relationships must have properly formatted source/target URNs."""
        client = _mock_client(
            _make_triage_response(
                [
                    {
                        "path": "svc.py",
                        "category": "component_definition",
                        "likely_key_component": True,
                    },
                ]
            ),
            _make_component_response(
                [
                    {
                        "component_name": "ServiceA",
                        "component_type": "service",
                        "purpose": "Does A",
                        "source_file": "svc.py",
                    },
                    {
                        "component_name": "ServiceB",
                        "component_type": "service",
                        "purpose": "Does B",
                        "source_file": "svc.py",
                    },
                ]
            ),
            _make_flow_response([]),
            _make_dependency_response(
                [
                    {
                        "source_name": "ServiceA",
                        "target_name": "ServiceB",
                        "dep_type": "code",
                        "discovery_method": "static-analysis",
                    },
                ]
            ),
            _make_relationship_response(
                [
                    {
                        "source_urn": "urn:bifrost:repo/test/component/ServiceA",
                        "target_urn": "urn:bifrost:repo/test/component/ServiceB",
                        "relationship_type": "depends-on",
                        "description": "ServiceA imports ServiceB",
                        "evidence": "import statement in svc.py",
                    },
                ]
            ),
            _make_knowledge_index_response(
                [
                    {
                        "entity_urn": "urn:bifrost:repo/test/component/ServiceA",
                        "summary": "Does A",
                        "key_facts": [],
                        "search_keywords": [],
                    },
                    {
                        "entity_urn": "urn:bifrost:repo/test/component/ServiceB",
                        "summary": "Does B",
                        "key_facts": [],
                        "search_keywords": [],
                    },
                ]
            ),
            _make_confidence_response(
                [
                    {
                        "entity_urn": "urn:bifrost:repo/test/component/ServiceA",
                        "confidence_level": "inferred",
                        "confidence_score": 0.7,
                        "reason": "Single file",
                    },
                    {
                        "entity_urn": "urn:bifrost:repo/test/component/ServiceB",
                        "confidence_level": "inferred",
                        "confidence_score": 0.7,
                        "reason": "Single file",
                    },
                ]
            ),
        )

        pipeline = ExtractionPipeline(client=client, repo_id="test")
        output = await pipeline.run(
            file_paths=["svc.py"],
            read_file=AsyncMock(return_value="class ServiceA: pass\nclass ServiceB: pass"),
        )

        assert len(output.relationships) == 1
        rel = output.relationships[0]
        assert rel.source_urn.startswith("urn:bifrost:")
        assert rel.target_urn.startswith("urn:bifrost:")
        assert rel.relationship_type in {
            "calls",
            "uses",
            "depends-on",
            "triggers",
            "extends",
            "implements",
            "contains",
            "produces",
            "consumes",
        }


# ── Phase H: Knowledge index compression ─────────────────────────────


class TestKnowledgeIndexCompression:
    def test_knowledge_index_entry_fields(self) -> None:
        entry = KnowledgeIndexEntry(
            entity_urn="urn:bifrost:repo/test/component/Svc",
            summary="A service that does things",
            key_facts=["Fact 1", "Fact 2"],
            search_keywords=["service", "things"],
        )
        assert entry.entity_urn.startswith("urn:bifrost:")
        assert len(entry.summary) <= 20000
        assert len(entry.key_facts) <= 10

    def test_summary_truncation(self) -> None:
        """Summaries exceeding 20K chars should be truncated in pipeline."""
        entry = KnowledgeIndexEntry(
            entity_urn="urn:bifrost:repo/test/component/Big",
            summary="x" * 25000,
        )
        # The pipeline truncates at parse time, but the dataclass itself doesn't enforce
        assert len(entry.summary) == 25000  # dataclass allows it


# ── Phase I: Confidence scoring ──────────────────────────────────────


class TestConfidenceScoring:
    def test_confidence_score_fields(self) -> None:
        score = ConfidenceScore(
            entity_urn="urn:bifrost:repo/test/component/Svc",
            confidence_level="confirmed",
            confidence_score=0.92,
            reason="Multiple sources agree",
        )
        assert score.confidence_level == "confirmed"
        assert 0.0 <= score.confidence_score <= 1.0

    def test_confidence_level_values(self) -> None:
        """Verify all canonical ConfidenceLevel values are accepted."""
        valid_levels = ["confirmed", "inferred", "stale", "uncertain", "unknown", "conflicting"]
        for level in valid_levels:
            score = ConfidenceScore(
                entity_urn="urn:test",
                confidence_level=level,
                confidence_score=0.5,
                reason="Test",
            )
            assert score.confidence_level == level


# ── Phase completion callback ────────────────────────────────────────


class TestPhaseCallback:
    @pytest.mark.asyncio
    async def test_on_phase_complete_called(self) -> None:
        """Verify the on_phase_complete callback fires for each phase."""
        completed_phases: list[str] = []

        async def track_phase(phase: str) -> None:
            completed_phases.append(phase)

        client = _mock_client(
            _make_triage_response(
                [
                    {
                        "path": "app.py",
                        "category": "component_definition",
                        "likely_key_component": False,
                    },
                ]
            ),
            _make_component_response(
                [
                    {
                        "component_name": "App",
                        "component_type": "module",
                        "purpose": "Main",
                        "source_file": "app.py",
                    },
                ]
            ),
            _make_flow_response([]),
            _make_dependency_response([]),
            _make_relationship_response([]),
            _make_knowledge_index_response(
                [
                    {
                        "entity_urn": "urn:bifrost:repo/cb/component/App",
                        "summary": "Main",
                        "key_facts": [],
                        "search_keywords": [],
                    },
                ]
            ),
            _make_confidence_response(
                [
                    {
                        "entity_urn": "urn:bifrost:repo/cb/component/App",
                        "confidence_level": "inferred",
                        "confidence_score": 0.6,
                        "reason": "Test",
                    },
                ]
            ),
        )

        pipeline = ExtractionPipeline(
            client=client,
            repo_id="cb",
            on_phase_complete=track_phase,
        )
        await pipeline.run(
            file_paths=["app.py"],
            read_file=AsyncMock(return_value="class App: pass"),
        )

        assert "F" in completed_phases
        assert "G" in completed_phases
        assert "H" in completed_phases
        assert "I" in completed_phases
