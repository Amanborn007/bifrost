"""Domain Knowledge Hub — onboarding-friendly per-domain summary.

Use case: an engineer joining a team opens this page and
sees, in one screen:

  • The squad's services (deep-indexed repos with component counts)
  • Top contributors (by recent activity in the squad's repos)
  • Recent design docs (Drive crawler output)
  • Active initiatives (open Jira epics from the squad's project)
  • Where to ask (slack channel, jira project)
  • A starter "Discuss this domain" link that pre-loads scope

This is purely aggregative; no MCP calls, no LLM. Bifrost has all
the data — this surface just composes it nicely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class HubService:
    repo_id: str
    name: str
    description: str | None
    component_count: int = 0
    flow_count: int = 0


@dataclass
class HubContributor:
    email: str
    activity_count: int


@dataclass
class HubGDoc:
    title: str
    url: str
    last_modified: str | None
    repo: str | None


@dataclass
class HubJiraEpic:
    key: str
    title: str
    url: str
    status: str | None
    repo: str | None


@dataclass
class DomainHub:
    domain: str
    label: str
    slack_channels: list[str] = field(default_factory=list)
    jira_projects: list[str] = field(default_factory=list)
    services: list[HubService] = field(default_factory=list)
    top_contributors: list[HubContributor] = field(default_factory=list)
    recent_design_docs: list[HubGDoc] = field(default_factory=list)
    active_epics: list[HubJiraEpic] = field(default_factory=list)
    generated_at: str = ""


async def build_domain_hub(
    session: AsyncSession,
    *,
    squad: str,
    range_days: int = 30,
) -> DomainHub:
    """Assemble the hub for ``squad`` (case-insensitive match
    against ``RepoOwnership.squad``)."""
    since = datetime.utcnow() - timedelta(days=range_days)
    hub = DomainHub(
        domain=squad,
        label=f"{squad} squad",
        generated_at=datetime.now(UTC).isoformat(),
    )

    # Squad's repos + ownership rollups
    own_rows = (
        await session.execute(
            text(
                "SELECT repo_id::text, slack_channel, jira_project_key "
                "FROM repo_ownership "
                "WHERE LOWER(squad) = LOWER(:s)"
            ),
            {"s": squad},
        )
    ).all()
    repo_ids = [r[0] for r in own_rows]
    slack_channels = sorted({r[1] for r in own_rows if r[1]})
    jira_projects = sorted({r[2] for r in own_rows if r[2]})
    hub.slack_channels = slack_channels
    hub.jira_projects = jira_projects

    if not repo_ids:
        return hub

    # Services — repos with their component/flow counts
    svc_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT r.id::text AS id, r.name, r.description,
                       (SELECT COUNT(*) FROM entities e
                        WHERE e.scope_repo_id = r.id
                          AND e.entity_type = 'component') AS comps,
                       (SELECT COUNT(*) FROM entities e
                        WHERE e.scope_repo_id = r.id
                          AND e.entity_type = 'flow') AS flows
                FROM repos r
                WHERE r.id::text = ANY(:repos)
                ORDER BY comps DESC NULLS LAST, r.name
                LIMIT 30
                """
                ),
                {"repos": repo_ids},
            )
        )
        .mappings()
        .all()
    )
    hub.services = [
        HubService(
            repo_id=r["id"],
            name=r["name"],
            description=r.get("description"),
            component_count=int(r.get("comps") or 0),
            flow_count=int(r.get("flows") or 0),
        )
        for r in svc_rows
    ]

    # Top contributors
    rows = (
        await session.execute(
            text(
                """
                SELECT email, SUM(n) AS n FROM (
                    SELECT owner_email AS email, COUNT(*) AS n
                    FROM tasks
                    WHERE updated_at > :since
                      AND owner_email IS NOT NULL
                      AND owner_email <> ''
                    GROUP BY owner_email
                    UNION ALL
                    SELECT owner_email AS email, COUNT(*) AS n
                    FROM conversations
                    WHERE deleted_at IS NULL
                      AND updated_at > :since
                      AND owner_email IS NOT NULL
                      AND owner_email <> ''
                      AND CAST(scope_repo_ids AS jsonb) ?| :repos
                    GROUP BY owner_email
                ) t
                GROUP BY email
                ORDER BY n DESC
                LIMIT 8
                """
            ),
            {"since": since, "repos": repo_ids},
        )
    ).all()
    hub.top_contributors = [
        HubContributor(email=str(r[0]), activity_count=int(r[1] or 0)) for r in rows if r[0]
    ]

    # Recent design docs
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT g.title, g.url, g.last_modified, r.name AS repo_name
                FROM gdoc_pages g
                JOIN repos r ON r.id = g.repo_id
                WHERE g.repo_id::text = ANY(:repos)
                ORDER BY COALESCE(g.last_modified, g.indexed_at) DESC
                LIMIT 10
                """
                ),
                {"repos": repo_ids},
            )
        )
        .mappings()
        .all()
    )
    hub.recent_design_docs = [
        HubGDoc(
            title=r["title"],
            url=r["url"],
            last_modified=(r["last_modified"].isoformat() if r.get("last_modified") else None),
            repo=r.get("repo_name"),
        )
        for r in rows
    ]

    # Active Jira epics — heuristic: rows with issue_type='Epic' from
    # the squad's Jira project that aren't done. Falls back to any
    # in-progress issue when the cache lacks epic-typed rows.
    epic_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT j.issue_key, j.title, j.url, j.status, r.name AS repo_name
                FROM jira_issues j
                JOIN repos r ON r.id = j.repo_id
                WHERE j.repo_id::text = ANY(:repos)
                  AND LOWER(COALESCE(j.status, '')) NOT IN ('done','closed','resolved',
                                                             'cancelled','won''t do')
                ORDER BY
                  CASE WHEN LOWER(COALESCE(j.issue_type,'')) = 'epic' THEN 0 ELSE 1 END,
                  j.updated_at DESC
                LIMIT 10
                """
                ),
                {"repos": repo_ids},
            )
        )
        .mappings()
        .all()
    )
    hub.active_epics = [
        HubJiraEpic(
            key=r["issue_key"],
            title=r["title"],
            url=r["url"],
            status=r.get("status"),
            repo=r.get("repo_name"),
        )
        for r in epic_rows
    ]

    return hub
