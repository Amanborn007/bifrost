"""Ownership lookup.

GET  /api/v1/ownership?q={query}
  Instant DB search (<200ms, no LLM). Matches repo name, squad, description.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from bifrost.api.deps import DB
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.storage.models import OrgRepo, Repo, RepoFreshness, RepoOwnership

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ownership", tags=["ownership"])

# Patterns that indicate an ownership intent question.
_OWNERSHIP_RE = re.compile(
    r"""
    (?:
        who\s+(?:owns?|maintains?|is\s+responsible\s+for|works?\s+on|runs?)
        |what\s+(?:team|squad|group)\s+(?:owns?|maintains?|is\s+responsible\s+for|works?\s+on)
        |which\s+(?:team|squad|group)\s+(?:owns?|maintains?|is\s+responsible\s+for)
        |ownership\s+of
        |owner\s+of
        |point\s+of\s+contact\s+for
        |who\s+do\s+I\s+(?:contact|talk\s+to|ping)\s+about
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_ownership_question(text: str) -> bool:
    """Return True if the text looks like an ownership lookup question."""
    return bool(_OWNERSHIP_RE.search(text))


class OwnershipResult(BaseModel):
    repo_name: str
    squad: str | None
    slack_channel: str | None
    primary_contact: str | None  # from RepoOwnership.jira_project_key or raw_manifest
    last_commit_at: str | None
    description: str | None
    service_tier: str | None
    jira_project_key: str | None
    confluence_space_key: str | None
    url: str | None
    source: str  # "registered" | "catalog"


class OwnershipResponse(BaseModel):
    results: list[OwnershipResult]
    query: str
    total: int


@router.get("", response_model=BifrostEnvelope[OwnershipResponse])
async def lookup_ownership(
    session: DB,
    q: str = Query(
        ..., min_length=1, max_length=200, description="Repo name, service name, or team name"
    ),
) -> BifrostEnvelope[OwnershipResponse]:
    """Instant ownership lookup — searches registered repos + org catalog.

    Priority order:
      1. Registered repos with RepoOwnership rows (most complete data)
      2. OrgRepo catalog rows (org-wide coverage, manifest enriched)

    No LLM. Pure DB. Target: <200ms.
    """
    like = f"%{q.lower()}%"

    # ── 1. Registered repos + ownership metadata ─────────────────────────
    # Join Repo → RepoOwnership → RepoFreshness (for last_commit).
    # Match on repo.name, ownership.name, or ownership.squad.
    reg_stmt = (
        select(Repo, RepoOwnership, RepoFreshness)
        .outerjoin(RepoOwnership, RepoOwnership.repo_id == Repo.id)
        .outerjoin(RepoFreshness, RepoFreshness.repo_id == Repo.id)
        .where(
            or_(
                func.lower(Repo.name).like(like),
                func.lower(func.coalesce(RepoOwnership.name, "")).like(like),
                func.lower(func.coalesce(RepoOwnership.squad, "")).like(like),
                func.lower(func.coalesce(Repo.description, "")).like(like),
            )
        )
        .limit(10)
    )
    reg_rows = (await session.execute(reg_stmt)).all()

    results: list[OwnershipResult] = []
    seen_names: set[str] = set()

    for repo, ownership, freshness in reg_rows:
        # Extract primary_contact: prefer raw_manifest contact field, then jira key.
        primary_contact: str | None = None
        if ownership and ownership.raw_manifest:
            raw = ownership.raw_manifest
            # Contact fields written by ``manifest_to_ownership_values``
            primary_contact = (
                raw.get("contact") or raw.get("owner") or raw.get("maintainer") or raw.get("oncall")
            )
            if isinstance(primary_contact, list):
                primary_contact = ", ".join(str(x) for x in primary_contact)
            elif primary_contact is not None:
                primary_contact = str(primary_contact)

        last_commit_at: str | None = None
        if freshness and freshness.last_indexed_at:
            last_commit_at = freshness.last_indexed_at.isoformat()

        seen_names.add(repo.name.lower())
        results.append(
            OwnershipResult(
                repo_name=repo.name,
                squad=ownership.squad if ownership else None,
                slack_channel=ownership.slack_channel if ownership else None,
                primary_contact=primary_contact
                or (ownership.jira_project_key if ownership else None),
                last_commit_at=last_commit_at,
                description=repo.description,
                service_tier=ownership.service_tier if ownership else None,
                jira_project_key=ownership.jira_project_key if ownership else None,
                confluence_space_key=ownership.confluence_space_key if ownership else None,
                url=repo.url,
                source="registered",
            )
        )

    # ── 2. Org-wide catalog (OrgRepo) — fill gaps not covered by registered ──
    # Skip repos already found via the registered path.
    cat_stmt = (
        select(OrgRepo)
        .where(
            or_(
                func.lower(OrgRepo.name).like(like),
                func.lower(func.coalesce(OrgRepo.squad, "")).like(like),
                func.lower(func.coalesce(OrgRepo.description, "")).like(like),
            )
        )
        .where(OrgRepo.archived.is_(False))
        .order_by(OrgRepo.pushed_at.desc().nullslast())
        .limit(10)
    )
    cat_rows = (await session.execute(cat_stmt)).scalars().all()

    for org_repo in cat_rows:
        if org_repo.name.lower() in seen_names:
            continue
        last_commit_at_str: str | None = (
            org_repo.pushed_at.isoformat() if org_repo.pushed_at else None
        )
        results.append(
            OwnershipResult(
                repo_name=org_repo.name,
                squad=org_repo.squad,
                slack_channel=None,  # not available in org catalog
                primary_contact=None,
                last_commit_at=last_commit_at_str,
                description=org_repo.description,
                service_tier=org_repo.service_tier,
                jira_project_key=None,
                confluence_space_key=None,
                url=org_repo.url,
                source="catalog",
            )
        )

    # Registered results are already higher quality — keep them first.
    # Within catalog results, push squad-less repos to the bottom.
    catalog_results = [r for r in results if r.source == "catalog"]
    catalog_results.sort(key=lambda r: (r.squad is None, r.repo_name))
    registered_results = [r for r in results if r.source == "registered"]
    final = registered_results + catalog_results

    return BifrostEnvelope(
        data=OwnershipResponse(
            results=final[:15],
            query=q,
            total=len(final),
        )
    )
