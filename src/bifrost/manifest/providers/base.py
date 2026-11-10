"""Provider protocol: turn one repo's files into a (possibly partial) manifest."""

from __future__ import annotations

from typing import Protocol

from bifrost.manifest.files import RepoFiles
from bifrost.manifest.model import ServiceManifest


class ManifestProvider(Protocol):
    name: str

    async def load(self, files: RepoFiles, *, repo_name: str) -> ServiceManifest | None: ...


async def first_existing(files: RepoFiles, paths: list[str]) -> tuple[str, str] | None:
    """Return ``(path, text)`` for the first path that exists, probing in order."""
    for rel in paths:
        text = await files.read_text(rel)
        if text is not None:
            return rel, text
    return None
