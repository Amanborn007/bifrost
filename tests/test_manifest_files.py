"""RepoFiles implementations: local sandboxing, GitHub via a fake gh runner, in-memory."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from bifrost.manifest.files import (
    GhError,
    GitHubRepoFiles,
    InMemoryRepoFiles,
    LocalRepoFiles,
    RepoFileError,
    match_glob,
)


class TestMatchGlob:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            ("config/*.env", "config/us.env", True),
            ("config/*.env", "config/sub/us.env", False),
            ("config/*.env", "other/us.env", False),
            (".env.example", ".env.example", True),
            ("docker-compose*.y*ml", "docker-compose.yml", True),
            ("docker-compose*.y*ml", "docker-compose.override.yaml", True),
            ("docker-compose*.y*ml", "sub/docker-compose.yml", False),
            ("helm/**/values*.yaml", "helm/app/values.yaml", True),
            ("helm/**/values*.yaml", "helm/a/b/values-prod.yaml", True),
            ("helm/**/values*.yaml", "helm/values.yaml", True),
            ("helm/**/values*.yaml", "charts/app/values.yaml", False),
            ("CODEOWNERS", "CODEOWNERS", True),
            ("CODEOWNERS", "docs/CODEOWNERS", False),
        ],
    )
    def test_cases(self, pattern: str, path: str, expected: bool) -> None:
        assert match_glob(pattern, path) is expected


class TestLocalRepoFiles:
    async def test_reads_text(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        assert await LocalRepoFiles(tmp_path, max_bytes=100).read_text("a.txt") == "hello"

    async def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert await LocalRepoFiles(tmp_path, max_bytes=100).read_text("nope.txt") is None

    async def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("s")
        with pytest.raises(RepoFileError):
            await LocalRepoFiles(root, max_bytes=100).read_text("../secret.txt")
        with pytest.raises(RepoFileError):
            await LocalRepoFiles(root, max_bytes=100).read_text("a/../../secret.txt")

    async def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        (tmp_path / "x").write_text("x")
        with pytest.raises(RepoFileError):
            await LocalRepoFiles(tmp_path, max_bytes=100).read_text(str(tmp_path / "x"))

    async def test_rejects_symlink_escaping_root(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        os.symlink(outside, root / "link.txt")
        with pytest.raises(RepoFileError):
            await LocalRepoFiles(root, max_bytes=100).read_text("link.txt")

    async def test_allows_symlink_inside_root(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("ok")
        os.symlink(tmp_path / "real.txt", tmp_path / "alias.txt")
        assert await LocalRepoFiles(tmp_path, max_bytes=100).read_text("alias.txt") == "ok"

    async def test_refuses_file_over_size_cap(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * 101)
        with pytest.raises(RepoFileError, match="max"):
            await LocalRepoFiles(tmp_path, max_bytes=100).read_text("big.txt")

    async def test_file_at_cap_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "ok.txt").write_text("x" * 100)
        assert len(await LocalRepoFiles(tmp_path, max_bytes=100).read_text("ok.txt") or "") == 100

    async def test_glob_sorted_and_relative_posix(self, tmp_path: Path) -> None:
        (tmp_path / "config").mkdir()
        for n in ("us.env", "eu.env", "ap.env"):
            (tmp_path / "config" / n).write_text("A=1")
        (tmp_path / "config" / "notes.txt").write_text("x")
        got = await LocalRepoFiles(tmp_path, max_bytes=100).glob("config/*.env")
        assert got == ["config/ap.env", "config/eu.env", "config/us.env"]

    async def test_glob_skips_git_and_node_modules(self, tmp_path: Path) -> None:
        for d in (".git", "node_modules", "pkg"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "values.yaml").write_text("a: 1")
        got = await LocalRepoFiles(tmp_path, max_bytes=100).glob("**/values.yaml")
        assert got == ["pkg/values.yaml"]

    async def test_glob_excludes_escaping_symlinks_and_oversize(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        (root / "config").mkdir(parents=True)
        outside = tmp_path / "o.env"
        outside.write_text("A=1")
        os.symlink(outside, root / "config" / "leak.env")
        (root / "config" / "big.env").write_text("A=" + "1" * 200)
        (root / "config" / "ok.env").write_text("A=1")
        got = await LocalRepoFiles(root, max_bytes=100).glob("config/*.env")
        assert got == ["config/ok.env"]

    def test_is_not_remote(self, tmp_path: Path) -> None:
        assert LocalRepoFiles(tmp_path, max_bytes=1).is_remote is False


class _FakeGh:
    """Scripted ``run_gh`` replacement; records every argv."""

    def __init__(
        self, tree: list[dict] | None = None, contents: dict[str, str] | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self.tree = tree or []
        self.contents = contents or {}
        self.fail_next: list[GhError] = []

    async def __call__(self, argv, timeout: float = 15.0) -> str:  # noqa: ASYNC109
        argv = list(argv)
        self.calls.append(argv)
        if self.fail_next:
            raise self.fail_next.pop(0)
        endpoint = next(a for a in argv if a.startswith("repos/"))
        if "/git/trees/" in endpoint:
            return json.dumps({"tree": self.tree, "truncated": False})
        path = endpoint.split("/contents/", 1)[1].split("?")[0]
        if path not in self.contents:
            raise GhError(1, "gh: Not Found (HTTP 404)")
        raw = self.contents[path].encode()
        b64 = base64.encodebytes(raw).decode()
        return json.dumps({"content": b64, "encoding": "base64", "size": len(raw)})


def _gh(fake: _FakeGh, host: str = "github.com", **kw) -> GitHubRepoFiles:
    return GitHubRepoFiles("acme", "svc", host, runner=fake, **kw)


class TestGitHubRepoFiles:
    async def test_read_decodes_base64(self) -> None:
        fake = _FakeGh(contents={"CODEOWNERS": "* @acme/dlvr\nline two\n"})
        assert await _gh(fake).read_text("CODEOWNERS") == "* @acme/dlvr\nline two\n"
        assert "repos/acme/svc/contents/CODEOWNERS" in " ".join(fake.calls[0])

    async def test_404_is_none(self) -> None:
        assert await _gh(_FakeGh()).read_text("missing") is None

    async def test_hostname_added_for_enterprise_host(self) -> None:
        fake = _FakeGh(contents={"a": "x"})
        await _gh(fake, host="ghe.example.com").read_text("a")
        argv = fake.calls[0]
        assert "--hostname" in argv
        assert argv[argv.index("--hostname") + 1] == "ghe.example.com"

    async def test_no_hostname_for_github_com(self) -> None:
        fake = _FakeGh(contents={"a": "x"})
        await _gh(fake).read_text("a")
        assert "--hostname" not in fake.calls[0]

    async def test_ref_is_passed_on_contents(self) -> None:
        fake = _FakeGh(contents={"a": "x"})
        await _gh(fake, ref="release").read_text("a")
        assert any("ref=release" in a for a in fake.calls[0])

    async def test_tree_fetched_once_for_many_globs(self) -> None:
        tree = [
            {"path": "config", "type": "tree"},
            {"path": "config/us.env", "type": "blob", "size": 10},
            {"path": "config/eu.env", "type": "blob", "size": 10},
            {"path": "README.md", "type": "blob", "size": 10},
        ]
        fake = _FakeGh(tree=tree)
        files = _gh(fake)
        assert await files.glob("config/*.env") == ["config/eu.env", "config/us.env"]
        assert await files.glob("README.md") == ["README.md"]
        tree_calls = [c for c in fake.calls if any("/git/trees/" in a for a in c)]
        assert len(tree_calls) == 1
        assert any("recursive=1" in a for a in tree_calls[0])

    async def test_glob_excludes_directories_and_oversize(self) -> None:
        tree = [
            {"path": "config/a.env", "type": "tree"},
            {"path": "config/big.env", "type": "blob", "size": 999},
            {"path": "config/ok.env", "type": "blob", "size": 5},
        ]
        got = await _gh(_FakeGh(tree=tree), max_bytes=100).glob("config/*.env")
        assert got == ["config/ok.env"]

    async def test_oversize_read_refused(self) -> None:
        fake = _FakeGh(contents={"big": "x" * 50})
        with pytest.raises(RepoFileError):
            await _gh(fake, max_bytes=10).read_text("big")

    async def test_rejects_traversal(self) -> None:
        with pytest.raises(RepoFileError):
            await _gh(_FakeGh()).read_text("../x")

    async def test_non_404_error_retried_once_then_succeeds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = _FakeGh(contents={"a": "ok"})
        fake.fail_next = [GhError(1, "gh: server error (HTTP 502)")]
        with caplog.at_level("WARNING"):
            assert await _gh(fake).read_text("a") == "ok"
        assert len(fake.calls) == 2
        assert any(r.levelname == "WARNING" for r in caplog.records)

    async def test_non_404_error_raised_after_one_retry(self) -> None:
        fake = _FakeGh(contents={"a": "ok"})
        fake.fail_next = [GhError(1, "gh: boom (HTTP 500)"), GhError(1, "gh: boom (HTTP 500)")]
        with pytest.raises(GhError):
            await _gh(fake).read_text("a")
        assert len(fake.calls) == 2

    async def test_404_not_retried(self) -> None:
        fake = _FakeGh()
        assert await _gh(fake).read_text("nope") is None
        assert len(fake.calls) == 1

    def test_is_remote(self) -> None:
        assert _gh(_FakeGh()).is_remote is True


class TestInMemoryRepoFiles:
    async def test_read_and_glob_sorted(self) -> None:
        files = InMemoryRepoFiles({"config/b.env": "B", "config/a.env": "A", "x": "X"})
        assert await files.read_text("config/a.env") == "A"
        assert await files.read_text("nope") is None
        assert await files.glob("config/*.env") == ["config/a.env", "config/b.env"]
        assert files.is_remote is False
