"""GitAdapter — subprocess-based SourceAdapter for local Git repositories.

Clones/pulls repos via `git` subprocess, lists files with `git ls-files`,
reads content from the worktree, and detects changes via `git log`/`git diff`.
This is the primary adapter feeding source files into the extraction pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from bifrost.adapters.base import (
    AdapterCapabilities,
    AdapterScope,
    ContentBody,
    ExtractedContent,
    HealthStatus,
    PermissionResult,
    RevisionInfo,
    SourceAdapter,
    SourceCategory,
    SourceMetadata,
    SourceObject,
    SourceObjectRef,
    SourceObjectType,
    UserContext,
)
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.core.local_roots import (
    LocalPathNotAllowedError,
    ensure_local_path_allowed,
    local_path_from_url,
)
from bifrost.errors import NotFoundError, UpstreamError
from bifrost.manifest.files import GhError, GhRunner, run_gh

logger = logging.getLogger(__name__)

# ── Exceptions ──────────────────────────────────────────────────────


class GitAdapterError(UpstreamError):
    """Base exception for Git adapter operations."""

    code = "adapter.git"
    retryable = False
    http_status = 502


class GitCloneError(GitAdapterError):
    """Failed to clone a repository."""

    code = "adapter.git.clone"


class GitPullError(GitAdapterError):
    """Failed to pull latest changes."""

    code = "adapter.git.pull"


class GitFileNotFoundError(GitAdapterError, NotFoundError):
    """Requested file does not exist in the repository."""

    code = "adapter.git.file_not_found"
    http_status = 404


class GitPermissionError(GitAdapterError):
    """Read access to a repository could not be established."""

    code = "adapter.git.permission"


class GitCommandError(GitAdapterError):
    """A git subprocess command failed."""

    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        cmd_str = " ".join(command)
        super().__init__(f"git command failed (exit {returncode}): {cmd_str}\n{stderr}")


# ── URL validation ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _GitResult:
    """Captured output from a git subprocess call."""

    stdout: str
    stderr: str
    returncode: int


_SAFE_URL_RE = re.compile(r"^(?:https?://[^\s]+|git@[^\s:]+:[^\s]+|ssh://[^\s]+|/[^\s]+)$")

# SSRF guard. Literal hosts are checked without DNS in _validate_repo_url; hostnames are
# resolved and every A/AAAA record checked in _assert_resolves_public before git runs.
# A hostname that rebinds between that check and git's own lookup is not covered; that
# needs an egress proxy. Dev opt-out: BIFROST_ALLOW_PRIVATE_GIT_URLS (refused in production).
_SSRF_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.googleapis.com",
        "metadata.azure.com",
    }
)
_SSRF_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_LOOKING = re.compile(r"^[0-9a-fx.]+$")


def _ssrf_opt_in() -> bool:
    enabled = os.environ.get("BIFROST_ALLOW_PRIVATE_GIT_URLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if enabled and get_settings().production_mode:
        msg = (
            "BIFROST_ALLOW_PRIVATE_GIT_URLS is refused in production mode; "
            "unset it to start cloning again."
        )
        raise GitAdapterError(msg)
    return enabled


def _extract_host(url: str) -> tuple[str, int | None]:
    """Host and explicit port for ssh / https / scp-style URLs; ("", None) for local paths."""
    if url.startswith("git@") and ":" in url:
        return url[4 : url.index(":")].lower().strip("[]"), None
    if url.startswith(("http://", "https://", "ssh://")):
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            msg = f"Invalid repository URL: {url!r}"
            raise GitAdapterError(msg) from exc
        return (parsed.hostname or "").lower(), port
    return "", None


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    host = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _IPV4_LOOKING.match(host):
        try:  # decimal / hex / octal / short forms (2130706433, 0x7f000001, 127.1)
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ip_is_blocked(ip.ipv4_mapped)
        if ip in _NAT64:
            return _ip_is_blocked(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None:
            return _ip_is_blocked(ip.sixtofour)
        if ip.teredo is not None:
            return any(_ip_is_blocked(part) for part in ip.teredo)
    return not ip.is_global or ip.is_multicast


def _validate_repo_url(url: str) -> None:
    """Reject URLs that could be used for command injection or SSRF."""
    if not _SAFE_URL_RE.match(url):
        msg = f"Invalid repository URL: {url!r}"
        raise GitAdapterError(msg)
    if any(c in url for c in (";", "|", "&", "$", "`", "\n", "\\")):
        msg = f"Repository URL contains disallowed characters: {url!r}"
        raise GitAdapterError(msg)

    allow_private = _ssrf_opt_in()

    host, _ = _extract_host(url)
    if not host:
        return  # local filesystem path -- not SSRF-reachable
    if not allow_private:
        host = host.rstrip(".")
        if host in _SSRF_BLOCKED_HOSTS or host.endswith(_SSRF_BLOCKED_SUFFIXES):
            msg = (
                f"Repository URL blocked by SSRF guard ({host!r}). "
                "Set BIFROST_ALLOW_PRIVATE_GIT_URLS=true to override in dev."
            )
            raise GitAdapterError(msg)
        ip = _parse_ip(host)
        if ip is not None and _ip_is_blocked(ip):
            msg = (
                f"Repository URL points at a private/loopback address "
                f"({host!r}); blocked by SSRF guard. Set "
                "BIFROST_ALLOW_PRIVATE_GIT_URLS=true to override in dev."
            )
            raise GitAdapterError(msg)
    if url.startswith("http://") and not allow_private:
        msg = (
            f"Repository URL scheme is not allowed ({url.split(':', 1)[0]!r}): "
            "only https and ssh are accepted; blocked by SSRF guard."
        )
        raise GitAdapterError(msg)


def _resolve_host(host: str, port: int | None = None) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _assert_resolves_public(url: str) -> None:
    """Resolve the URL's host and refuse it if any address is non-public."""
    if _ssrf_opt_in():
        return
    host, port = _extract_host(url)
    if not host or _parse_ip(host) is not None:
        return
    try:
        addrs = await asyncio.to_thread(_resolve_host, host, port)
    except OSError as exc:
        msg = f"Could not resolve {host!r}: {exc}"
        raise GitCloneError(msg) from exc
    if not addrs:
        msg = f"Could not resolve {host!r}: no addresses returned"
        raise GitCloneError(msg)
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if _ip_is_blocked(ip):
            msg = (
                f"Repository host {host!r} resolves to a private/loopback address "
                f"({addr}); blocked by SSRF guard."
            )
            raise GitAdapterError(msg)


