"""load_manifest: provider coalescing, config mining, placeholder drop, conventions."""

from __future__ import annotations

from bifrost.cli.config_sections import BackstageConfig, ConventionsConfig, ManifestConfig
from bifrost.manifest.files import InMemoryRepoFiles
from bifrost.manifest.loader import load_manifest
from bifrost.manifest.providers.none import NoneProvider
from bifrost.manifest.providers.registry import build_providers

CATALOG = """
kind: Component
metadata:
  name: svc
  description: From backstage
spec:
  lifecycle: production
  owner: group:default/dlvr
"""


def _cfg(*providers: str, **kw) -> ManifestConfig:
    return ManifestConfig(providers=list(providers), **kw)


async def test_first_provider_wins_per_field_and_gaps_are_filled() -> None:
    files = InMemoryRepoFiles({"catalog-info.yaml": CATALOG, "CODEOWNERS": "* @acme/plat\n"})
    md = await load_manifest(
        files, repo_name="svc", cfg=_cfg("backstage", "codeowners"), conventions=ConventionsConfig()
    )
    assert md.team == "dlvr"
    assert md.description == "From backstage"
    assert md.owners == ["plat"]
    assert md.providers == ["backstage", "codeowners"]
    assert md.source_files == ["catalog-info.yaml", "CODEOWNERS"]


async def test_order_decides_conflicts() -> None:
    files = InMemoryRepoFiles({"catalog-info.yaml": CATALOG, "CODEOWNERS": "* @acme/plat\n"})
    md = await load_manifest(
        files, repo_name="svc", cfg=_cfg("codeowners", "backstage"), conventions=ConventionsConfig()
    )
    assert md.team == "plat"
    assert md.description == "From backstage"
    assert md.lifecycle == "production"


async def test_codeowners_only_fallback_still_mines_config() -> None:
    files = InMemoryRepoFiles(
        {
            "CODEOWNERS": "* @acme/dlvr\n",
            "config/us.env": "PEER_HOST=peer.us.acme.internal\nJOBS_QUEUE=jobs-us\n",
            "config/eu.env": "PEER_HOST=peer.eu.acme.internal\nJOBS_QUEUE=jobs-eu\n",
        }
    )
    md = await load_manifest(
        files, repo_name="svc", cfg=_cfg("codeowners"), conventions=ConventionsConfig()
    )
    assert md.team == "dlvr"
    assert md.name == "svc"
    assert md.downstream_services == {"peer": "peer.eu.acme.internal"}
    assert md.topic_values == {"jobs": {"jobs-us", "jobs-eu"}}
    assert md.jira_project_key == "DLVR"
    assert "config/us.env" in md.source_files


async def test_no_files_at_all_gives_name_only_manifest() -> None:
    md = await load_manifest(
        InMemoryRepoFiles({}),
        repo_name="svc",
        cfg=_cfg("backstage", "codeowners"),
        conventions=ConventionsConfig(),
    )
    assert md.name == "svc"
    assert md.team is None
    assert md.providers == []
    assert md.source_files == []


async def test_none_provider_yields_empty_manifest_without_mining() -> None:
    files = InMemoryRepoFiles(
        {"CODEOWNERS": "* @acme/dlvr\n", "config/us.env": "PEER_HOST=peer.example.com\n"}
    )
    md = await load_manifest(
        files,
        repo_name="svc",
        cfg=_cfg("codeowners"),
        conventions=ConventionsConfig(),
        providers=[NoneProvider()],
    )
    assert md.name == "svc"
    assert md.team is None
    assert md.downstream_services == {}
    assert md.jira_project_key is None
    assert md.source_files == []


async def test_placeholder_team_dropped_and_conventions_not_derived() -> None:
    files = InMemoryRepoFiles({"CODEOWNERS": "* @acme/your-squad-here\n"})
    md = await load_manifest(
        files, repo_name="svc", cfg=_cfg("codeowners"), conventions=ConventionsConfig()
    )
    assert md.team is None
    assert md.jira_project_key is None
    assert md.slack_channel is None


async def test_configured_placeholder_teams_dropped() -> None:
    files = InMemoryRepoFiles({"CODEOWNERS": "* @acme/legacy\n"})
    md = await load_manifest(
        files,
        repo_name="svc",
        cfg=_cfg("codeowners"),
        conventions=ConventionsConfig(placeholder_teams=["Legacy"]),
    )
    assert md.team is None


async def test_remote_files_not_mined_unless_enabled() -> None:
    class RemoteFiles(InMemoryRepoFiles):
        is_remote = True

    data = {"CODEOWNERS": "* @acme/dlvr\n", "config/us.env": "PEER_HOST=peer.example.com\n"}
    off = await load_manifest(
        RemoteFiles(data), repo_name="svc", cfg=_cfg("codeowners"), conventions=ConventionsConfig()
    )
    assert off.downstream_services == {}
    on = await load_manifest(
        RemoteFiles(data),
        repo_name="svc",
        cfg=_cfg("codeowners", remote_config_globs=True),
        conventions=ConventionsConfig(),
    )
    assert on.downstream_services == {"peer": "peer.example.com"}


async def test_declared_dependencies_and_annotation_map_flow_through_registry() -> None:
    cfg = ManifestConfig(
        providers=["backstage"],
        backstage=BackstageConfig(annotation_map={"a/tier": "tier"}),
    )
    text = (
        CATALOG.replace("metadata:", "metadata:\n  annotations:\n    a/tier: '1'")
        + "  dependsOn: [component:other]\n"
    )
    md = await load_manifest(
        InMemoryRepoFiles({"catalog-info.yaml": text}),
        repo_name="svc",
        cfg=cfg,
        conventions=ConventionsConfig(),
    )
    assert md.tier == "1"
    assert md.declared_dependencies == ["other"]


def test_build_providers_follows_configured_order() -> None:
    names = [p.name for p in build_providers(ManifestConfig(providers=["codeowners", "backstage"]))]
    assert names == ["codeowners", "backstage"]
