"""Sync + query the org-wide GitHub catalog.

Uses the authenticated ``gh`` CLI (no API keys) to enumerate every repo in
the orgs named by ``sources.github.orgs`` on ``sources.github.host``, then
optionally backfills each with team/tier from its manifest. Chat's
skim-search queries this when the registered-repo scope is too thin to
answer a question.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.manifest.files import GhError, GitHubRepoFiles, RepoFileError, run_gh
from bifrost.manifest.loader import load_manifest
from bifrost.storage.models import OrgRepo, Repo

logger = logging.getLogger(__name__)

NO_ORGS_ERROR = "no sources.github.orgs configured"

_ORG_REPO_FIELDS = (
    ".[] | {name, full_name, html_url, description, language, default_branch, "
    "archived, fork, private, stargazers_count, pushed_at, topics}"
)
_BACKFILL_CAP = 400


@dataclass
class CatalogSyncResult:
    org: str
    total_fetched: int
    inserted: int
    updated: int
    manifest_hits: int
    errors: list[str]


async def _list_org_repos(org: str, host: str) -> list[dict[str, Any]]:
    """Enumerate ALL repos in an org via ``gh api --paginate``."""
    argv = ["api"]
    if host != "github.com":
        argv += ["--hostname", host]
    argv += [f"orgs/{org}/repos", "--paginate", "--jq", _ORG_REPO_FIELDS]
    text = await run_gh(argv, timeout=120.0)
    # gh --paginate + --jq emits one JSON object per line, not a JSON array.
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def sync_org_catalog(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    org: str | None = None,
    backfill: bool = True,
    concurrency: int = 8,
    settings: BifrostSettings | None = None,
) -> CatalogSyncResult:
    """Pull every repo of ``org`` (default: each configured org) into ``org_repos``.

    With ``backfill`` the manifest of each repo still missing a team is
    loaded to fill squad/tier/region, with bounded concurrency so GitHub
    isn't hammered.
    """
    settings = settings or get_settings()
    host = settings.sources.github.host
    orgs = [org] if org else list(settings.sources.github.orgs)
    if not orgs:
        return CatalogSyncResult(
            org="",
            total_fetched=0,
            inserted=0,
            updated=0,
            manifest_hits=0,
            errors=[NO_ORGS_ERROR],
        )

    errors: list[str] = []
    raw: list[dict[str, Any]] = []
    for name in orgs:
        try:
            raw.extend(await _list_org_repos(name, host))
        except (GhError, json.JSONDecodeError) as exc:
            errors.append(f"gh org listing failed for {name}: {exc}")
    label = ",".join(orgs)
    if not raw:
        return CatalogSyncResult(
            org=label, total_fetched=0, inserted=0, updated=0, manifest_hits=0, errors=errors
        )

    upserted = 0
    async with session_factory() as session:
        registered = (await session.execute(select(Repo.id, Repo.name))).all()
        reg_by_name: dict[str, _uuid.UUID] = {r.name.lower(): r.id for r in registered}

        for r in raw:
            full_name = r.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, name = full_name.split("/", 1)
            values = {
                "id": _uuid.uuid4(),
                "owner": owner,
                "name": name,
                "url": r.get("html_url") or f"https://{host}/{full_name}",
                "description": (r.get("description") or None),
                "primary_language": r.get("language") or None,
                "default_branch": r.get("default_branch") or None,
                "archived": bool(r.get("archived")),
                "is_fork": bool(r.get("fork")),
                "is_private": bool(r.get("private")),
                "stars": int(r.get("stargazers_count") or 0),
                "pushed_at": _parse_iso(str(r.get("pushed_at") or "")),
                "topics": list(r.get("topics") or []),
                "registered_repo_id": reg_by_name.get(name.lower()),
                "fetched_at": datetime.now(),
            }
            stmt = (
                pg_insert(OrgRepo)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["owner", "name"],
                    set_={k: v for k, v in values.items() if k not in ("id", "owner", "name")},
                )
            )
            await session.execute(stmt)
            upserted += 1
        await session.commit()

    hits = await _backfill_manifests(session_factory, settings, concurrency) if backfill else 0
    return CatalogSyncResult(
        org=label,
        total_fetched=len(raw),
        inserted=upserted,
        updated=0,
        manifest_hits=hits,
        errors=errors,
    )


async def _backfill_manifests(
    session_factory: async_sessionmaker[AsyncSession],
    settings: BifrostSettings,
    concurrency: int,
) -> int:
    """Fill squad/tier/region for catalog rows that have no team yet. Returns hits."""
    async with session_factory() as session:
        need = (
            await session.execute(
                select(OrgRepo.owner, OrgRepo.name)
                .where(OrgRepo.archived.is_(False))
                .where(OrgRepo.is_fork.is_(False))
                .where(OrgRepo.squad.is_(None))
            )
        ).all()

    host = settings.sources.github.host
    sem = asyncio.Semaphore(concurrency)

    async def _one(owner: str, name: str) -> tuple[str, str, dict[str, Any]]:
        async with sem:
            files = GitHubRepoFiles(
                owner, name, host=host, runner=run_gh, max_bytes=settings.manifest.max_file_bytes
            )
            try:
                md = await load_manifest(
                    files,
                    repo_name=name,
                    cfg=settings.manifest,
                    conventions=settings.conventions,
                )
            except (GhError, RepoFileError) as exc:
                logger.warning("manifest backfill for %s/%s failed: %s", owner, name, exc)
                return owner, name, {"manifest_error": f"manifest fetch failed: {exc}"[:200]}
            if not md.providers:
                return owner, name, {"manifest_error": "no_manifest"}
            return (
                owner,
                name,
                {
                    "squad": md.team,
                    "service_tier": md.tier,
                    "region": md.region,
                    "is_primary": md.is_primary,
                    "manifest_error": None,
                },
            )

    results = await asyncio.gather(*(_one(r.owner, r.name) for r in need[:_BACKFILL_CAP]))

    hits = 0
    async with session_factory() as session:
        for owner, name, fields in results:
            await session.execute(
                update(OrgRepo)
                .where(OrgRepo.owner == owner, OrgRepo.name == name)
                .values(manifest_fetched_at=datetime.now(), **fields)
            )
            if fields.get("squad"):
                hits += 1
        await session.commit()
    return hits


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
