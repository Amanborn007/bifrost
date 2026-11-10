"""Manifest wiring that needs no database: source resolution, ownership mapping,
refresh coalescing, org-catalog behaviour with no configured orgs, route shape."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path

import pytest

from bifrost.api.routes import admin as admin_routes
from bifrost.api.routes import catalog as catalog_routes
from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import (
    GitHubSourceConfig,
    LocalSourceConfig,
    SourcesConfig,
)
from bifrost.manifest.files import GitHubRepoFiles, LocalRepoFiles
from bifrost.manifest.graph_apply import GraphRefresher
from bifrost.manifest.model import ServiceManifest
from bifrost.research import org_catalog
from bifrost.research.ownership_sync import manifest_to_ownership_values, resolve_repo_files
from bifrost.storage.models import RepoOwnership
from tests import leak_terms


def _settings(*, roots: list[str] | None = None, host: str = "github.com") -> BifrostSettings:
    return BifrostSettings(
        sources=SourcesConfig(
            github=GitHubSourceConfig(host=host),
            local=LocalSourceConfig(allowed_roots=roots or []),
        )
    )


class TestResolveRepoFiles:
    def test_clone_path_wins_over_url(self, tmp_path: Path) -> None:
        clone = tmp_path / "clone"
        clone.mkdir()
        files = resolve_repo_files("https://github.com/acme/svc", str(clone), _settings())
        assert isinstance(files, LocalRepoFiles)
        assert files.root == clone.resolve()

    def test_missing_clone_path_falls_through_to_url(self) -> None:
        files = resolve_repo_files("https://github.com/acme/svc", "/no/such/dir", _settings())
        assert isinstance(files, GitHubRepoFiles)

    def test_local_directory_url_without_allowed_roots(self, tmp_path: Path) -> None:
        files = resolve_repo_files(str(tmp_path), None, _settings())
        assert isinstance(files, LocalRepoFiles)
        assert files.root == tmp_path.resolve()

    def test_file_scheme_url(self, tmp_path: Path) -> None:
        files = resolve_repo_files(tmp_path.as_uri(), None, _settings())
        assert isinstance(files, LocalRepoFiles)

    def test_local_directory_inside_allowed_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "root" / "svc"
        repo.mkdir(parents=True)
        files = resolve_repo_files(str(repo), None, _settings(roots=[str(tmp_path / "root")]))
        assert isinstance(files, LocalRepoFiles)

    def test_local_directory_outside_allowed_roots_is_rejected(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        assert resolve_repo_files(str(outside), None, _settings(roots=[str(allowed)])) is None

    def test_dotdot_cannot_escape_allowed_root(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (tmp_path / "outside").mkdir()
        sneaky = f"{allowed}/../outside"
        assert resolve_repo_files(sneaky, None, _settings(roots=[str(allowed)])) is None

    def test_github_url_uses_configured_host(self) -> None:
        files = resolve_repo_files(
            "https://git.acme.internal/platform/svc.git", None, _settings(host="git.acme.internal")
        )
        assert isinstance(files, GitHubRepoFiles)
        assert (files.owner, files.name, files.host) == ("platform", "svc", "git.acme.internal")

    def test_other_host_is_none(self) -> None:
        assert resolve_repo_files("https://gitlab.com/acme/svc", None, _settings()) is None

    def test_garbage_is_none(self) -> None:
        assert resolve_repo_files("not a url", None, _settings()) is None
        assert resolve_repo_files("https://github.com/only-owner", None, _settings()) is None


class TestOwnershipValues:
    def test_every_key_is_a_ownership_column(self) -> None:
        md = ServiceManifest(
            name="svc",
            team="dlvr",
            owners=["a"],
            tier="1",
            data_classification="internal",
            compliance=["hipaa", "soc2"],
            jira_project_key="DLVR",
            confluence_space_key="DLVR",
            slack_channel="#dlvr",
            downstream_services={"peer": "peer.acme"},
            messaging_topics={"q": "queue"},
            database_names=["db"],
            providers=["backstage", "codeowners"],
            source_files=["catalog-info.yaml", "CODEOWNERS"],
        )
        values = manifest_to_ownership_values(uuid.uuid4(), md)
        columns = {c.name for c in RepoOwnership.__table__.columns}
        assert set(values) <= columns
        assert values["squad"] == "dlvr"
        assert values["service_tier"] == "1"
        assert values["compliance_tags"] == ["hipaa", "soc2"]
        assert values["raw_manifest"] == {
            "providers": ["backstage", "codeowners"],
            "files": ["catalog-info.yaml", "CODEOWNERS"],
            "owner": "a",
        }
        assert values["source"] == "backstage+codeowners"

    def test_empty_manifest_is_missing(self) -> None:
        values = manifest_to_ownership_values(uuid.uuid4(), ServiceManifest())
        assert values["source"] == "missing"
        assert values["compliance_tags"] == []
        assert values["raw_manifest"] == {"providers": [], "files": []}
        assert set(values) <= {c.name for c in RepoOwnership.__table__.columns}


class TestGraphRefresher:
    async def test_ten_concurrent_requests_coalesce(self) -> None:
        calls = 0

        async def rebuild() -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)

        refresher = GraphRefresher(rebuild)
        await asyncio.gather(*(refresher.request() for _ in range(10)))
        assert 1 <= calls <= 2

    async def test_sequential_requests_each_rebuild(self) -> None:
        calls = 0

        async def rebuild() -> None:
            nonlocal calls
            calls += 1

        refresher = GraphRefresher(rebuild)
        await refresher.request()
        await refresher.request()
        assert calls == 2

    async def test_rebuild_failure_propagates_and_refresher_recovers(self) -> None:
        state = {"fail": True, "calls": 0}

        async def rebuild() -> None:
            state["calls"] += 1
            if state["fail"]:
                raise RuntimeError("boom")

        refresher = GraphRefresher(rebuild)
        with pytest.raises(RuntimeError, match="boom"):
            await refresher.request()
        state["fail"] = False
        await refresher.request()
        assert state["calls"] == 2


class TestOrgCatalogWithoutOrgs:
    async def test_no_orgs_makes_no_gh_calls_and_reports_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        async def fake_gh(*args: object, **kwargs: object) -> str:
            calls.append(args)
            raise AssertionError("gh must not be called when no orgs are configured")

        monkeypatch.setattr(org_catalog, "run_gh", fake_gh)
        result = await org_catalog.sync_org_catalog(
            session_factory=None,  # type: ignore[arg-type]
            settings=_settings(),
        )
        assert calls == []
        assert result.total_fetched == 0
        assert "no sources.github.orgs configured" in result.errors

    def test_no_default_org_constant(self) -> None:
        assert not hasattr(org_catalog, "DEFAULT_ORG")
        assert not leak_terms.matches(inspect.getsource(org_catalog))


class TestRoutes:
    def test_catalog_has_no_org_default(self) -> None:
        sig = inspect.signature(catalog_routes.trigger_catalog_sync)
        assert sig.parameters["org"].default is None
        assert not leak_terms.matches(inspect.getsource(catalog_routes))

    def test_admin_exposes_index_services_and_no_banned_path(self) -> None:
        paths = {getattr(r, "path", "") for r in admin_routes.router.routes}
        assert "/admin/index-services" in paths
        assert [p for p in paths if leak_terms.matches(p)] == []
