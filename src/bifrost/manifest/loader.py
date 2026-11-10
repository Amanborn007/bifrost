"""Load one repo's neutral manifest from its files."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from bifrost.cli.config_sections import ConventionsConfig, ManifestConfig
from bifrost.manifest.conventions import apply_conventions, is_placeholder_team
from bifrost.manifest.env_miner import MinedDeps, mine_file
from bifrost.manifest.files import RepoFiles
from bifrost.manifest.model import ServiceManifest
from bifrost.manifest.providers.base import ManifestProvider
from bifrost.manifest.providers.registry import build_providers

logger = logging.getLogger(__name__)


async def load_manifest(
    files: RepoFiles,
    *,
    repo_name: str,
    cfg: ManifestConfig,
    conventions: ConventionsConfig,
    providers: Sequence[ManifestProvider] | None = None,
) -> ServiceManifest:
    chain = list(providers) if providers is not None else build_providers(cfg)
    if chain and all(p.name == "none" for p in chain):
        return ServiceManifest(name=repo_name)

    merged = ServiceManifest()
    for provider in chain:
        found = await provider.load(files, repo_name=repo_name)
        if found is not None and not found.is_empty:
            merged.merge_from(found)

    if not files.is_remote or cfg.remote_config_globs:
        await _mine_config_files(files, cfg, merged)

    if merged.team and is_placeholder_team(merged.team, conventions.placeholder_teams):
        merged.team = None
    apply_conventions(merged, conventions)
    if not merged.name:
        merged.name = repo_name
    return merged


async def _mine_config_files(files: RepoFiles, cfg: ManifestConfig, md: ServiceManifest) -> None:
    mined = MinedDeps()
    seen: list[str] = []
    for pattern in cfg.config_globs:
        for rel in await files.glob(pattern):
            if rel in seen:
                continue
            seen.append(rel)
            text = await files.read_text(rel)
            if text is not None:
                mine_file(rel, text, mined)
    mined.apply_to(md)
    md.source_files.extend(r for r in seen if r not in md.source_files)
