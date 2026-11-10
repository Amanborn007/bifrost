"""plan_service_graph: pure graph planning, golden over examples/acmeco and synthetic cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from bifrost.cli.config_sections import ConventionsConfig, ManifestConfig
from bifrost.manifest.files import LocalRepoFiles
from bifrost.manifest.graph_plan import GraphPlan, plan_service_graph
from bifrost.manifest.loader import load_manifest
from bifrost.manifest.model import ServiceManifest

ACME = Path(__file__).resolve().parents[1] / "examples" / "acmeco"
REPOS = ["delivery-gateway", "dispatcher", "signing-keys", "usage-metering", "webhook-api"]


def _urn(repo: str) -> str:
    return f"urn:bifrost:service:{repo}"


async def _load_all(*providers: str) -> dict[str, ServiceManifest]:
    cfg = ManifestConfig(providers=list(providers))
    out: dict[str, ServiceManifest] = {}
    for repo in REPOS:
        files = LocalRepoFiles(ACME / repo, max_bytes=cfg.max_file_bytes)
        out[repo] = await load_manifest(
            files, repo_name=repo, cfg=cfg, conventions=ConventionsConfig()
        )
    return out


def _edges(plan: GraphPlan, rel: str) -> set[tuple[str, str]]:
    return {(e.source_urn, e.target_urn) for e in plan.edges if e.rel_type == rel}


async def test_acmeco_services_domains_and_teams() -> None:
    plan = plan_service_graph(await _load_all("backstage", "codeowners"))
    assert sorted(s.urn for s in plan.services) == [_urn(r) for r in REPOS]
    teams = {s.urn: s.team for s in plan.services}
    assert teams[_urn("webhook-api")] == "dlvr"
    assert teams[_urn("dispatcher")] == "dlvr"
    assert teams[_urn("delivery-gateway")] == "dlvr"
    assert teams[_urn("signing-keys")] == "plat"
    assert teams[_urn("usage-metering")] == "plat"
    assert [(d.name, d.urn) for d in plan.domains] == [
        ("dlvr", "urn:bifrost:domain:dlvr"),
        ("plat", "urn:bifrost:domain:plat"),
    ]
    assert plan.unresolved == []


async def test_acmeco_conventions_on_manifests() -> None:
    manifests = await _load_all("backstage", "codeowners")
    assert {m.jira_project_key for r, m in manifests.items() if m.team == "dlvr"} == {"DLVR"}
    assert {m.jira_project_key for r, m in manifests.items() if m.team == "plat"} == {"PLAT"}
    assert manifests["dispatcher"].slack_channel == "#dlvr"
    assert manifests["dispatcher"].database_names == ["subscriptions"]


async def test_acmeco_edges() -> None:
    plan = plan_service_graph(await _load_all("backstage", "codeowners"))
    assert _edges(plan, "depends_on") == {
        (_urn("webhook-api"), _urn("dispatcher")),
        (_urn("dispatcher"), _urn("delivery-gateway")),
        (_urn("dispatcher"), _urn("usage-metering")),
        (_urn("delivery-gateway"), _urn("signing-keys")),
    }
    assert _edges(plan, "calls_service") == {
        (_urn("dispatcher"), _urn("delivery-gateway")),
        (_urn("delivery-gateway"), _urn("signing-keys")),
    }
    assert _edges(plan, "shares_topic") == {
        (_urn("dispatcher"), _urn("webhook-api")),
        (_urn("dispatcher"), _urn("usage-metering")),
    }
    assert _edges(plan, "publishes_to") == set()


async def test_acmeco_confidences() -> None:
    plan = plan_service_graph(await _load_all("backstage", "codeowners"))
    conf = {e.rel_type: e.confidence for e in plan.edges}
    assert conf == {"depends_on": 0.9, "calls_service": 0.88, "shares_topic": 0.8}


async def test_codeowners_only_still_yields_calls_and_topic_edges() -> None:
    plan = plan_service_graph(await _load_all("codeowners"))
    assert _edges(plan, "depends_on") == set()
    assert (_urn("dispatcher"), _urn("delivery-gateway")) in _edges(plan, "calls_service")
    assert (_urn("delivery-gateway"), _urn("signing-keys")) in _edges(plan, "calls_service")
    assert (_urn("dispatcher"), _urn("webhook-api")) in _edges(plan, "shares_topic")
    assert {s.urn: s.team for s in plan.services}[_urn("signing-keys")] == "plat"


async def test_plan_is_deterministic() -> None:
    a = plan_service_graph(await _load_all("backstage", "codeowners"))
    b = plan_service_graph(
        dict(reversed(list((await _load_all("backstage", "codeowners")).items())))
    )
    assert a == b


def _m(**kw) -> ServiceManifest:
    return ServiceManifest(**kw)


def test_unresolved_targets_are_listed() -> None:
    plan = plan_service_graph(
        {
            "a": _m(
                name="a",
                declared_dependencies=["ghost"],
                downstream_services={"phantom": "phantom.example.com"},
            )
        }
    )
    assert plan.edges == []
    refs = {(u.source_repo, u.target, u.rel_type) for u in plan.unresolved}
    assert refs == {("a", "ghost", "depends_on"), ("a", "phantom", "calls_service")}


def test_no_self_edges() -> None:
    plan = plan_service_graph(
        {
            "a": _m(
                declared_dependencies=["a"],
                downstream_services={"a": "a.example.com"},
                messaging_topics={"a": "t"},
            )
        }
    )
    assert plan.edges == []


def test_host_resolves_by_subdomain_when_name_does_not_match() -> None:
    plan = plan_service_graph(
        {
            "caller": _m(downstream_services={"gw": "https://billing-api.prod.example.com/x"}),
            "billing-api": _m(),
        }
    )
    edge = next(e for e in plan.edges if e.rel_type == "calls_service")
    assert (edge.source_urn, edge.target_urn) == (_urn("caller"), _urn("billing-api"))
    assert "host_subdomain" in edge.method


def test_name_normalisation_matches_underscore_and_hyphen() -> None:
    plan = plan_service_graph(
        {
            "caller": _m(downstream_services={"webhook_api": "x.example.com"}),
            "webhook-api": _m(),
        }
    )
    assert _edges(plan, "calls_service") == {(_urn("caller"), _urn("webhook-api"))}


def test_publishes_to_when_topic_prefix_names_a_service() -> None:
    plan = plan_service_graph(
        {
            "producer": _m(messaging_topics={"inventory": "inventory-events"}),
            "inventory": _m(),
        }
    )
    assert _edges(plan, "publishes_to") == {(_urn("producer"), _urn("inventory"))}
    assert next(e for e in plan.edges if e.rel_type == "publishes_to").confidence == 0.85


def test_shares_topic_needs_two_repos_and_is_one_edge_per_sorted_pair() -> None:
    plan = plan_service_graph(
        {
            "b": _m(topic_values={"x": {"t1", "t2"}}),
            "a": _m(topic_values={"x": {"t1", "t2"}}),
            "c": _m(topic_values={"y": {"t1"}}),
            "solo": _m(topic_values={"z": {"only-me"}}),
        }
    )
    assert sorted(_edges(plan, "shares_topic")) == [
        (_urn("a"), _urn("b")),
        (_urn("a"), _urn("c")),
        (_urn("b"), _urn("c")),
    ]
    assert len([e for e in plan.edges if e.rel_type == "shares_topic"]) == 3


def test_duplicate_edges_collapsed() -> None:
    plan = plan_service_graph(
        {
            "a": _m(declared_dependencies=["b", "b"], downstream_services={"b": "b.example.com"}),
            "b": _m(),
        }
    )
    assert len([e for e in plan.edges if e.rel_type == "depends_on"]) == 1


def test_placeholder_team_does_not_create_domain() -> None:
    plan = plan_service_graph({"a": _m(team="your-squad-here"), "b": _m(team="Core")})
    assert [d.name for d in plan.domains] == ["Core"]
    assert [s.domain_urn for s in sorted(plan.services, key=lambda s: s.urn)] == [
        None,
        "urn:bifrost:domain:core",
    ]


@pytest.mark.parametrize("repo", REPOS)
def test_service_urn_format_is_frozen(repo: str) -> None:
    plan = plan_service_graph({repo: _m()})
    assert plan.services[0].urn == f"urn:bifrost:service:{repo}"
