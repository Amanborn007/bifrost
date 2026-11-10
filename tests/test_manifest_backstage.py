"""Backstage catalog-info.yaml provider (pure parser plus provider wrapper)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bifrost.cli.config_sections import BackstageConfig
from bifrost.manifest.files import InMemoryRepoFiles
from bifrost.manifest.providers.backstage import BackstageProvider, parse_catalog_info

ACME = Path(__file__).resolve().parents[1] / "examples" / "acmeco"


def _doc(owner: str = "group:default/dlvr", extra_spec: str = "", meta_extra: str = "") -> str:
    return f"""
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: svc
  description: A service
{meta_extra}
spec:
  type: service
  lifecycle: production
  owner: {owner}
{extra_spec}
"""


def test_acmeco_dispatcher() -> None:
    text = (ACME / "dispatcher" / "catalog-info.yaml").read_text()
    md = parse_catalog_info(text, "dispatcher", BackstageConfig())
    assert md is not None
    assert md.name == "dispatcher"
    assert md.team == "dlvr"
    assert md.owners == []
    assert md.lifecycle == "production"
    assert md.system == "webhook-delivery"
    assert md.description == "Fan-out worker that consumes DELIVERY_QUEUE and drives deliveries"
    assert md.declared_dependencies == ["delivery-gateway", "usage-metering"]
    assert md.providers == ["backstage"]


def test_acmeco_leaf_service_has_no_dependencies() -> None:
    text = (ACME / "signing-keys" / "catalog-info.yaml").read_text()
    md = parse_catalog_info(text, "signing-keys", BackstageConfig())
    assert md is not None
    assert md.team == "plat"
    assert md.declared_dependencies == []


@pytest.mark.parametrize(
    "owner",
    ["group:default/dlvr", "group:dlvr", "dlvr", "default/dlvr"],
)
def test_group_owner_forms_give_team(owner: str) -> None:
    md = parse_catalog_info(_doc(owner), "svc", BackstageConfig())
    assert md is not None
    assert md.team == "dlvr"


def test_user_owner_is_owners_only() -> None:
    md = parse_catalog_info(_doc("user:default/jdoe"), "svc", BackstageConfig())
    assert md is not None
    assert md.team is None
    assert md.owners == ["jdoe"]


def test_depends_on_only_components_and_normalised() -> None:
    spec = """  dependsOn:
    - component:default/alpha
    - component:beta
    - resource:default/some-db
    - gamma
"""
    md = parse_catalog_info(_doc(extra_spec=spec), "svc", BackstageConfig())
    assert md is not None
    assert md.declared_dependencies == ["alpha", "beta", "gamma"]


def test_multi_document_prefers_component_named_after_repo() -> None:
    text = """
apiVersion: backstage.io/v1alpha1
kind: System
metadata: {name: sys}
---
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: other}
spec: {owner: group:default/wrong}
---
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: mine}
spec: {owner: group:default/right}
"""
    md = parse_catalog_info(text, "mine", BackstageConfig())
    assert md is not None
    assert md.name == "mine"
    assert md.team == "right"


def test_multi_document_falls_back_to_first_component() -> None:
    text = """
kind: System
metadata: {name: sys}
---
kind: Component
metadata: {name: first}
spec: {owner: group:default/one}
---
kind: Component
metadata: {name: second}
spec: {owner: group:default/two}
"""
    md = parse_catalog_info(text, "unrelated", BackstageConfig())
    assert md is not None
    assert md.name == "first"
    assert md.team == "one"


def test_no_component_returns_none() -> None:
    assert parse_catalog_info("kind: System\nmetadata: {name: s}\n", "s", BackstageConfig()) is None


def test_jira_project_key_annotation() -> None:
    meta = "  annotations:\n    jira/project-key: SHIP\n"
    md = parse_catalog_info(_doc(meta_extra=meta), "svc", BackstageConfig())
    assert md is not None
    assert md.jira_project_key == "SHIP"


def test_annotation_map_and_label_map() -> None:
    meta = (
        "  annotations:\n    acme.example/tier: '1'\n    acme.example/class: D3\n"
        "  labels:\n    region: us-eu\n"
    )
    cfg = BackstageConfig(
        annotation_map={"acme.example/tier": "tier", "acme.example/class": "data_classification"},
        label_map={"region": "region"},
    )
    md = parse_catalog_info(_doc(meta_extra=meta), "svc", cfg)
    assert md is not None
    assert md.tier == "1"
    assert md.data_classification == "D3"
    assert md.region == "us-eu"


def test_unmapped_annotations_are_ignored() -> None:
    meta = "  annotations:\n    acme.example/tier: '1'\n"
    md = parse_catalog_info(_doc(meta_extra=meta), "svc", BackstageConfig())
    assert md is not None
    assert md.tier is None


def test_unknown_map_target_raises() -> None:
    meta = "  annotations:\n    x/y: v\n"
    cfg = BackstageConfig(annotation_map={"x/y": "not_a_field"})
    with pytest.raises(ValueError, match="not_a_field"):
        parse_catalog_info(_doc(meta_extra=meta), "svc", cfg)


def test_malformed_yaml_returns_none_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        assert parse_catalog_info("a: [unclosed", "svc", BackstageConfig()) is None
    assert any("catalog-info" in r.message.lower() for r in caplog.records)


def test_empty_text_returns_none() -> None:
    assert parse_catalog_info("", "svc", BackstageConfig()) is None


async def test_provider_reads_first_existing_path_and_records_source() -> None:
    files = InMemoryRepoFiles({"catalog-info.yml": _doc("group:default/dlvr")})
    md = await BackstageProvider(BackstageConfig()).load(files, repo_name="svc")
    assert md is not None
    assert md.team == "dlvr"
    assert md.source_files == ["catalog-info.yml"]


async def test_provider_returns_none_when_no_file() -> None:
    md = await BackstageProvider(BackstageConfig()).load(InMemoryRepoFiles({}), repo_name="svc")
    assert md is None
