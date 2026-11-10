"""``sources.local.allowed_roots`` policy for local-path repos.

Empty roots allow any path in dev and refuse every path in production mode.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from bifrost.cli.config import BifrostSettings
from bifrost.errors import ValidationError


class LocalPathNotAllowedError(ValidationError):
    code = "request.local_path_not_allowed"


def local_path_from_url(url: str) -> Path | None:
    """Path for a ``file://`` or absolute-path URL, None for any remote form."""
    if url.startswith("file://"):
        return Path(unquote(urlparse(url).path))
    if url.startswith("/"):
        return Path(url)
    return None


def local_path_allowed(path: Path | str, settings: BifrostSettings) -> bool:
    roots = [Path(r).resolve() for r in settings.sources.local.allowed_roots]
    if not roots:
        return not settings.production_mode
    resolved = Path(path).resolve()
    return any(resolved.is_relative_to(root) for root in roots)


def ensure_local_path_allowed(path: Path | str, settings: BifrostSettings) -> None:
    if local_path_allowed(path, settings):
        return
    if not settings.sources.local.allowed_roots:
        raise LocalPathNotAllowedError(
            f"local path {str(path)!r} refused: production mode requires "
            "sources.local.allowed_roots to be set"
        )
    raise LocalPathNotAllowedError(
        f"local path {str(path)!r} is outside sources.local.allowed_roots"
    )
