"""Pure planning of the org service graph from per-repo manifests (no DB, no I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from bifrost.manifest.conventions import is_placeholder_team
from bifrost.manifest.model import ServiceManifest

REL_DEPENDS_ON = "depends_on"
REL_CALLS_SERVICE = "calls_service"
REL_PUBLISHES_TO = "publishes_to"
REL_SHARES_TOPIC = "shares_topic"

CONF_DEPENDS_ON = 0.9
CONF_CALLS_SERVICE = 0.88
CONF_PUBLISHES_TO = 0.85
CONF_SHARES_TOPIC = 0.8


def service_urn(repo: str) -> str:
    return f"urn:bifrost:service:{repo}"


def domain_urn(team: str) -> str:
    return f"urn:bifrost:domain:{team.lower()}"


@dataclass(frozen=True)
class PlannedService:
    repo: str
    urn: str
    name: str
    team: str | None
    domain_urn: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedDomain:
    name: str
    urn: str


@dataclass(frozen=True)
class PlannedEdge:
    source_urn: str
    target_urn: str
    rel_type: str
    confidence: float
    method: str
    matched_on: str


@dataclass(frozen=True)
class UnresolvedRef:
    source_repo: str
    target: str
    rel_type: str


@dataclass
class GraphPlan:
    services: list[PlannedService] = field(default_factory=list)
    edges: list[PlannedEdge] = field(default_factory=list)
    domains: list[PlannedDomain] = field(default_factory=list)
    unresolved: list[UnresolvedRef] = field(default_factory=list)


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _service_metadata(repo: str, md: ServiceManifest) -> dict[str, Any]:
    return {
        "repo_name": repo,
        "team": md.team,
        "owners": list(md.owners),
        "description": md.description,
        "tier": md.tier,
        "lifecycle": md.lifecycle,
        "system": md.system,
        "region": md.region,
        "data_classification": md.data_classification,
        "is_primary": md.is_primary,
        "compliance": list(md.compliance),
        "jira_project_key": md.jira_project_key,
        "confluence_space_key": md.confluence_space_key,
        "slack_channel": md.slack_channel,
        "database_names": list(md.database_names),
        "downstream_service_count": len(md.downstream_services),
        "topic_count": len(md.messaging_topics),
        "providers": list(md.providers),
        "source_files": list(md.source_files),
    }


def plan_service_graph(manifests: Mapping[str, ServiceManifest]) -> GraphPlan:
    plan = GraphPlan()
    repos = sorted(manifests)

    domains: dict[str, PlannedDomain] = {}
    for repo in repos:
        md = manifests[repo]
        d_urn = None
        if md.team and not is_placeholder_team(md.team):
            key = md.team.lower()
            if key not in domains:
                domains[key] = PlannedDomain(name=md.team, urn=domain_urn(md.team))
            d_urn = domains[key].urn
        plan.services.append(
            PlannedService(
                repo=repo,
                urn=service_urn(repo),
                name=md.name or repo,
                team=md.team,
                domain_urn=d_urn,
                metadata=_service_metadata(repo, md),
            )
        )
    plan.services.sort(key=lambda s: s.urn)
    plan.domains = sorted(domains.values(), key=lambda d: d.name.lower())

    index: dict[str, str] = {_norm(repo): repo for repo in repos}
    for repo in repos:
        alias = _norm(manifests[repo].name or "")
        if alias:
            index.setdefault(alias, repo)

    seen: set[tuple[str, str, str]] = set()

    def add(src: str, dst: str, rel: str, conf: float, method: str, matched_on: str) -> None:
        key = (src, dst, rel)
        if src == dst or key in seen:
            return
        seen.add(key)
        plan.edges.append(
            PlannedEdge(service_urn(src), service_urn(dst), rel, conf, method, matched_on)
        )

    for repo in repos:
        md = manifests[repo]

        for dep in md.declared_dependencies:
            target = index.get(_norm(dep))
            if target is None:
                plan.unresolved.append(UnresolvedRef(repo, dep, REL_DEPENDS_ON))
                continue
            add(repo, target, REL_DEPENDS_ON, CONF_DEPENDS_ON, "declared_dependency", dep)

        for name in sorted(md.downstream_services):
            host = md.downstream_services[name]
            target = index.get(_norm(name))
            via = "name_match"
            if target is None:
                target = index.get(_norm(host.split("//")[-1].split(".")[0]))
                via = "host_subdomain_match"
            if target is None:
                plan.unresolved.append(UnresolvedRef(repo, name, REL_CALLS_SERVICE))
                continue
            add(
                repo,
                target,
                REL_CALLS_SERVICE,
                CONF_CALLS_SERVICE,
                f"declared_host:{via}",
                f"{name} -> {host[:80]}",
            )

        for name in sorted(md.messaging_topics):
            target = index.get(_norm(name))
            if target is not None:
                add(
                    repo,
                    target,
                    REL_PUBLISHES_TO,
                    CONF_PUBLISHES_TO,
                    "declared_topic",
                    f"{name} -> {md.messaging_topics[name][:80]}",
                )

    by_value: dict[str, set[str]] = {}
    for repo in repos:
        for values in manifests[repo].topic_values.values():
            for value in values:
                by_value.setdefault(value, set()).add(repo)
    for value in sorted(by_value):
        sharers = sorted(by_value[value])
        for i, a in enumerate(sharers):
            for b in sharers[i + 1 :]:
                add(a, b, REL_SHARES_TOPIC, CONF_SHARES_TOPIC, "shared_topic_value", value[:80])
    return plan
