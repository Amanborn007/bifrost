"""GitAdapter.check_permission: real read check, fail closed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bifrost.adapters.base import SourceObjectRef, SourceObjectType, UserContext
from bifrost.adapters.git import GitAdapter, GitAdapterError, GitPermissionError
from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import GitHubSourceConfig, LocalSourceConfig, SourcesConfig
from bifrost.manifest.files import GhError

USER = UserContext(user_id="u1")


def _settings(
    *, roots: list[str] | None = None, host: str = "github.com", production: bool = False
) -> BifrostSettings:
    return BifrostSettings(
        production_mode=production,
        sources=SourcesConfig(
            github=GitHubSourceConfig(host=host),
            local=LocalSourceConfig(allowed_roots=roots or []),
        ),
    )


def _repo(path: Path, remote: str | None) -> Path:
    path.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, env=env
    )
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "c"], cwd=path, check=True, capture_output=True, env=env)
    if remote:
        subprocess.run(
            ["git", "remote", "add", "origin", remote], cwd=path, check=True, capture_output=True
        )
    return path


def _ref(repo: Path | str) -> SourceObjectRef:
    return SourceObjectRef(
        urn="bifrost:git:inst:file:a.py",
        adapter_id="git",
        source_instance=str(repo),
        object_type=SourceObjectType.FILE,
        object_id="a.py",
    )


class FakeGh:
    def __init__(self, *results: str | Exception) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, argv, timeout: float = 15.0) -> str:  # noqa: ANN001
        self.calls.append(list(argv))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestGitHubRemote:
    async def test_readable_repo_allowed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "r", "https://github.com/acme/svc.git")
        gh = FakeGh("{}")
        adapter = GitAdapter(gh_runner=gh, settings=_settings())
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is True and result.determined is True
        assert gh.calls == [["api", "repos/acme/svc"]]

    async def test_enterprise_host_passes_hostname(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "r", "git@ghe.example.com:acme/svc.git")
        gh = FakeGh("{}")
        adapter = GitAdapter(gh_runner=gh, settings=_settings(host="ghe.example.com"))
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is True
        assert gh.calls == [["api", "--hostname", "ghe.example.com", "repos/acme/svc"]]

    @pytest.mark.parametrize("stderr", ["gh: Not Found (HTTP 404)", "gh: Forbidden (HTTP 403)"])
    async def test_not_visible_is_a_determined_denial(self, tmp_path: Path, stderr: str) -> None:
        repo = _repo(tmp_path / "r", "https://github.com/acme/svc.git")
        gh = FakeGh(GhError(1, stderr))
        adapter = GitAdapter(gh_runner=gh, settings=_settings())
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is False and result.determined is True
        assert result.reason
        assert len(gh.calls) == 1

    async def test_transient_error_retried_once_then_allowed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "r", "https://github.com/acme/svc.git")
        gh = FakeGh(GhError(1, "HTTP 500"), "{}")
        adapter = GitAdapter(gh_runner=gh, settings=_settings())
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is True
        assert len(gh.calls) == 2

    async def test_persistent_error_raises(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "r", "https://github.com/acme/svc.git")
        gh = FakeGh(GhError(1, "HTTP 500"), GhError(1, "HTTP 502"))
        adapter = GitAdapter(gh_runner=gh, settings=_settings())
        with pytest.raises(GitPermissionError, match="502"):
            await adapter.check_permission(USER, _ref(repo))
        assert len(gh.calls) == 2

    async def test_unsupported_remote_host_raises(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "r", "https://gitlab.com/acme/svc.git")
        adapter = GitAdapter(gh_runner=FakeGh(), settings=_settings())
        with pytest.raises(GitPermissionError, match="gitlab.com"):
            await adapter.check_permission(USER, _ref(repo))


class TestLocalPaths:
    async def test_inside_allowed_root(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "root" / "svc", None)
        adapter = GitAdapter(gh_runner=FakeGh(), settings=_settings(roots=[str(tmp_path / "root")]))
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is True and result.determined is True

    async def test_outside_allowed_root_denied(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "elsewhere" / "svc", None)
        adapter = GitAdapter(gh_runner=FakeGh(), settings=_settings(roots=[str(tmp_path / "root")]))
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is False and result.determined is True
        assert "allowed_roots" in (result.reason or "")

    async def test_production_without_roots_denied(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "svc", None)
        adapter = GitAdapter(gh_runner=FakeGh(), settings=_settings(production=True))
        result = await adapter.check_permission(USER, _ref(repo))
        assert result.can_read is False and result.determined is True

    async def test_unresolvable_ref_fails_closed(self, tmp_path: Path) -> None:
        adapter = GitAdapter(gh_runner=FakeGh(), settings=_settings())
        with pytest.raises(GitPermissionError):
            await adapter.check_permission(USER, _ref(tmp_path / "does-not-exist"))


def test_error_type_and_capability() -> None:
    assert issubclass(GitPermissionError, GitAdapterError)
    assert GitAdapter().get_capabilities().supports_permission_check is True
