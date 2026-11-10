"""sources.local.allowed_roots enforcement: production requires roots, everywhere local paths enter."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.adapters.git import GitAdapter, GitAdapterError
from bifrost.api.app import create_app
from bifrost.api.deps import get_repository
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import LocalSourceConfig, SourcesConfig
from bifrost.core.local_roots import (
    LocalPathNotAllowedError,
    ensure_local_path_allowed,
    local_path_allowed,
    local_path_from_url,
)
from bifrost.research.ownership_sync import resolve_repo_files
from bifrost.storage.repository import EntityRepository


def _settings(roots: list[str] | None = None, *, production: bool = False) -> BifrostSettings:
    return BifrostSettings(
        production_mode=production,
        sources=SourcesConfig(local=LocalSourceConfig(allowed_roots=roots or [])),
    )


class TestPolicy:
    def test_dev_without_roots_allows(self, tmp_path: Path) -> None:
        assert local_path_allowed(tmp_path, _settings()) is True

    def test_production_without_roots_refuses(self, tmp_path: Path) -> None:
        assert local_path_allowed(tmp_path, _settings(production=True)) is False
        with pytest.raises(LocalPathNotAllowedError, match="allowed_roots"):
            ensure_local_path_allowed(tmp_path, _settings(production=True))

    @pytest.mark.parametrize("production", [False, True])
    def test_inside_root_allowed_outside_refused(self, tmp_path: Path, production: bool) -> None:
        root = tmp_path / "root"
        (root / "svc").mkdir(parents=True)
        s = _settings([str(root)], production=production)
        assert local_path_allowed(root / "svc", s) is True
        assert local_path_allowed(tmp_path / "other", s) is False

    def test_dotdot_and_symlink_escape_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        s = _settings([str(root)])
        assert local_path_allowed(root / ".." / "outside", s) is False
        assert local_path_allowed(root / "link", s) is False

    def test_sibling_prefix_is_not_inside(self, tmp_path: Path) -> None:
        (tmp_path / "root").mkdir()
        (tmp_path / "root-evil").mkdir()
        assert (
            local_path_allowed(tmp_path / "root-evil", _settings([str(tmp_path / "root")])) is False
        )

    def test_url_forms(self, tmp_path: Path) -> None:
        assert local_path_from_url(str(tmp_path)) == tmp_path
        assert local_path_from_url(tmp_path.as_uri()) == tmp_path
        assert local_path_from_url("https://github.com/o/r") is None
        assert local_path_from_url("git@github.com:o/r.git") is None


class TestResolveRepoFiles:
    def test_production_without_roots_refuses_local_url(self, tmp_path: Path) -> None:
        assert resolve_repo_files(str(tmp_path), None, _settings(production=True)) is None

    def test_production_with_roots_allows_inside(self, tmp_path: Path) -> None:
        s = _settings([str(tmp_path)], production=True)
        assert resolve_repo_files(str(tmp_path), None, s) is not None


class TestGitClone:
    async def test_production_without_roots_refuses_local_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = GitAdapter(settings=_settings(production=True))

        async def boom(args, *, cwd=None):  # noqa: ANN001
            raise AssertionError("git must not run")

        monkeypatch.setattr(adapter, "_run_git", boom)
        with pytest.raises(GitAdapterError, match="allowed_roots"):
            await adapter.clone(str(tmp_path), str(tmp_path / "dest"))

    async def test_outside_roots_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        adapter = GitAdapter(settings=_settings([str(root)]))

        async def boom(args, *, cwd=None):  # noqa: ANN001
            raise AssertionError("git must not run")

        monkeypatch.setattr(adapter, "_run_git", boom)
        with pytest.raises(GitAdapterError, match="allowed_roots"):
            await adapter.clone(str(tmp_path / "elsewhere"), str(tmp_path / "dest"))


@pytest.fixture
def app(db_session: AsyncSession):
    application = create_app()
    db_session.commit = db_session.flush  # routes now commit; keep the test rollback-only

    async def _override_repo():
        yield EntityRepository(db_session)

    application.dependency_overrides[get_repository] = _override_repo
    return application


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _patch_route_settings(monkeypatch: pytest.MonkeyPatch, **update: object) -> None:
    base = get_settings()
    patched = base.model_copy(update=update)
    monkeypatch.setattr("bifrost.api.routes.repos.get_settings", lambda: patched)


class TestRegistration:
    async def test_production_without_roots_refuses_local_path(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_route_settings(monkeypatch, production_mode=True)
        resp = await client.post(
            "/api/v1/repos", json={"name": f"loc-{uuid.uuid4().hex[:6]}", "url": str(tmp_path)}
        )
        assert resp.status_code == 400
        assert "allowed_roots" in resp.text

    async def test_outside_roots_refused(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _patch_route_settings(
            monkeypatch,
            sources=SourcesConfig(local=LocalSourceConfig(allowed_roots=[str(root)])),
        )
        resp = await client.post(
            "/api/v1/repos",
            json={"name": f"loc-{uuid.uuid4().hex[:6]}", "url": str(tmp_path / "other")},
        )
        assert resp.status_code == 400

    async def test_github_url_unaffected_in_production(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_route_settings(monkeypatch, production_mode=True)
        resp = await client.post(
            "/api/v1/repos",
            json={"name": f"gh-{uuid.uuid4().hex[:6]}", "url": "https://github.com/acme/svc"},
        )
        assert resp.status_code == 201
