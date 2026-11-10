"""The provider-neutral shape of one repo's service metadata."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

_SCALARS = (
    "name",
    "team",
    "description",
    "tier",
    "lifecycle",
    "system",
    "region",
    "data_classification",
    "jira_project_key",
    "confluence_space_key",
    "slack_channel",
)
_LISTS = (
    "owners",
    "compliance",
    "declared_dependencies",
    "database_names",
    "providers",
    "source_files",
)


@dataclass
class ServiceManifest:
    name: str | None = None
    team: str | None = None
    owners: list[str] = field(default_factory=list)
    description: str | None = None
    tier: str | None = None
    lifecycle: str | None = None
    system: str | None = None
    region: str | None = None
    data_classification: str | None = None
    is_primary: bool = False
    compliance: list[str] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    downstream_services: dict[str, str] = field(default_factory=dict)
    messaging_topics: dict[str, str] = field(default_factory=dict)
    topic_values: dict[str, set[str]] = field(default_factory=dict)
    database_names: list[str] = field(default_factory=list)
    jira_project_key: str | None = None
    confluence_space_key: str | None = None
    slack_channel: str | None = None
    providers: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when no field carries information (ignores provenance-only fields)."""
        provenance = {"providers", "source_files"}
        for f in fields(self):
            if f.name in provenance:
                continue
            if getattr(self, f.name):
                return False
        return True

    def merge_from(self, other: ServiceManifest) -> None:
        """First non-empty scalar wins; collections are unioned in order."""
        for attr in _SCALARS:
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        self.is_primary = self.is_primary or other.is_primary
        for attr in _LISTS:
            mine = getattr(self, attr)
            mine.extend(v for v in getattr(other, attr) if v not in mine)
        for key, value in other.downstream_services.items():
            self.downstream_services.setdefault(key, value)
        for key, value in other.messaging_topics.items():
            self.messaging_topics.setdefault(key, value)
        for key, values in other.topic_values.items():
            self.topic_values.setdefault(key, set()).update(values)
        for key, value in other.raw.items():
            self.raw.setdefault(key, value)
