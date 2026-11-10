"""The explicit "no manifest provider" choice."""

from __future__ import annotations

from bifrost.manifest.files import RepoFiles
from bifrost.manifest.model import ServiceManifest


class NoneProvider:
    name = "none"

    async def load(self, files: RepoFiles, *, repo_name: str) -> ServiceManifest | None:
        return None
