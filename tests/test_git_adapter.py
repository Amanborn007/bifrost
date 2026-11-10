"""Integration tests for GitAdapter — exercises real git subprocess operations.

Uses a local bare repo fixture (no network required) and the
octocat/Hello-World public repo for a live clone test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bifrost.adapters.base import (
    AdapterScope,
    ScopeFilter,
    SourceAdapter,
    SourceObjectType,
)
from bifrost.adapters.git import (
    GitAdapter,
    GitAdapterError,
    GitCloneError,
    GitFileNotFoundError,
    _validate_repo_url,
)

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def adapter() -> GitAdapter:
    return GitAdapter(instance="test-instance")


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """Create a small local git repo with a few commits."""
    repo = tmp_path / "repo"
    repo.mkdir()

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env)

    git("init", "-b", "main")
    git("config", "user.email", "test@test.com")
    git("config", "user.name", "Test")

    # First commit
    (repo / "README.md").write_text("# Test Repo\n")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def main(): pass\n")
    git("add", ".")
    git("commit", "-m", "Initial commit")

    # Second commit — add more files and modify one
    (repo / "src" / "utils.py").write_text("def helper(): return 42\n")
    (repo / "src" / "main.py").write_text("def main():\n    return helper()\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    git("add", ".")
    git("commit", "-m", "Add utils and gitignore")

    return repo


@pytest.fixture
def bare_repo(tmp_path: Path, local_repo: Path) -> Path:
    """Create a bare clone for testing clone operations."""
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(local_repo), str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


@pytest.fixture
def first_commit(local_repo: Path) -> str:
    """Return the SHA of the first commit."""
    result = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=str(local_repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _scope(repo_path: str | Path, **kwargs) -> AdapterScope:
    """Build a scope pointing at a local repo."""
    return AdapterScope(
        source_scope={"repo_path": str(repo_path)},
        **kwargs,
    )


# ── ABC contract ────────────────────────────────────────────────────


class TestGitAdapterIsSourceAdapter:
    def test_subclass(self) -> None:
        assert issubclass(GitAdapter, SourceAdapter)

    def test_adapter_id(self, adapter: GitAdapter) -> None:
        assert adapter.adapter_id == "git"

    def test_capabilities(self, adapter: GitAdapter) -> None:
        caps = adapter.get_capabilities()
        assert caps.supports_incremental_identify is True
        assert caps.supports_change_summary is True
        assert SourceObjectType.FILE in caps.supported_object_types


# ── Clone / pull ────────────────────────────────────────────────────


class TestClone:
    @pytest.mark.asyncio
    async def test_clone_local_bare(
        self, adapter: GitAdapter, bare_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "cloned"
        await adapter.clone(str(bare_repo), str(dest), shallow=True)
        assert (dest / "README.md").exists()
        assert (dest / "src" / "main.py").exists()

    @pytest.mark.asyncio
    async def test_clone_invalid_url_rejected(self, adapter: GitAdapter, tmp_path: Path) -> None:
        with pytest.raises(GitAdapterError, match="Invalid repository URL"):
            await adapter.clone("not a valid url", str(tmp_path / "bad"))

    @pytest.mark.asyncio
    async def test_clone_injection_rejected(self, adapter: GitAdapter, tmp_path: Path) -> None:
        with pytest.raises(GitAdapterError, match="Invalid repository URL"):
            await adapter.clone("https://example.com; rm -rf /", str(tmp_path / "bad"))


class TestSSRFGuard:
    """`_validate_repo_url` must block SSRF targets (cloud-metadata,
    loopback, RFC1918, link-local) across https/ssh/git URL forms.

    These guard the fix from `feature/audit-fixes-tier-1` — the audit
    flagged that the metadata service (169.254.169.254) and private
    ranges were reachable. Pure-function tests: no network, no subprocess.
    """

    @pytest.fixture(autouse=True)
    def _no_optin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure the dev opt-out is off so the guard is active.
        monkeypatch.delenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", raising=False)

    @pytest.mark.parametrize(
        "url",
        [
            "https://169.254.169.254/latest/meta-data/",  # AWS IMDS
            "http://metadata.google.internal/computeMetadata",  # GCP
            "https://metadata.googleapis.com/x",
            "https://metadata.azure.com/x",
            "https://localhost/owner/repo.git",
            "https://myhost.local/owner/repo.git",
            "https://127.0.0.1/owner/repo.git",  # loopback
            "https://10.0.0.5/owner/repo.git",  # RFC1918
            "https://192.168.1.10/owner/repo.git",  # RFC1918
            "https://172.16.0.1/owner/repo.git",  # RFC1918 172.16/12
            "https://172.31.255.254/owner/repo.git",  # RFC1918 upper
            "https://169.254.1.1/owner/repo.git",  # link-local
            "git@10.0.0.5:owner/repo.git",  # ssh scp-form
            "ssh://git@192.168.0.1/owner/repo.git",  # ssh:// form
        ],
    )
    def test_ssrf_targets_blocked(self, url: str) -> None:
        with pytest.raises(GitAdapterError, match="SSRF|private/loopback"):
            _validate_repo_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/owner/repo.git",
            "https://gitlab.com/owner/repo.git",
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
        ],
    )
    def test_public_hosts_allowed(self, url: str) -> None:
        # Should not raise.
        _validate_repo_url(url)

    def test_opt_out_bypasses_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", "true")
        # With the dev opt-out, a private address is allowed through.
        _validate_repo_url("https://10.0.0.5/owner/repo.git")

    def test_172_non_private_range_allowed(self) -> None:
        # 172.15 and 172.32 are NOT in the RFC1918 172.16/12 block.
        _validate_repo_url("https://172.15.0.1/owner/repo.git")
        _validate_repo_url("https://172.32.0.1/owner/repo.git")

    @pytest.mark.asyncio
    async def test_clone_nonexistent_repo(self, adapter: GitAdapter, tmp_path: Path) -> None:
        with pytest.raises(GitCloneError):
            await adapter.clone(
                "https://github.com/nonexistent-owner-12345/nonexistent-repo-67890.git",
                str(tmp_path / "bad"),
            )


class TestPull:
    @pytest.mark.asyncio
    async def test_pull_local_clone(
        self, adapter: GitAdapter, bare_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "cloned"
        await adapter.clone(str(bare_repo), str(dest), shallow=False)
        # Pull should succeed (no-op since no new commits)
        await adapter.pull(str(dest))


# ── File listing ────────────────────────────────────────────────────


class TestListFiles:
    @pytest.mark.asyncio
    async def test_lists_tracked_files(self, adapter: GitAdapter, local_repo: Path) -> None:
        files = await adapter.list_files(str(local_repo))
        assert ".gitignore" in files
        assert "README.md" in files
        assert "src/main.py" in files
        assert "src/utils.py" in files

    @pytest.mark.asyncio
    async def test_gitignore_filtering(self, adapter: GitAdapter, local_repo: Path) -> None:
        # Create an ignored file — should NOT appear in ls-files
        pycache = local_repo / "__pycache__"
        pycache.mkdir()
        (pycache / "test.pyc").write_text("bytecode")

        files = await adapter.list_files(str(local_repo))
        assert not any("__pycache__" in f for f in files)
        assert not any(".pyc" in f for f in files)


# ── File reading ────────────────────────────────────────────────────


class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_existing_file(self, adapter: GitAdapter, local_repo: Path) -> None:
        content = await adapter.read_file(str(local_repo), "README.md")
        assert "# Test Repo" in content

    @pytest.mark.asyncio
    async def test_read_nested_file(self, adapter: GitAdapter, local_repo: Path) -> None:
        content = await adapter.read_file(str(local_repo), "src/main.py")
        assert "def main()" in content

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, adapter: GitAdapter, local_repo: Path) -> None:
        with pytest.raises(GitFileNotFoundError):
            await adapter.read_file(str(local_repo), "does-not-exist.txt")

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, adapter: GitAdapter, local_repo: Path) -> None:
        with pytest.raises(GitAdapterError, match="Path traversal"):
            await adapter.read_file(str(local_repo), "../../etc/passwd")


# ── Change detection ────────────────────────────────────────────────


class TestChanges:
    @pytest.mark.asyncio
    async def test_changes_since_first_commit(
        self, adapter: GitAdapter, local_repo: Path, first_commit: str
    ) -> None:
        changes = await adapter.get_changes_since(str(local_repo), first_commit)
        paths = [c["path"] for c in changes]
        # Second commit added utils.py, modified main.py, added .gitignore
        assert "src/utils.py" in paths
        assert "src/main.py" in paths
        assert ".gitignore" in paths

    @pytest.mark.asyncio
    async def test_no_changes_since_head(self, adapter: GitAdapter, local_repo: Path) -> None:
        changes = await adapter.get_changes_since(str(local_repo), "HEAD")
        assert changes == []


# ── Metadata ────────────────────────────────────────────────────────


class TestMetadata:
    @pytest.mark.asyncio
    async def test_metadata_extraction(self, adapter: GitAdapter, local_repo: Path) -> None:
        meta = await adapter.get_metadata(str(local_repo))
        assert "branch" in meta
        assert meta["branch"] == "main"
        assert "head_commit" in meta
        assert len(meta["head_commit"]) == 40  # Full SHA
        # local_repo has no remote, so url is empty
        assert meta["url"] == ""

    @pytest.mark.asyncio
    async def test_metadata_url_for_clone(
        self, adapter: GitAdapter, bare_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "cloned"
        await adapter.clone(str(bare_repo), str(dest), shallow=False)
        meta = await adapter.get_metadata(str(dest))
        assert "url" in meta
        assert str(bare_repo) in meta["url"]


# ── SourceAdapter ABC methods ───────────────────────────────────────


class TestDiscover:
    @pytest.mark.asyncio
    async def test_discover_yields_source_objects(
        self, adapter: GitAdapter, local_repo: Path
    ) -> None:
        scope = _scope(local_repo)
        objects = [obj async for obj in adapter.discover(scope)]
        names = [obj.ref.object_id for obj in objects]
        assert "README.md" in names
        assert "src/main.py" in names

    @pytest.mark.asyncio
    async def test_discover_with_path_filter(self, adapter: GitAdapter, local_repo: Path) -> None:
        scope = _scope(local_repo, filter=ScopeFilter(path_pattern="src/*.py"))
        objects = [obj async for obj in adapter.discover(scope)]
        names = [obj.ref.object_id for obj in objects]
        assert "src/main.py" in names
        assert "src/utils.py" in names
        assert "README.md" not in names


class TestIdentify:
    @pytest.mark.asyncio
    async def test_identify_returns_refs(self, adapter: GitAdapter, local_repo: Path) -> None:
        scope = _scope(local_repo)
        refs = await adapter.identify(scope)
        assert len(refs) >= 4
        urns = [r.urn for r in refs]
        assert any("README.md" in u for u in urns)


class TestExtract:
    @pytest.mark.asyncio
    async def test_extract_returns_content(self, local_repo: Path) -> None:
        # Use repo path as instance so _parse_ref can resolve it
        adapter = GitAdapter(instance=str(local_repo))
        scope = _scope(local_repo)
        refs = await adapter.identify(scope)
        readme_ref = next(r for r in refs if "README.md" in r.object_id)

        content = await adapter.extract(readme_ref)
        assert content.content.kind == "text"
        assert "# Test Repo" in (content.content.text or "")
        assert content.revision.revision_token  # non-empty SHA


class TestTrackRevision:
    @pytest.mark.asyncio
    async def test_track_revision(self, local_repo: Path) -> None:
        adapter = GitAdapter(instance=str(local_repo))
        scope = _scope(local_repo)
        refs = await adapter.identify(scope)
        readme_ref = next(r for r in refs if "README.md" in r.object_id)

        rev = await adapter.track_revision(readme_ref)
        assert len(rev.revision_token) == 40


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_passes(self, adapter: GitAdapter) -> None:
        status = await adapter.health_check()
        assert status.status == "healthy"
        assert "git version" in (status.message or "")


class TestSparseCheckout:
    @pytest.mark.asyncio
    async def test_sparse_clone(self, adapter: GitAdapter, bare_repo: Path, tmp_path: Path) -> None:
        dest = tmp_path / "sparse"
        await adapter.clone(str(bare_repo), str(dest), shallow=False, sparse_paths=["src"])
        files = await adapter.list_files(str(dest))
        # Sparse checkout should include src/ files
        assert any("src/" in f for f in files)
