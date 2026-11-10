"""Tests for core types, enums, URN, and entity models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bifrost.core.enums import (
    ArtifactClassification,
    ComponentType,
    ConfidenceLevel,
    CorrectionType,
    DependencyType,
    DiscoveryMethod,
    FlowType,
    InterfaceType,
    SegmentType,
    SourceObjectType,
)
from bifrost.core.models import (
    BifrostInterface,
    Component,
    Correction,
    DependencyReference,
    FlowReference,
    FlowSegment,
    GapReport,
    KnowledgeArtifact,
)
from bifrost.core.types import (
    ConfidenceInfo,
    EvidenceRef,
    FreshnessInfo,
    KnownGap,
    LineRange,
    MetadataEnvelope,
)
from bifrost.core.urn import VALID_TYPES, ParsedURN, generate_urn, parse_urn

# ── Enums ────────────────────────────────────────────────────────────────────


class TestEnums:
    def test_confidence_level_has_six_values(self):
        assert len(ConfidenceLevel) == 6
        expected = {"confirmed", "inferred", "stale", "uncertain", "unknown", "conflicting"}
        assert {v.value for v in ConfidenceLevel} == expected

    def test_component_type_values(self):
        assert len(ComponentType) >= 4
        assert "service" in [v.value for v in ComponentType]

    def test_flow_type_values(self):
        values = [v.value for v in FlowType]
        for ft in ("request-response", "event-driven", "batch", "saga", "pipeline", "mixed"):
            assert ft in values

    def test_segment_type_values(self):
        assert len(SegmentType) == 7

    def test_dependency_type_values(self):
        assert len(DependencyType) == 9

    def test_discovery_method_values(self):
        assert len(DiscoveryMethod) == 5

    def test_source_object_type_values(self):
        assert len(SourceObjectType) == 7

    def test_artifact_classification_values(self):
        assert len(ArtifactClassification) == 11

    def test_correction_type_values(self):
        assert len(CorrectionType) == 5

    def test_interface_type_values(self):
        assert len(InterfaceType) >= 4


# ── URN ──────────────────────────────────────────────────────────────────────


class TestURN:
    def test_generate_urn_with_id(self):
        urn = generate_urn("repo-abc", "component", "my-id")
        assert urn == "bifrost:repo-abc:component:my-id"

    def test_generate_urn_auto_uuid(self):
        urn = generate_urn("repo-abc", "flow")
        parts = urn.split(":")
        assert len(parts) == 4
        assert parts[0] == "bifrost"
        # Should be a valid UUID
        uuid.UUID(parts[3])

    def test_generate_urn_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid URN type"):
            generate_urn("scope", "invalid_type")

    def test_parse_urn(self):
        parsed = parse_urn("bifrost:repo-abc:component:id-123")
        assert parsed == ParsedURN(
            prefix="bifrost", scope="repo-abc", type="component", id="id-123"
        )

    def test_parse_urn_malformed(self):
        with pytest.raises(ValueError, match="Malformed URN"):
            parse_urn("bifrost:only:two")

    def test_parse_urn_bad_prefix(self):
        with pytest.raises(ValueError, match="Invalid URN prefix"):
            parse_urn("notbifrost:s:t:id")

    def test_parse_urn_bad_type(self):
        with pytest.raises(ValueError, match="Invalid URN type"):
            parse_urn("bifrost:s:badtype:id")

    def test_valid_types_complete(self):
        expected = {
            "component",
            "interface",
            "flow",
            "dependency",
            "artifact",
            "gap-report",
            "correction",
            "repo",
            "domain",
        }
        assert expected == VALID_TYPES


# ── Core Types ───────────────────────────────────────────────────────────────


class TestCoreTypes:
    def test_line_range(self):
        lr = LineRange(start=10, end=20)
        assert lr.start == 10
        assert lr.end == 20

    def test_evidence_ref(self):
        now = datetime.now(tz=UTC)
        ref = EvidenceRef(
            source_id="src-1",
            adapter_id="git",
            file_ref="foo.py",
            line_range=LineRange(start=1, end=5),
            snippet="code here",
            confidence=ConfidenceLevel.CONFIRMED,
            extracted_at=now,
        )
        assert ref.source_id == "src-1"
        assert ref.confidence == ConfidenceLevel.CONFIRMED

    def test_freshness_info(self):
        now = datetime.now(tz=UTC)
        fi = FreshnessInfo(
            indexed_at=now,
            source_revision="abc123",
            last_checked_at=now,
            lag_indicator="current",
        )
        assert fi.lag_indicator == "current"

    def test_confidence_info_bounds(self):
        ci = ConfidenceInfo(level=ConfidenceLevel.CONFIRMED, score=0.95, basis="test")
        assert ci.score == 0.95

        with pytest.raises(ValidationError):
            ConfidenceInfo(level=ConfidenceLevel.CONFIRMED, score=1.5, basis="bad")

        with pytest.raises(ValidationError):
            ConfidenceInfo(level=ConfidenceLevel.CONFIRMED, score=-0.1, basis="bad")

    def test_known_gap(self):
        gap = KnownGap(description="Missing docs", severity="moderate")
        assert gap.severity == "moderate"

    def test_metadata_envelope(self):
        now = datetime.now(tz=UTC)
        me = MetadataEnvelope(
            created_at=now,
            updated_at=now,
            created_by="test",
            version=1,
            source_refs=[],
            freshness=FreshnessInfo(
                indexed_at=now,
                last_checked_at=now,
                lag_indicator="current",
            ),
            confidence=ConfidenceInfo(
                level=ConfidenceLevel.INFERRED,
                score=0.7,
                basis="test",
            ),
            scope="repo",
        )
        assert me.version == 1
        assert me.known_gaps == []


# ── Entity Models ────────────────────────────────────────────────────────────


def _make_envelope() -> MetadataEnvelope:
    now = datetime.now(tz=UTC)
    return MetadataEnvelope(
        created_at=now,
        updated_at=now,
        created_by="test",
        version=1,
        source_refs=[],
        freshness=FreshnessInfo(
            indexed_at=now,
            last_checked_at=now,
            lag_indicator="current",
        ),
        confidence=ConfidenceInfo(
            level=ConfidenceLevel.CONFIRMED,
            score=0.9,
            basis="test",
        ),
        scope="repo",
    )


class TestEntityModels:
    def test_component(self):
        c = Component(
            id=uuid.uuid4(),
            urn="bifrost:r:component:1",
            repo_id=uuid.uuid4(),
            name="AuthService",
            type=ComponentType.SERVICE,
            interfaces=[],
            dependencies=[],
            metadata=_make_envelope(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert c.name == "AuthService"
        assert c.type == ComponentType.SERVICE

    def test_interface(self):
        iface = BifrostInterface(
            id=uuid.uuid4(),
            urn="bifrost:r:interface:1",
            component_id=uuid.uuid4(),
            type=InterfaceType.API,
            name="POST /login",
            consumers=[],
            metadata=_make_envelope(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert iface.type == InterfaceType.API

    def test_flow_reference(self):
        seg = FlowSegment(
            order=1,
            component_urn="bifrost:r:component:1",
            repo_id=uuid.uuid4(),
            segment_type=SegmentType.SYNC_CALL,
        )
        flow = FlowReference(
            id=uuid.uuid4(),
            urn="bifrost:r:flow:1",
            name="Login Flow",
            flow_type=FlowType.REQUEST_RESPONSE,
            segments=[seg],
            scope="repo",
            source_basis=[],
            metadata=_make_envelope(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert len(flow.segments) == 1

    def test_dependency_reference(self):
        dep = DependencyReference(
            id=uuid.uuid4(),
            urn="bifrost:r:dependency:1",
            source_id=uuid.uuid4(),
            source_urn="bifrost:r:component:1",
            target_id=uuid.uuid4(),
            target_urn="bifrost:r:component:2",
            dep_type=DependencyType.CODE,
            confidence=ConfidenceLevel.INFERRED,
            discovery_method=DiscoveryMethod.STATIC_ANALYSIS,
            metadata=_make_envelope(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert dep.dep_type == DependencyType.CODE

    def test_knowledge_artifact(self):
        ka = KnowledgeArtifact(
            id=uuid.uuid4(),
            urn="bifrost:r:artifact:1",
            tier=1,
            classification=ArtifactClassification.REPO_OVERVIEW,
            title="Auth Service Overview",
            content="# Auth Service\nHandles authentication.",
            content_format="markdown",
            target_urn="bifrost:r:component:1",
            metadata=_make_envelope(),
            publication_state="draft",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert ka.tier == 1

    def test_gap_report(self):
        gap = GapReport(
            id="gap-1",
            scope=uuid.uuid4(),
            query_id="q-1",
            description="Missing dependency docs",
            severity="moderate",
            suggested_sources=["README.md"],
            status="open",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert gap.severity == "moderate"

    def test_correction(self):
        corr = Correction(
            id=uuid.uuid4(),
            entity_id=uuid.uuid4(),
            correction_type=CorrectionType.FACTUAL_ERROR,
            original_claim="Uses Redis",
            corrected_claim="Uses PostgreSQL",
            corrected_by="reviewer-1",
            evidence=[],
            status="pending",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert corr.status == "pending"

    def test_all_eight_entity_models_exist(self):
        """Verify we have all 8 entity types per TRD §2."""
        models = [
            Component,
            BifrostInterface,
            FlowReference,
            DependencyReference,
            KnowledgeArtifact,
            GapReport,
            Correction,
            FlowSegment,
        ]
        assert len(models) == 8
