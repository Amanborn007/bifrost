"""Find the repos a team owns: code-search for provider manifest files, then verify each manifest."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import ManifestConfig
from bifrost.errors import ConfigError, UpstreamError, ValidationError
from bifrost.manifest.files import (
    GhError,
    GhRunner,
    GitHubRepoFiles,
    RepoFileError,
    RepoFiles,
    run_gh,
)
from bifrost.manifest.loader import load_manifest

logger = logging.getLogger(__name__)

_SEARCH_JQ = ".items[].repository.full_name"
_SEARCH_TIMEOUT = 30.0

FilesFactory = Callable[[str, str, str], RepoFiles]


@dataclass(frozen=True)
class TeamRepoMatch:
    """A repo whose manifest names the requested team."""

    owner: str
    name: str
    full_name: str
    git_url: str
    description: str | None
    team: str
    providers: list[str]


def _search_filenames(cfg: ManifestConfig) -> list[str]:
    """Basenames of the files the enabled providers read, in provider order, de-duplicated."""
    paths: list[str] = []
    for provider in cfg.providers:
        if provider == "backstage":
            paths += cfg.backstage.paths
        elif provider == "codeowners":
            paths += cfg.codeowners.paths
        elif provider == "generic_yaml" and cfg.generic_yaml.path:
            paths.append(cfg.generic_yaml.path)
    names: list[str] = []
    for path in paths:
        name = PurePosixPath(path).name
        if name not in names:
            names.append(name)
    return names


def _api_argv(host: str, *args: str) -> list[str]:
    argv = ["api"]
    if host != "github.com":
        argv += ["--hostname", host]
    return [*argv, *args]


async def discover_team_repos(
    team: str,
    *,
    settings: BifrostSettings | None = None,
    runner: GhRunner = run_gh,
    files_factory: FilesFactory | None = None,
    search_limit: int = 100,
    concurrency: int = 8,
) -> list[TeamRepoMatch]:
    """Repos in the configured orgs whose manifest declares ``team`` (case-insensitive).

    Code search is over-inclusive, so each hit is confirmed by loading its manifest
    through the configured providers. Forks and archived repos are skipped.
    """
    settings = settings or get_settings()
    team = team.strip()
    if not team:
        raise ValidationError("team name must be non-empty")

    gh = settings.sources.github
    if not gh.orgs:
        raise ConfigError(
            "sources.github.orgs is empty; list the GitHub orgs to search in bifrost.yaml",
            http_status=400,
        )
    filenames = _search_filenames(settings.manifest)
    if not filenames:
        raise ConfigError(
            f"manifest.providers {settings.manifest.providers} read no searchable files; "
            "enable backstage, codeowners or generic_yaml",
            http_status=400,
        )

    host = gh.host
    sem = asyncio.Semaphore(concurrency)
    make_files: FilesFactory = files_factory or (
        lambda owner, name, h: GitHubRepoFiles(
            owner, name, host=h, runner=runner, max_bytes=settings.manifest.max_file_bytes
        )
    )

    async def _search(org: str, filename: str) -> list[str] | GhError:
        argv = _api_argv(
            host,
            "-X",
            "GET",
            "search/code",
            "-f",
            f"q={team} org:{org} filename:{filename}",
            "-f",
            f"per_page={search_limit}",
            "--jq",
            _SEARCH_JQ,
        )
        async with sem:
            try:
                out = await runner(argv, timeout=_SEARCH_TIMEOUT)
            except GhError as exc:
                logger.warning("code search failed for org %s filename %s: %s", org, filename, exc)
                return exc
        return [line.strip() for line in out.splitlines() if "/" in line]

    results = await asyncio.gather(*(_search(o, f) for o in gh.orgs for f in filenames))
    failures = [r for r in results if isinstance(r, GhError)]
    if len(failures) == len(results):
        raise UpstreamError(f"every gh code search failed; last error: {failures[-1]}")

    candidates: list[str] = []
    for found in results:
        if isinstance(found, GhError):
            continue
        for full_name in found:
            if full_name not in candidates:
                candidates.append(full_name)

    async def _verify(full_name: str) -> TeamRepoMatch | None:
        owner, _, name = full_name.partition("/")
        async with sem:
            try:
                md = await load_manifest(
                    make_files(owner, name, host),
                    repo_name=name,
                    cfg=settings.manifest,
                    conventions=settings.conventions,
                )
            except (GhError, RepoFileError) as exc:
                logger.warning("manifest load for %s failed: %s", full_name, exc)
                return None
            if not md.team or md.team.casefold() != team.casefold():
                return None
            try:
                meta = json.loads(await runner(_api_argv(host, f"repos/{full_name}")))
            except (GhError, json.JSONDecodeError) as exc:
                logger.warning("metadata fetch for %s failed: %s", full_name, exc)
                return None
        if meta.get("fork") or meta.get("archived"):
            return None
        return TeamRepoMatch(
            owner=owner,
            name=name,
            full_name=full_name,
            git_url=f"https://{host}/{owner}/{name}.git",
            description=meta.get("description"),
            team=md.team,
            providers=list(md.providers),
        )

    verified = await asyncio.gather(*(_verify(c) for c in candidates))
    return sorted((m for m in verified if m is not None), key=lambda m: m.full_name)
