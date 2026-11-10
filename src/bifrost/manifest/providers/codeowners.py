"""CODEOWNERS provider: the catch-all ``*`` rule names the owning team."""

from __future__ import annotations

from bifrost.cli.config_sections import CodeownersConfig
from bifrost.manifest.files import RepoFiles
from bifrost.manifest.model import ServiceManifest
from bifrost.manifest.providers.base import first_existing

NAME = "codeowners"


def _normalise_owner(token: str, strip_org_prefix: bool) -> str | None:
    if not token.startswith("@"):
        return None  # email owners and anything else that is not a handle
    handle = token[1:]
    if strip_org_prefix:
        handle = handle.rsplit("/", 1)[-1]
    return handle or None


def parse_codeowners(text: str, strip_org_prefix: bool) -> ServiceManifest | None:
    owners: list[str] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *tokens = line.split()
        if pattern != "*":
            continue
        owners = []
        for token in tokens:
            owner = _normalise_owner(token, strip_org_prefix)
            if owner and owner not in owners:
                owners.append(owner)
    if owners is None:
        return None
    return ServiceManifest(
        team=owners[0] if owners else None,
        owners=owners,
        providers=[NAME],
    )


class CodeownersProvider:
    name = NAME

    def __init__(self, cfg: CodeownersConfig) -> None:
        self.cfg = cfg

    async def load(self, files: RepoFiles, *, repo_name: str) -> ServiceManifest | None:
        found = await first_existing(files, self.cfg.paths)
        if found is None:
            return None
        rel, text = found
        md = parse_codeowners(text, self.cfg.strip_org_prefix)
        if md is not None:
            md.source_files.append(rel)
        return md