def _github_owner_name(url: str, host: str) -> tuple[str, str] | None:
    """(owner, name) when *url* is an https / ssh / scp-style remote on *host*."""
    url_host, _ = _extract_host(url)
    if not url_host or url_host != host.lower():
        return None
    path = url[url.index(":") + 1 :] if url.startswith("git@") else urlparse(url).path
    parts = path.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1].removesuffix(".git")


# ── GitAdapter ──────────────────────────────────────────────────────


class GitAdapter(SourceAdapter):
    """SourceAdapter backed by local `git` subprocess calls.

    Operates on local clones. Call ``clone()`` to create a worktree,
    then use the ABC methods (``discover``, ``extract``, etc.) scoped
    to that worktree path via ``AdapterScope.source_scope["repo_path"]``.
    """

    def __init__(
        self,
        *,
        git_binary: str = "git",
        default_depth: int = 1,
        instance: str = "",
        gh_runner: GhRunner = run_gh,
        settings: BifrostSettings | None = None,
    ) -> None:
        self._git = git_binary
        self._default_depth = default_depth
        self._instance = instance
        self._gh = gh_runner
        self._settings = settings

    # ── SourceAdapter ABC ───────────────────────────────────────────

    @property
    def adapter_id(self) -> str:
        return "git"

    async def discover(self, scope: AdapterScope) -> AsyncIterator[SourceObject]:
        """Yield a SourceObject for every tracked file in the repo."""
        repo_path = self._resolve_repo_path(scope)
        files = await self.list_files(repo_path)

        for fpath in files:
            if scope.filter and scope.filter.path_pattern:
                import fnmatch

                if not fnmatch.fnmatch(fpath, scope.filter.path_pattern):
                    continue

            ref = self._make_ref(repo_path, fpath)
            full = Path(repo_path) / fpath
            stat = full.stat() if full.exists() else None

            yield SourceObject(
                ref=ref,
                name=Path(fpath).name,
                category=SourceCategory.CODE_TRUTH,
                metadata=SourceMetadata(
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC) if stat else None,
                    size_bytes=stat.st_size if stat else None,
                    content_type="text/plain",
                ),
            )

    async def identify(self, scope: AdapterScope) -> list[SourceObjectRef]:
        refs: list[SourceObjectRef] = []
        async for obj in self.discover(scope):
            refs.append(obj.ref)
        return refs

    async def extract(self, ref: SourceObjectRef) -> ExtractedContent:
        repo_path, fpath = self._parse_ref(ref)
        content = await self.read_file(repo_path, fpath)
        meta = await self.get_metadata(repo_path)
        full = Path(repo_path) / fpath
        stat = full.stat() if full.exists() else None

        return ExtractedContent(
            ref=ref,
            revision=RevisionInfo(
                ref=ref,
                revision_token=meta["head_commit"],
                revision_timestamp=datetime.now(UTC),
                change_summary=None,
            ),
            content=ContentBody(kind="text", text=content),
            metadata=SourceMetadata(
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC) if stat else None,
                size_bytes=len(content.encode()) if content else 0,
                content_type="text/plain",
            ),
            relevance_hints=[],
            extracted_at=datetime.now(UTC),
            original_token_estimate=len(content) // 4 if content else 0,
        )

    async def track_revision(self, ref: SourceObjectRef) -> RevisionInfo:
        repo_path, fpath = self._parse_ref(ref)
        # Get the last commit that touched this file
        result = await self._run_git(
            ["log", "-1", "--format=%H %aI", "--", fpath],
            cwd=repo_path,
        )
        parts = result.stdout.strip().split(" ", 1)
        if len(parts) == 2:
            sha, iso_date = parts
            ts = datetime.fromisoformat(iso_date)
        else:
            meta = await self.get_metadata(repo_path)
            sha = meta["head_commit"]
            ts = datetime.now(UTC)

        return RevisionInfo(
            ref=ref,
            revision_token=sha,
            revision_timestamp=ts,
        )

    async def check_permission(self, user: UserContext, ref: SourceObjectRef) -> PermissionResult:
        """Can Bifrost read this repo? GitHub remotes are probed with the configured gh
        identity; local repos must sit inside ``sources.local.allowed_roots``.

        Raises ``GitPermissionError`` when access cannot be established (fail closed).
        """
        settings = self._settings or get_settings()
        repo_path = ref.source_instance
        if not Path(repo_path).is_dir():
            msg = f"cannot resolve a local clone for {ref.urn}: {repo_path!r} is not a directory"
            raise GitPermissionError(msg)
        try:
            remote = (
                await self._run_git(["config", "--get", "remote.origin.url"], cwd=repo_path)
            ).stdout.strip()
        except GitCommandError:
            remote = ""  # no origin configured

        local = local_path_from_url(remote) if remote else Path(repo_path)
        if local is not None:
            try:
                ensure_local_path_allowed(local, settings)
            except LocalPathNotAllowedError as exc:
                return PermissionResult(can_read=False, determined=True, reason=str(exc))
            return PermissionResult(can_read=True, determined=True)

        host = settings.sources.github.host
        owner_name = _github_owner_name(remote, host)
        if owner_name is None:
            msg = f"no permission check for remote {remote!r}: only {host} and local paths"
            raise GitPermissionError(msg)
        return await self._check_github_read(*owner_name, host=host)

    async def _check_github_read(self, owner: str, name: str, *, host: str) -> PermissionResult:
        argv = ["api"]
        if host != "github.com":
            argv += ["--hostname", host]
        argv.append(f"repos/{owner}/{name}")
        for attempt in (1, 2):
            try:
                await self._gh(argv, timeout=15.0)
                return PermissionResult(can_read=True, determined=True)
            except GhError as exc:
                stderr = exc.stderr.lower()
                rate_limited = "rate limit" in stderr
                if exc.not_found or ("403" in stderr and not rate_limited):
                    return PermissionResult(
                        can_read=False,
                        determined=True,
                        reason=f"{owner}/{name} is not readable by the configured gh identity",
                    )
                if attempt == 2:
                    msg = f"could not verify read access to {owner}/{name}: {exc}"
                    raise GitPermissionError(msg) from exc
                logger.warning("gh api failed for %s/%s (%s); retrying once", owner, name, exc)
        raise AssertionError("unreachable")

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_watch=False,
            supports_incremental_identify=True,
            supports_change_summary=True,
            supports_permission_check=True,
            supported_object_types=[
                SourceObjectType.FILE,
                SourceObjectType.COMMIT,
                SourceObjectType.BRANCH,
            ],
            source_category=SourceCategory.CODE_TRUTH,
            max_concurrent_extractions=10,
            supports_partial_extraction=False,
        )

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            result = await self._run_git(["--version"])
            elapsed = (time.monotonic() - start) * 1000
            return HealthStatus(
                status="healthy",
                latency_ms=elapsed,
                message=result.stdout.strip(),
                checked_at=datetime.now(UTC),
            )
        except GitCommandError as exc:
            elapsed = (time.monotonic() - start) * 1000
            return HealthStatus(
                status="unhealthy",
                latency_ms=elapsed,
                message=str(exc),
                checked_at=datetime.now(UTC),
            )

    # ── Git-specific operations ─────────────────────────────────────

    async def clone(
        self,
        url: str,
        dest: str,
        *,
        shallow: bool = True,
        sparse_paths: list[str] | None = None,
    ) -> None:
        """Clone a repository to *dest*.

        Args:
            url: Repository URL (https, ssh, or local path).
            dest: Destination directory for the clone.
            shallow: Use ``--depth 1`` for initial extraction speed.
            sparse_paths: If set, enable sparse checkout and only fetch these paths.
        """
        _validate_repo_url(url)
        await _assert_resolves_public(url)
        local = local_path_from_url(url)
        if local is not None:
            try:
                ensure_local_path_allowed(local, self._settings or get_settings())
            except LocalPathNotAllowedError as exc:
                raise GitAdapterError(str(exc)) from exc
        cmd = ["clone"]
        if shallow:
            cmd.extend(["--depth", str(self._default_depth)])
        if sparse_paths:
            cmd.append("--no-checkout")
        cmd.extend([url, dest])

        start = time.monotonic()
        try:
            await self._run_git(cmd)
        except GitCommandError as exc:
            raise GitCloneError(f"Failed to clone {url}: {exc}") from exc
        elapsed = time.monotonic() - start
        logger.info("Cloned %s to %s in %.1fs (shallow=%s)", url, dest, elapsed, shallow)

        if sparse_paths:
            await self._run_git(["sparse-checkout", "init", "--cone"], cwd=dest)
            await self._run_git(["sparse-checkout", "set", *sparse_paths], cwd=dest)
            await self._run_git(["checkout"], cwd=dest)
            logger.info("Sparse checkout enabled for %d paths", len(sparse_paths))

    async def pull(self, repo_path: str) -> None:
        """Pull latest changes for an existing clone."""
        start = time.monotonic()
        try:
            await self._run_git(["pull", "--ff-only"], cwd=repo_path)
        except GitCommandError as exc:
            raise GitPullError(f"Failed to pull in {repo_path}: {exc}") from exc
        elapsed = time.monotonic() - start
        logger.info("Pulled %s in %.1fs", repo_path, elapsed)

    async def fetch_all(self, repo_path: str) -> None:
        """Fetch all refs without merging. Safe to call on any repo."""
        try:
            await self._run_git(["fetch", "--all", "--tags"], cwd=repo_path)
        except GitCommandError as exc:
            raise GitAdapterError(f"git fetch failed in {repo_path}: {exc}") from exc

    async def reset_hard_to_remote(self, repo_path: str) -> None:
        """Reset the worktree to the tip of origin's default branch.

        Used for incremental extraction to pick up the latest commits while
        keeping the local clone reusable (preserves history for git diff).
        """
        # Resolve default remote branch (main or master)
        branch = "main"
        try:
            result = await self._run_git(
                ["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path
            )
            ref = result.stdout.strip()  # e.g. "refs/remotes/origin/main"
            if "/" in ref:
                branch = ref.split("/")[-1]
        except GitCommandError:
            pass  # fall back to "main"
        try:
            await self._run_git(["reset", "--hard", f"origin/{branch}"], cwd=repo_path)
        except GitCommandError as exc:
            raise GitAdapterError(
                f"reset --hard origin/{branch} failed in {repo_path}: {exc}"
            ) from exc

    async def unshallow_if_needed(self, repo_path: str) -> None:
        """Convert a shallow clone to full so `git diff <old_sha> HEAD` works.

        Idempotent — silent if already full.
        """
        shallow_file = Path(repo_path) / ".git" / "shallow"
        if not shallow_file.exists():
            return
        try:
            await self._run_git(["fetch", "--unshallow", "--tags"], cwd=repo_path)
            logger.info("Unshallowed clone at %s", repo_path)
        except GitCommandError as exc:
            logger.warning("fetch --unshallow failed for %s: %s", repo_path, exc)

    async def commit_exists(self, repo_path: str, sha: str) -> bool:
        """Return True if the given sha is present in the local clone."""
        try:
            await self._run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_path)
            return True
        except GitCommandError:
            return False

    async def list_files(self, repo_path: str) -> list[str]:
        """List tracked files respecting .gitignore.

        Uses ``git ls-files`` so we never reimplement ignore logic.
        """
        result = await self._run_git(["ls-files"], cwd=repo_path)
        files = [line for line in result.stdout.splitlines() if line.strip()]
        return sorted(files)

    async def read_file(self, repo_path: str, file_path: str) -> str:
        """Read a file from the worktree."""
        full_path = Path(repo_path) / file_path
        # Prevent path traversal
        try:
            full_path.resolve().relative_to(Path(repo_path).resolve())
        except ValueError:
            msg = f"Path traversal detected: {file_path!r}"
            raise GitAdapterError(msg) from None

        if not full_path.is_file():
            raise GitFileNotFoundError(f"File not found: {file_path} in {repo_path}")

        return await asyncio.to_thread(full_path.read_text, encoding="utf-8", errors="replace")

    async def get_changes_since(
        self,
        repo_path: str,
        since_commit: str,
    ) -> list[dict[str, str]]:
        """Return files changed since a given commit.

        Each entry has keys: ``status`` (A/M/D/R), ``path``, and
        optionally ``old_path`` for renames.
        """
        result = await self._run_git(
            ["diff", "--name-status", "--no-renames", since_commit, "HEAD"],
            cwd=repo_path,
        )
        changes: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                changes.append({"status": parts[0], "path": parts[1]})
        return changes

    async def get_metadata(self, repo_path: str) -> dict[str, str]:
        """Return repo metadata: url, branch, head_commit.

        If no remote is configured, ``url`` is an empty string.
        """
        try:
            url_result = await self._run_git(
                ["config", "--get", "remote.origin.url"],
                cwd=repo_path,
            )
            url = url_result.stdout.strip()
        except GitCommandError:
            url = ""

        branch_result = await self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
        )
        commit_result = await self._run_git(
            ["rev-parse", "HEAD"],
            cwd=repo_path,
        )
        return {
            "url": url,
            "branch": branch_result.stdout.strip(),
            "head_commit": commit_result.stdout.strip(),
        }

    # ── Internal helpers ────────────────────────────────────────────

    def _resolve_repo_path(self, scope: AdapterScope) -> str:
        repo_path = scope.source_scope.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            msg = "AdapterScope.source_scope must contain 'repo_path' (str)"
            raise GitAdapterError(msg)
        return repo_path

    def _make_ref(self, repo_path: str, file_path: str) -> SourceObjectRef:
        instance = self._instance or self._instance_from_path(repo_path)
        return SourceObjectRef(
            urn=f"bifrost:git:{instance}:file:{file_path}",
            adapter_id="git",
            source_instance=instance,
            object_type=SourceObjectType.FILE,
            object_id=file_path,
        )

    def _parse_ref(self, ref: SourceObjectRef) -> tuple[str, str]:
        """Extract (repo_path, file_path) from a ref.

        Since refs don't carry the filesystem path, we derive the repo_path
        from the instance name. Callers using the ABC flow should set
        ``AdapterScope.source_scope["repo_path"]`` instead.
        """
        # The ref's source_instance may be a path or a name.
        # The object_id is always the relative file path.
        return ref.source_instance, ref.object_id

    @staticmethod
    def _instance_from_path(repo_path: str) -> str:
        """Derive a stable instance identifier from a repo path."""
        return hashlib.sha256(os.path.realpath(repo_path).encode()).hexdigest()[:12]

    async def _run_git(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
    ) -> _GitResult:
        """Run a git command and return captured output.

        Never uses ``shell=True`` — always passes an argument list.
        Raises ``GitCommandError`` on non-zero exit.
        """
        cmd = [self._git, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = proc.returncode or 0

        if proc.returncode != 0:
            raise GitCommandError(command=cmd, returncode=returncode, stderr=stderr)

        return _GitResult(stdout=stdout, stderr=stderr, returncode=returncode)
