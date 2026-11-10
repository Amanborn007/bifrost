"""Load each registered repo's manifest and persist it as ``repo_ownership``.

``sync_repo_ownership`` and ``sync_all_ownership`` do the loading,
``resolve_repo_files`` picks where a repo's files are read from, and
``manifest_to_ownership_values`` is the only ServiceManifest to RepoOwnership
mapping. A repo with no manifest still gets a row (``source='missing'``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.core.local_roots import local_path_allowed
from bifrost.manifest.files import (
    GhError,
    GitHubRepoFiles,
    LocalRepoFiles,
    RepoFileError,
    RepoFiles,
)
from bifrost.manifest.loader import load_manifest
from bifrost.manifest.model import ServiceManifest
from bifrost.research.jira_discovery import discover_jira_project_key
from bifrost.storage.models import Repo, RepoOwnership

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    repo_id: str
    repo_name: str
    status: str  # "updated" | "no_manifest" | "error"
    squad: str | None = None
    downstream_count: int = 0
    error: str | None = None


def _local_dir_from_url(url: str) -> Path | None:
    if url.startswith("file://"):
        path = Path(unquote(urlparse(url).path))
    elif url.startswith("/"):
        path = Path(url)
    else:
        return None
    resolved = path.resolve()
    return resolved if resolved.is_dir() else None


def _github_owner_name(url: str, host: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.lower():
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1].removesuffix(".git")


def resolve_repo_files(
    url: str,
    local_clone_path: str | None,
    cfg: BifrostSettings,
) -> RepoFiles | None:
    """Where to read this repo's files: its clone, a local directory URL, or GitHub.

    Returns None when the URL is neither a permitted local directory nor a repo on
    ``sources.github.host``.
    """
    max_bytes = cfg.manifest.max_file_bytes
    if local_clone_path and Path(local_clone_path).is_dir():
        return LocalRepoFiles(local_clone_path, max_bytes=max_bytes)

    local = _local_dir_from_url(url)
    if local is not None:
        if not local_path_allowed(local, cfg):
            logger.warning("local repo %s is outside sources.local.allowed_roots; ignoring", local)
            return None
        return LocalRepoFiles(local, max_bytes=max_bytes)

    host = cfg.sources.github.host
    parsed = _github_owner_name(url, host)
    if parsed is None:
        return None
    owner, name = parsed
    return GitHubRepoFiles(owner, name, host=host, max_bytes=max_bytes)


_CONTACT_KEYS = frozenset({"contact", "maintainer", "oncall"})


def _contact_text(value: Any) -> str | None:
    items = value if isinstance(value, list) else [value]
    parts = [str(i).strip() for i in items if isinstance(i, (str, int, float)) and str(i).strip()]
    return ", ".join(parts) or None


def _contact_fields(md: ServiceManifest) -> dict[str, str]:
    """The ``contact`` / ``owner`` / ``maintainer`` / ``oncall`` keys the ownership lookups read."""
    found: dict[str, str] = {}
    for doc in md.raw.values():
        if not isinstance(doc, dict):
            continue
        meta = doc.get("metadata")
        annotations = meta.get("annotations") if isinstance(meta, dict) else None
        for source in (doc, annotations):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                name = str(key).rsplit("/", 1)[-1]
                text = _contact_text(value) if name in _CONTACT_KEYS else None
                if text:
                    found.setdefault(name, text)
    owner = ", ".join(md.owners) or md.team
    if owner:
        found.setdefault("owner", owner)
    if md.slack_channel and not found:
        found["contact"] = md.slack_channel
    return found


def manifest_to_ownership_values(repo_id: _uuid.UUID, md: ServiceManifest) -> dict[str, Any]:
    """Map a manifest onto the ``repo_ownership`` column names."""
    compliance_tags = sorted({c.strip().lower() for c in md.compliance if c.strip()})
    return {
        "repo_id": repo_id,
        "name": md.name,
        "squad": md.team,
        "service_tier": md.tier,
        "data_classification": md.data_classification,
        "compliance_tags": compliance_tags,
        "jira_project_key": md.jira_project_key,
        "confluence_space_key": md.confluence_space_key,
        "slack_channel": md.slack_channel,
        "downstream_services": md.downstream_services,
        "messaging_topics": md.messaging_topics,
        "database_names": md.database_names,
        "raw_manifest": {
            "providers": md.providers,
            "files": md.source_files,
            **_contact_fields(md),
        },
        "source": "+".join(md.providers) or "missing",
        "last_synced_at": datetime.now(),
    }


async def _upsert_ownership(
    session: AsyncSession,
    repo_id: _uuid.UUID,
    md: ServiceManifest,
) -> None:
    values = manifest_to_ownership_values(repo_id, md)
    stmt = pg_insert(RepoOwnership).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["repo_id"],
        set_={k: v for k, v in values.items() if k != "repo_id"},
    )
    await session.execute(stmt)


async def sync_repo_ownership(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repo_id: str,
    clone_path: str | None = None,
    settings: BifrostSettings | None = None,
) -> SyncResult:
    """Load + persist ownership for one repo.

    Files come from ``clone_path`` (or the repo's stored ``local_clone_path``),
    else from the repo URL (see ``resolve_repo_files``). When a clone is
    available, the Jira project key inferred from Jira browse-URL refs
    overrides the team-derived default.
    """
    settings = settings or get_settings()
    rid = _uuid.UUID(repo_id)
    async with session_factory() as session:
        repo = (await session.execute(select(Repo).where(Repo.id == rid))).scalar_one_or_none()
        if repo is None:
            return SyncResult(
                repo_id=repo_id,
                repo_name="?",
                status="error",
                error="repo not found",
            )

    effective_clone = clone_path or repo.local_clone_path
    files = resolve_repo_files(repo.url, effective_clone, settings)
    if files is None:
        return SyncResult(
            repo_id=str(repo.id),
            repo_name=repo.name,
            status="error",
            error=(
                f"no readable source for {repo.url}: not a permitted local directory "
                f"(sources.local.allowed_roots) or a repo on {settings.sources.github.host}"
            ),
        )

    try:
        md = await load_manifest(
            files,
            repo_name=repo.name,
            cfg=settings.manifest,
            conventions=settings.conventions,
        )
    except (GhError, RepoFileError, OSError) as exc:
        logger.warning("manifest load for %s failed: %s", repo.name, exc)
        return SyncResult(
            repo_id=str(repo.id),
            repo_name=repo.name,
            status="error",
            error=str(exc)[:200],
        )

    if effective_clone:
        discovered_key, _counts = await asyncio.to_thread(
            discover_jira_project_key, effective_clone, jira=settings.sources.jira
        )
        if discovered_key:
            logger.info(
                "jira_project_key for %s: derived=%s -> discovered=%s",
                repo.name,
                md.jira_project_key,
                discovered_key,
            )
            md.jira_project_key = discovered_key

    async with session_factory() as session:
        await _upsert_ownership(session, repo.id, md)
        await session.execute(
            update(Repo).where(Repo.id == repo.id).values(is_primary=md.is_primary),
        )
        await session.commit()

    return SyncResult(
        repo_id=str(repo.id),
        repo_name=repo.name,
        status="updated" if md.providers else "no_manifest",
        squad=md.team,
        downstream_count=len(md.downstream_services),
    )


async def sync_all_ownership(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    concurrency: int = 8,
    settings: BifrostSettings | None = None,
) -> list[SyncResult]:
    """Parallel refresh for every registered repo."""
    async with session_factory() as session:
        repo_rows = (await session.execute(select(Repo.id))).scalars().all()

    sem = asyncio.Semaphore(concurrency)

    async def _one(rid: _uuid.UUID) -> SyncResult:
        async with sem:
            return await sync_repo_ownership(
                session_factory=session_factory,
                repo_id=str(rid),
                settings=settings,
            )

    return await asyncio.gather(*(_one(r) for r in repo_rows))
