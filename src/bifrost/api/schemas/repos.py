"""Repo endpoint schemas per TRD §4.1."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class RegisterRepoRequest(BaseModel):
    name: str
    url: str
    domain_id: str | None = None
    description: str | None = None
    adapter_type: Literal["git", "github"] = "git"
    adapter_config: dict[str, str] | None = None


class RegisterRepoData(BaseModel):
    id: str
    urn: str
    adapter_config: dict[str, str] = {}
    status: Literal["registered"] = "registered"


class RepoSummary(BaseModel):
    id: str
    urn: str
    name: str
    url: str
    domain_id: str | None
    freshness_state: str
    index_status: str
    last_indexed_at: datetime | None


class RepoFreshness(BaseModel):
    state: str
    last_commit_indexed: str | None
    last_indexed_at: datetime | None
    pending_commits: int


class RepoStats(BaseModel):
    component_count: int
    flow_count: int
    dependency_count: int


class RepoDetail(BaseModel):
    id: str
    urn: str
    name: str
    url: str
    domain_id: str | None
    freshness: RepoFreshness
    stats: RepoStats


class DeregisterRepoData(BaseModel):
    id: str
    status: Literal["deregistered"] = "deregistered"


class TriggerExtractionRequest(BaseModel):
    force_full: bool = False
    budget_override: dict[str, object] | None = None
    max_files: int | None = None  # cap Phase D files; None = server default (500)
    # Selectively re-run specific phases. Useful after fixing a phase's
    # prompt/logic — re-runs the named phases AND their downstream dependents
    # (G depends on D/E/F; H/I depend on G) without redoing the expensive
    # earlier phases. Example: {"invalidate_phases": ["E"]} re-extracts
    # flows + relationships + knowledge index, keeps components.
    invalidate_phases: list[str] | None = None


class TriggerExtractionData(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"


class RepoCheckpointPhase(BaseModel):
    """Per-phase checkpoint state surfaced to the UI."""

    phase: str  # "A".."I"
    label: str  # human-friendly (e.g., "Triage", "Components")
    done: bool
    units_done: int  # for D/E: files processed so far


class RepoCheckpointStatus(BaseModel):
    """Checkpoint snapshot for a repo — powers the Resume/Invalidate UI."""

    repo_id: str
    completed_phases: list[str]
    resume_phase: str | None  # next phase that would run; None if all done
    last_updated: datetime | None
    phases: list[RepoCheckpointPhase]  # ordered A..I with per-phase detail
