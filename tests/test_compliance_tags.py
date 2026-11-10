"""Generic compliance tags replace the two fixed compliance booleans."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from bifrost.manifest.model import ServiceManifest
from bifrost.research.fast_path import _build_ownership_prompt
from bifrost.research.ownership_sync import manifest_to_ownership_values
from bifrost.storage.models import RepoOwnership

_BASELINE = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "001_baseline.py"
_OLD_COLUMNS = ("is_pci_in_scope", "is_sox_in_scope")


def test_orm_has_compliance_tags_and_no_boolean_columns() -> None:
    columns = RepoOwnership.__table__.columns
    assert "compliance_tags" in columns
    assert not columns["compliance_tags"].nullable
    for old in _OLD_COLUMNS:
        assert old not in columns
    assert not hasattr(RepoOwnership, "is_pci_in_scope")
    assert not hasattr(RepoOwnership, "is_sox_in_scope")


def test_baseline_declares_compliance_tags_only() -> None:
    text = _BASELINE.read_text()
    assert '"compliance_tags"' in text
    for old in _OLD_COLUMNS:
        assert old not in text


def test_manifest_compliance_is_normalised() -> None:
    md = ServiceManifest(name="svc", compliance=["SOX", "hipaa", "sox", " "])
    values = manifest_to_ownership_values(uuid.uuid4(), md)
    assert values["compliance_tags"] == ["hipaa", "sox"]
    for old in _OLD_COLUMNS:
        assert old not in values


def test_manifest_without_compliance_yields_empty_list() -> None:
    values = manifest_to_ownership_values(uuid.uuid4(), ServiceManifest(name="svc"))
    assert values["compliance_tags"] == []


def _render(tags: list[str]) -> str:
    repo = SimpleNamespace(name="svc", urn="urn:x", url="http://x", description=None)
    own = SimpleNamespace(
        squad=None,
        service_tier=None,
        jira_project_key=None,
        confluence_space_key=None,
        slack_channel=None,
        data_classification=None,
        compliance_tags=tags,
    )
    return _build_ownership_prompt(repo, own, "who owns it?")  # type: ignore[arg-type]


def test_fast_path_template_renders_tags() -> None:
    assert "- Compliance tags: hipaa, sox" in _render(["hipaa", "sox"])


def test_fast_path_template_renders_none_when_empty() -> None:
    out = _render([])
    assert "- Compliance tags: none" in out
    assert "PCI=" not in out
