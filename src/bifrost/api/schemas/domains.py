"""Domain endpoint schemas per TRD §4.2."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CreateDomainRequest(BaseModel):
    name: str
    description: str | None = None


class CreateDomainData(BaseModel):
    id: str
    urn: str


class DomainSummary(BaseModel):
    id: str
    name: str
    repo_count: int


class DomainRepoEntry(BaseModel):
    id: str
    name: str
    url: str
    freshness_state: str
    last_indexed_at: str | None = None
    entity_counts: dict[str, int] = {}
    latest_task_id: str | None = None
    latest_task_status: str | None = None


class DomainDetail(BaseModel):
    id: str
    name: str
    description: str | None
    repos: list[DomainRepoEntry]
    cross_repo_flow_count: int
    total_entities: int = 0
    fresh_count: int = 0
    stale_count: int = 0


class UpdateDomainRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class UpdateDomainData(BaseModel):
    id: str
    updated: Literal[True] = True


class DeleteDomainData(BaseModel):
    id: str
    status: Literal["deleted"] = "deleted"


class AddRepoDomainRequest(BaseModel):
    repo_id: str


class AddRepoDomainData(BaseModel):
    domain_id: str
    repo_id: str


class RemoveRepoDomainData(BaseModel):
    domain_id: str
    repo_id: str
    removed: Literal[True] = True


class DiscoverTeamReposRequest(BaseModel):
    """Optional team override — defaults to the domain's name."""

    team: str | None = None
    dry_run: bool = False
    trigger_extraction: bool = False


class TeamRepoEntry(BaseModel):
    full_name: str
    action: Literal["added", "linked", "already_linked", "error"]
    repo_id: str | None = None
    git_url: str
    description: str | None = None
    task_id: str | None = None
    error: str | None = None


class DiscoverTeamReposData(BaseModel):
    team: str
    dry_run: bool
    candidates_found: int
    providers: list[str] = []
    added: list[TeamRepoEntry] = []
    linked: list[TeamRepoEntry] = []
    already_linked: list[TeamRepoEntry] = []
    errors: list[TeamRepoEntry] = []
