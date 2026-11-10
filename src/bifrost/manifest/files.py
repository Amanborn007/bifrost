"""Read-only repo file access: local checkout, GitHub via ``gh api``, or in-memory."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SKIP_DIRS = frozenset({".git", "node_modules"})
_DEFAULT_MAX_BYTES = 262144


class RepoFileError(ValueError):
    """A requested path was unsafe or a file was over the size cap."""


class GhError(RuntimeError):
    """``gh`` exited non-zero (or timed out)."""

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f"gh exited {returncode}: {stderr[:300]}")
        self.returncode = returncode
        self.stderr = stderr

    @property
    def not_found(self) -> bool:
        return "404" in self.stderr or "Not Found" in self.stderr


GhRunner = Callable[..., Awaitable[str]]


async def run_gh(argv: Sequence[str], timeout: float = 15.0) -> str:  # noqa: ASYNC109
    """Run ``gh <argv...>`` and return stdout; raise ``GhError`` on failure."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GhError(-1, f"timed out after {timeout}s: gh {' '.join(argv)[:120]}") from None
    if proc.returncode != 0:
        raise GhError(proc.returncode or 1, stderr.decode("utf-8", errors="replace"))
    return stdout.decode("utf-8", errors="replace")


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts = pattern.split("/")
    out: list[str] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        seg = "".join(
            "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in part
        )
        out.append(seg if last else seg + "/")
    return re.compile("^" + "".join(out) + "$")


def match_glob(pattern: str, path: str) -> bool:
    """Glob over repo-relative posix paths: ``*``/``?`` stay in a segment, ``**`` spans dirs."""
    return _glob_regex(pattern).match(path) is not None


def _check_rel(rel: str) -> str:
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise RepoFileError(f"absolute or empty path not allowed: {rel!r}")
    if ".." in PurePosixPath(rel.replace("\\", "/")).parts:
        raise RepoFileError(f"path escapes repo root: {rel!r}")
    return rel


@runtime_checkable
class RepoFiles(Protocol):
    is_remote: bool

    async def read_text(self, rel: str) -> str | None: ...

    async def glob(self, pattern: str) -> list[str]: ...


class LocalRepoFiles:
    is_remote = False

    def __init__(self, root: Path | str, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    def _inside(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self.root)

    async def read_text(self, rel: str) -> str | None:
        _check_rel(rel)
        return await asyncio.to_thread(self._read, rel)

    def _read(self, rel: str) -> str | None:
        path = self.root / rel
        if not self._inside(path):
            raise RepoFileError(f"{rel!r} resolves outside the repo root")
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > self.max_bytes:
            raise RepoFileError(f"{rel!r} is {size} bytes, over max_bytes={self.max_bytes}")
        return path.read_text(encoding="utf-8", errors="replace")

    async def glob(self, pattern: str) -> list[str]:
        return await asyncio.to_thread(self._glob, pattern)

    def _glob(self, pattern: str) -> list[str]:
        rx = _glob_regex(pattern)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(self.root).as_posix()
                if not rx.match(rel) or not self._inside(full) or not full.is_file():
                    continue
                if full.stat().st_size > self.max_bytes:
                    continue
                found.append(rel)
        return sorted(found)


class GitHubRepoFiles:
    is_remote = True

    def __init__(
        self,
        owner: str,
        name: str,
        host: str = "github.com",
        ref: str | None = None,
        runner: GhRunner = run_gh,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self.owner = owner
        self.name = name
        self.host = host
        self.ref = ref
        self.max_bytes = max_bytes
        self._runner = runner
        self._tree: list[str] | None = None
        self._tree_lock = asyncio.Lock()

    def _argv(self, endpoint: str) -> list[str]:
        argv = ["api"]
        if self.host != "github.com":
            argv += ["--hostname", self.host]
        return [*argv, endpoint]

    async def _call(self, endpoint: str) -> str | None:
        """Run one API call; None on 404, one logged retry on other errors, then raise."""
        argv = self._argv(endpoint)
        for attempt in (1, 2):
            try:
                return await self._runner(argv, timeout=15.0)
            except GhError as exc:
                if exc.not_found:
                    return None
                if attempt == 2:
                    raise
                logger.warning("gh api failed for %s (%s); retrying once", endpoint, exc)
        raise AssertionError("unreachable")

    async def read_text(self, rel: str) -> str | None:
        _check_rel(rel)
        endpoint = f"repos/{self.owner}/{self.name}/contents/{quote(rel, safe='/')}"
        if self.ref:
            endpoint += f"?ref={quote(self.ref, safe='')}"
        out = await self._call(endpoint)
        if out is None:
            return None
        blob = json.loads(out)
        if not isinstance(blob, dict) or blob.get("type", "file") != "file":
            return None
        size = blob.get("size")
        if isinstance(size, int) and size > self.max_bytes:
            raise RepoFileError(f"{rel!r} is {size} bytes, over max_bytes={self.max_bytes}")
        if blob.get("encoding") != "base64":
            return None
        data = base64.b64decode(blob.get("content") or "")
        if len(data) > self.max_bytes:
            raise RepoFileError(f"{rel!r} is {len(data)} bytes, over max_bytes={self.max_bytes}")
        return data.decode("utf-8", errors="replace")

    async def _load_tree(self) -> list[str]:
        async with self._tree_lock:
            if self._tree is not None:
                return self._tree
            ref = quote(self.ref or "HEAD", safe="")
            out = await self._call(f"repos/{self.owner}/{self.name}/git/trees/{ref}?recursive=1")
            if out is None:
                logger.warning("git tree not found for %s/%s", self.owner, self.name)
                self._tree = []
                return self._tree
            blob = json.loads(out)
            if blob.get("truncated"):
                logger.warning(
                    "git tree truncated for %s/%s; globs may miss files", self.owner, self.name
                )
            self._tree = sorted(
                e["path"]
                for e in blob.get("tree", [])
                if e.get("type") == "blob" and (e.get("size") or 0) <= self.max_bytes
            )
            return self._tree

    async def glob(self, pattern: str) -> list[str]:
        rx = _glob_regex(pattern)
        return [p for p in await self._load_tree() if rx.match(p)]


class InMemoryRepoFiles:
    is_remote = False

    def __init__(self, files: Mapping[str, str]) -> None:
        self._files = dict(files)

    async def read_text(self, rel: str) -> str | None:
        return self._files.get(_check_rel(rel))

    async def glob(self, pattern: str) -> list[str]:
        rx = _glob_regex(pattern)
        return sorted(p for p in self._files if rx.match(p))
