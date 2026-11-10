"""generic_yaml provider: maps configured YAML paths onto manifest fields and mined config."""

from __future__ import annotations

import pytest

from bifrost.cli.config_sections import ConventionsConfig, GenericYamlConfig
from bifrost.manifest.conventions import apply_conventions
from bifrost.manifest.files import InMemoryRepoFiles
from bifrost.manifest.providers.generic_yaml import GenericYamlProvider, parse_generic_yaml

MANIFEST = """
name: orders-api
team: DLVR
tier: T1
classification: D3
region: eu
primary: true
settings:
  default:
    WEBHOOK_API_HOST: webhook-api.prod.example.com
    STOCK_URL: https://stock.example.com/v1
    API_BASE_URL: https://public.example.com
    DELIVERY_TOPIC: delivery-events
    DB_URL: jdbc:postgresql://db.example.com:5432/orders?ssl=true
    MAX_RETRIES: 3
  prod:
    WEBHOOK_API_HOST: other.example.com
    NOTIFY_QUEUE: notify-q
endpoints:
  prod:
    INVENTORY_SERVICE: inventory.example.com
"""

MANIFEST_CFG = GenericYamlConfig(
    path="service.yml",
    fields={
        "name": "name",
        "team": "team",
        "tier": "tier",
        "data_classification": "classification",
        "region": "region",
        "is_primary": "primary",
    },
    config_blocks=["settings.*", "endpoints.*"],
)


def test_parses_expected_manifest_fields() -> None:
    new = parse_generic_yaml(MANIFEST, MANIFEST_CFG)
    assert new is not None
    apply_conventions(new, ConventionsConfig())

    assert new.name == "orders-api"
    assert new.team == "DLVR"
    assert new.tier == "T1"
    assert new.data_classification == "D3"
    assert new.region == "eu"
    assert new.is_primary is True
    assert new.downstream_services == {
        "webhook_api": "webhook-api.prod.example.com",
        "stock": "https://stock.example.com/v1",
        "inventory": "inventory.example.com",
    }
    assert new.messaging_topics == {"delivery": "delivery-events", "notify": "notify-q"}
    assert new.database_names == ["orders"]
    assert new.jira_project_key == "DLVR"
    assert new.confluence_space_key == "DLVR"
    assert new.slack_channel == "#dlvr"


def test_mined_values_and_provider_name() -> None:
    new = parse_generic_yaml(MANIFEST, MANIFEST_CFG)
    assert new is not None
    assert new.downstream_services == {
        "webhook_api": "webhook-api.prod.example.com",
        "stock": "https://stock.example.com/v1",
        "inventory": "inventory.example.com",
    }
    assert new.messaging_topics == {"delivery": "delivery-events", "notify": "notify-q"}
    assert new.database_names == ["orders"]
    assert new.providers == ["generic_yaml"]


def test_config_blocks_pattern_limits_what_is_mined() -> None:
    cfg = GenericYamlConfig(path="service.yml", fields={}, config_blocks=["settings.default"])
    new = parse_generic_yaml(MANIFEST, cfg)
    assert new is not None
    assert new.downstream_services.keys() == {"webhook_api", "stock"}
    assert "notify" not in new.messaging_topics


def test_dotted_field_paths() -> None:
    text = "service:\n  meta:\n    owner: dlvr\n    tier: 2\n"
    cfg = GenericYamlConfig(
        path="x.yaml", fields={"team": "service.meta.owner", "tier": "service.meta.tier"}
    )
    new = parse_generic_yaml(text, cfg)
    assert new is not None
    assert new.team == "dlvr"
    assert new.tier == "2"


def test_missing_paths_are_skipped() -> None:
    cfg = GenericYamlConfig(path="x.yaml", fields={"team": "no.such.path"})
    new = parse_generic_yaml("a: 1\n", cfg)
    assert new is None or new.team is None


def test_list_field_targets() -> None:
    text = "owners: [alice, bob]\ndeps: [a, b]\n"
    cfg = GenericYamlConfig(
        path="x.yaml", fields={"owners": "owners", "declared_dependencies": "deps"}
    )
    new = parse_generic_yaml(text, cfg)
    assert new is not None
    assert new.owners == ["alice", "bob"]
    assert new.declared_dependencies == ["a", "b"]


def test_unknown_field_target_raises() -> None:
    cfg = GenericYamlConfig(path="x.yaml", fields={"bogus": "a"})
    with pytest.raises(ValueError, match="bogus"):
        parse_generic_yaml("a: 1\n", cfg)


def test_malformed_and_non_mapping_yaml_return_none() -> None:
    assert parse_generic_yaml("a: [unclosed", MANIFEST_CFG) is None
    assert parse_generic_yaml("- just\n- a list\n", MANIFEST_CFG) is None
    assert parse_generic_yaml("", MANIFEST_CFG) is None


async def test_provider_reads_configured_path() -> None:
    files = InMemoryRepoFiles({"service.yml": MANIFEST})
    md = await GenericYamlProvider(MANIFEST_CFG).load(files, repo_name="orders-api")
    assert md is not None
    assert md.team == "DLVR"
    assert md.source_files == ["service.yml"]


async def test_provider_none_when_file_absent() -> None:
    assert (
        await GenericYamlProvider(MANIFEST_CFG).load(InMemoryRepoFiles({}), repo_name="x") is None
    )
