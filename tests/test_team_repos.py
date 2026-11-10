"""Team repo discovery: gh code search for provider manifest files, verified by loading each manifest."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from bifrost.cli.config import BifrostSettings
from bifrost.errors import ConfigError, UpstreamError, ValidationError
from bifrost.manifest.files import GhError, InMemoryRepoFiles

BACKSTAGE_ORDERS = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: order-svc
spec:
  owner: group:default/orders
  lifecycle: production
"""
BACKSTAGE_BILLING = BACKSTAGE_ORDERS.replace("orders", "billing")
CODEOWNERS_ORDERS = "# owners\n* @acme/orders\n/docs/ @acme/docs\n"
CODEOWNERS_BILLING = "* @acme/billing\n"

SEARCH_JQ = ".items[].repository.full_name"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIFROST_CONFIG", "")


def _settings(
    *,
    orgs: tuple[str, ...] = ("acme",),
    host: str = "github.com",
    manifest: dict[str, Any] | None = None,
) -> BifrostSettings:
    kw: dict[str, Any] = {"sources": {"github": {"host": host, "orgs": list(orgs)}}}
    if manifest is not None:
        kw["manifest"] = manifest
    return BifrostSettings(**kw)


class FakeGh:
    """Stands in for ``run_gh``: answers code searches and repo metadata, records every argv."""

    def __init__(
        self,
        search: dict[tuple[str, str], list[str] | Exception] | None = None,
        repos: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.search = search or {}
        self.repos = repos or {}
        self.calls: list[list[str]] = []

    @property
    def searches(self) -> list[list[str]]:
        return [a for a in self.calls if "search/code" in a]

    @staticmethod
    def query(argv: list[str]) -> str:
        return next(a for a in argv if a.startswith("q="))[2:]

    async def __call__(self, argv, timeout: float = 15.0) -> str:  # noqa: ASYNC109
        argv = list(argv)
        self.calls.append(argv)
        if "search/code" in argv:
            m = re.search(r"org:(\S+) filename:(\S+)$", self.query(argv))
            assert m, argv
            found = self.search.get((m.group(1), m.group(2)), [])
            if isinstance(found, Exception):
                raise found
            return "\n".join(found) + ("\n" if found else "")
        endpoint = argv[-1]
        assert endpoint.startswith("repos/"), argv
        full = endpoint.removeprefix("repos/")
        if full not in self.repos:
            raise GhError(1, "gh: Not Found (HTTP 404)")
        return json.dumps(self.repos[full])


def _meta(**over: Any) -> dict[str, Any]:
    return {"description": "a service", "fork": False, "archived": False, **over}


def _factory(files: dict[str, dict[str, str]], seen_hosts: list[str] | None = None):
    def make(owner: str, name: str, host: str = "github.com"):
        if seen_hosts is not None:
            seen_hosts.append(host)
        return InMemoryRepoFiles(files.get(f"{owner}/{name}", {}))

    return make


async def _discover(team: str, gh: FakeGh, files: dict[str, dict[str, str]], settings=None, **kw):
    from bifrost.manifest.team_repos import discover_team_repos

    return await discover_team_repos(
        team,
        settings=settings or _settings(),
        runner=gh,
        files_factory=_factory(files),
        **kw,
    )


# ── matching ────────────────────────────────────────────────────────────


async def test_backstage_owner_match() -> None:
    gh = FakeGh(
        search={("acme", "catalog-info.yaml"): ["acme/order-svc"]},
        repos={"acme/order-svc": _meta(description="Places orders")},
    )
    files = {"acme/order-svc": {"catalog-info.yaml": BACKSTAGE_ORDERS}}
    got = await _discover("orders", gh, files)
    assert len(got) == 1
    m = got[0]
    assert (m.owner, m.name, m.full_name) == ("acme", "order-svc", "acme/order-svc")
    assert m.team == "orders"
    assert m.description == "Places orders"
    assert m.providers == ["backstage"]
    assert m.git_url == "https://github.com/acme/order-svc.git"


async def test_codeowners_team_handle_match() -> None:
    gh = FakeGh(
        search={("acme", "CODEOWNERS"): ["acme/inventory"]},
        repos={"acme/inventory": _meta()},
    )
    files = {"acme/inventory": {".github/CODEOWNERS": CODEOWNERS_ORDERS}}
    got = await _discover("orders", gh, files)
    assert [m.full_name for m in got] == ["acme/inventory"]
    assert got[0].providers == ["codeowners"]


async def test_other_teams_are_excluded() -> None:
    gh = FakeGh(
        search={
            ("acme", "catalog-info.yaml"): ["acme/order-svc", "acme/bill-svc"],
            ("acme", "CODEOWNERS"): ["acme/inventory", "acme/bill-inventory"],
        },
        repos={
            n: _meta()
            for n in ("acme/order-svc", "acme/bill-svc", "acme/inventory", "acme/bill-inventory")
        },
    )
    files = {
        "acme/order-svc": {"catalog-info.yaml": BACKSTAGE_ORDERS},
        "acme/bill-svc": {"catalog-info.yaml": BACKSTAGE_BILLING},
        "acme/inventory": {"CODEOWNERS": CODEOWNERS_ORDERS},
        "acme/bill-inventory": {"CODEOWNERS": CODEOWNERS_BILLING},
    }
    got = await _discover("orders", gh, files)
    assert [m.full_name for m in got] == ["acme/inventory", "acme/order-svc"]


async def test_team_match_ignores_case() -> None:
    gh = FakeGh(
        search={("acme", "catalog-info.yaml"): ["acme/order-svc"]},
        repos={"acme/order-svc": _meta()},
    )
    files = {"acme/order-svc": {"catalog-info.yaml": BACKSTAGE_ORDERS}}
    got = await _discover("ORDERS", gh, files)
    assert [m.full_name for m in got] == ["acme/order-svc"]


async def test_a_search_hit_without_a_manifest_is_not_a_match() -> None:
    gh = FakeGh(search={("acme", "CODEOWNERS"): ["acme/empty"]}, repos={"acme/empty": _meta()})
    assert await _discover("orders", gh, {"acme/empty": {"README.md": "hi"}}) == []


async def test_a_repo_found_by_two_searches_is_returned_once_with_both_providers() -> None:
    gh = FakeGh(
        search={
            ("acme", "catalog-info.yaml"): ["acme/order-svc"],
            ("acme", "CODEOWNERS"): ["acme/order-svc"],
        },
        repos={"acme/order-svc": _meta()},
    )
    files = {
        "acme/order-svc": {
            "catalog-info.yaml": BACKSTAGE_ORDERS,
            "CODEOWNERS": CODEOWNERS_ORDERS,
        }
    }
    got = await _discover("orders", gh, files)
    assert len(got) == 1
    assert got[0].providers == ["backstage", "codeowners"]


async def test_forks_and_archived_repos_are_skipped() -> None:
    gh = FakeGh(
        search={("acme", "catalog-info.yaml"): ["acme/live", "acme/forked", "acme/old"]},
        repos={
            "acme/live": _meta(),
            "acme/forked": _meta(fork=True),
            "acme/old": _meta(archived=True),
        },
    )
    files = {
        n: {"catalog-info.yaml": BACKSTAGE_ORDERS} for n in ("acme/live", "acme/forked", "acme/old")
    }
    got = await _discover("orders", gh, files)
    assert [m.full_name for m in got] == ["acme/live"]


# ── what is sent to gh ──────────────────────────────────────────────────


async def test_github_com_searches_carry_no_hostname() -> None:
    gh = FakeGh()
    await _discover("orders", gh, {})
    assert gh.searches
    assert all("--hostname" not in a for a in gh.searches)


async def test_non_default_host_is_passed_as_hostname_and_used_in_git_url() -> None:
    gh = FakeGh(
        search={("acme", "catalog-info.yaml"): ["acme/order-svc"]},
        repos={"acme/order-svc": _meta()},
    )
    files = {"acme/order-svc": {"catalog-info.yaml": BACKSTAGE_ORDERS}}
    got = await _discover("orders", gh, files, settings=_settings(host="ghe.acme.com"))
    assert got[0].git_url == "https://ghe.acme.com/acme/order-svc.git"
    for argv in gh.calls:
        i = argv.index("--hostname")
        assert argv[i + 1] == "ghe.acme.com"


async def test_search_argv_shape() -> None:
    gh = FakeGh()
    await _discover("orders", gh, {}, search_limit=7)
    argv = gh.searches[0]
    assert argv[0] == "api"
    assert argv[argv.index("-X") + 1] == "GET"
    assert "search/code" in argv
    assert "per_page=7" in argv
    assert argv[argv.index("--jq") + 1] == SEARCH_JQ
    assert gh.query(argv) == "orders org:acme filename:catalog-info.yaml"


async def test_one_search_per_org_per_filename() -> None:
    gh = FakeGh()
    await _discover("orders", gh, {}, settings=_settings(orgs=("acme", "acme-labs")))
    queries = sorted(gh.query(a) for a in gh.searches)
    assert queries == sorted(
        f"orders org:{org} filename:{fn}"
        for org in ("acme", "acme-labs")
        for fn in ("catalog-info.yaml", "catalog-info.yml", "CODEOWNERS")
    )


async def test_search_filenames_come_from_the_enabled_providers_paths() -> None:
    settings = _settings(
        manifest={
            "providers": ["backstage", "generic_yaml"],
            "backstage": {"paths": ["deploy/service.yaml", "catalog-info.yaml"]},
            "generic_yaml": {"path": "meta/team.yml"},
        }
    )
    gh = FakeGh()
    await _discover("orders", gh, {}, settings=settings)
    filenames = sorted(gh.query(a).rsplit("filename:", 1)[1] for a in gh.searches)
    assert filenames == ["catalog-info.yaml", "service.yaml", "team.yml"]


async def test_codeowners_only_searches_codeowners_once() -> None:
    gh = FakeGh()
    await _discover("orders", gh, {}, settings=_settings(manifest={"providers": ["codeowners"]}))
    assert [gh.query(a).rsplit("filename:", 1)[1] for a in gh.searches] == ["CODEOWNERS"]


# ── failures ────────────────────────────────────────────────────────────


async def test_no_orgs_configured_is_a_400_config_error() -> None:
    with pytest.raises(ConfigError) as exc:
        await _discover("orders", FakeGh(), {}, settings=_settings(orgs=()))
    assert exc.value.http_status == 400
    assert "orgs" in str(exc.value)


async def test_no_manifest_provider_enabled_is_a_config_error() -> None:
    gh = FakeGh()
    with pytest.raises(ConfigError):
        await _discover("orders", gh, {}, settings=_settings(manifest={"providers": ["none"]}))
    assert gh.calls == []


async def test_blank_team_is_a_validation_error() -> None:
    gh = FakeGh()
    with pytest.raises(ValidationError):
        await _discover("   ", gh, {})
    assert gh.calls == []


async def test_every_search_failing_is_an_upstream_error() -> None:
    boom = GhError(1, "HTTP 401: Bad credentials")
    gh = FakeGh(
        search={
            ("acme", "catalog-info.yaml"): boom,
            ("acme", "catalog-info.yml"): boom,
            ("acme", "CODEOWNERS"): boom,
        }
    )
    with pytest.raises(UpstreamError) as exc:
        await _discover("orders", gh, {})
    assert "Bad credentials" in str(exc.value)


async def test_one_failing_search_does_not_hide_the_others() -> None:
    gh = FakeGh(
        search={
            ("acme", "catalog-info.yaml"): GhError(1, "HTTP 422"),
            ("acme", "CODEOWNERS"): ["acme/inventory"],
        },
        repos={"acme/inventory": _meta()},
    )
    files = {"acme/inventory": {"CODEOWNERS": CODEOWNERS_ORDERS}}
    got = await _discover("orders", gh, files)
    assert [m.full_name for m in got] == ["acme/inventory"]
