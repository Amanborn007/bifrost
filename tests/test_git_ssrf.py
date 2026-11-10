"""SSRF guard for git URLs: literal-IP checks, resolve-then-check, scheme rules.

No network: the resolver seam ``bifrost.adapters.git._resolve_host`` is monkeypatched.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from bifrost.adapters import git as git_mod
from bifrost.adapters.git import (
    GitAdapter,
    GitAdapterError,
    GitCloneError,
    _assert_resolves_public,
    _validate_repo_url,
)
from bifrost.cli.config import reset_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", raising=False)
    monkeypatch.delenv("BIFROST_PRODUCTION_MODE", raising=False)
    reset_settings()
    yield
    reset_settings()


def _resolver(monkeypatch: pytest.MonkeyPatch, addrs: list[str] | Exception):
    calls: list[tuple[str, int | None]] = []

    def fake(host: str, port: int | None = None) -> list[str]:
        calls.append((host, port))
        if isinstance(addrs, Exception):
            raise addrs
        return addrs

    monkeypatch.setattr(git_mod, "_resolve_host", fake)
    return calls


class TestLiteralHosts:
    @pytest.mark.parametrize(
        "url",
        [
            "https://[::1]/o/r.git",
            "https://[::]/o/r.git",
            "https://[fe80::1]/o/r.git",
            "https://[febf::1]/o/r.git",
            "https://[fc00::1]/o/r.git",
            "https://[fd12:3456:789a::1]/o/r.git",
            "https://[::ffff:10.0.0.1]/o/r.git",
            "https://[::ffff:127.0.0.1]/o/r.git",
            "https://[::ffff:169.254.169.254]/o/r.git",
            "https://[64:ff9b::7f00:1]/o/r.git",
            "https://[fd00:ec2::254]/o/r.git",
            "https://0.0.0.0/o/r.git",
            "https://100.64.0.1/o/r.git",
            "https://100.127.255.254/o/r.git",
            "https://169.254.169.254/latest/meta-data/",
            "https://2130706433/o/r.git",
            "https://0x7f000001/o/r.git",
            "https://127.1/o/r.git",
            "https://github.com@127.0.0.1/o/r.git",
            "https://localhost./o/r.git",
            "https://svc.internal/o/r.git",
            "ssh://git@[::1]/o/r.git",
            "ssh://git@100.64.1.1/o/r.git",
        ],
    )
    def test_blocked(self, url: str) -> None:
        with pytest.raises(GitAdapterError, match="SSRF|private/loopback"):
            _validate_repo_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://8.8.8.8/o/r.git",
            "https://[2606:4700:4700::1111]/o/r.git",
            "https://100.128.0.1/o/r.git",
            "https://100.63.255.255/o/r.git",
            "ssh://git@github.com/o/r.git",
            "git@github.com:o/r.git",
        ],
    )
    def test_public_allowed(self, url: str) -> None:
        _validate_repo_url(url)


class TestSchemes:
    def test_plain_http_rejected_for_public_host(self) -> None:
        with pytest.raises(GitAdapterError, match="https"):
            _validate_repo_url("http://github.com/o/r.git")

    def test_http_allowed_only_under_private_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", "true")
        _validate_repo_url("http://localhost:3000/o/r.git")

    def test_local_path_not_a_network_scheme(self) -> None:
        _validate_repo_url("/srv/repos/svc")


class TestProductionOptOut:
    def test_opt_in_refused_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", "true")
        monkeypatch.setenv("BIFROST_PRODUCTION_MODE", "true")
        reset_settings()
        with pytest.raises(GitAdapterError, match="production"):
            _validate_repo_url("https://10.0.0.5/o/r.git")

    def test_opt_in_refused_in_production_even_for_public_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", "true")
        monkeypatch.setenv("BIFROST_PRODUCTION_MODE", "true")
        reset_settings()
        with pytest.raises(GitAdapterError, match="production"):
            _validate_repo_url("https://github.com/o/r.git")

    def test_opt_in_still_works_outside_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", "true")
        _validate_repo_url("https://10.0.0.5/o/r.git")


class TestResolveThenCheck:
    async def test_public_resolution_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _resolver(monkeypatch, ["93.184.216.34", "2606:2800:220:1::1"])
        await _assert_resolves_public("https://example.com/o/r.git")
        assert calls == [("example.com", None)] or calls == [("example.com", 443)]

    async def test_any_private_address_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _resolver(monkeypatch, ["93.184.216.34", "10.0.0.5"])
        with pytest.raises(GitAdapterError, match="resolves"):
            await _assert_resolves_public("https://rebind.example.com/o/r.git")

    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "::1",
            "fe80::1%eth0",
            "fc00::1",
            "::ffff:169.254.169.254",
            "100.64.0.9",
            "0.0.0.0",
            "64:ff9b::a00:1",
        ],
    )
    async def test_each_blocked_family(self, monkeypatch: pytest.MonkeyPatch, addr: str) -> None:
        _resolver(monkeypatch, [addr])
        with pytest.raises(GitAdapterError, match="resolves"):
            await _assert_resolves_public("https://x.example.com/o/r.git")

    async def test_scp_form_host_is_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _resolver(monkeypatch, ["10.1.2.3"])
        with pytest.raises(GitAdapterError, match="resolves"):
            await _assert_resolves_public("git@git.example.com:o/r.git")
        assert calls[0][0] == "git.example.com"

    async def test_unresolvable_host_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _resolver(monkeypatch, socket.gaierror(-2, "Name or service not known"))
        with pytest.raises(GitCloneError):
            await _assert_resolves_public("https://nx.example.com/o/r.git")

    async def test_empty_resolution_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _resolver(monkeypatch, [])
        with pytest.raises(GitCloneError):
            await _assert_resolves_public("https://nx.example.com/o/r.git")

    async def test_literal_ip_and_local_path_skip_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _resolver(monkeypatch, AssertionError("resolver must not be called"))
        await _assert_resolves_public("https://8.8.8.8/o/r.git")
        await _assert_resolves_public("/srv/repos/svc")
        assert calls == []

    async def test_opt_in_skips_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_ALLOW_PRIVATE_GIT_URLS", "true")
        calls = _resolver(monkeypatch, AssertionError("resolver must not be called"))
        await _assert_resolves_public("https://internal.example.com/o/r.git")
        assert calls == []


class TestCloneUsesGuard:
    async def test_clone_checks_resolution_before_running_git(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _resolver(monkeypatch, ["10.0.0.5"])
        adapter = GitAdapter()
        ran: list[list[str]] = []

        async def fake_run(args, *, cwd=None):  # noqa: ANN001
            ran.append(args)
            raise AssertionError("git must not run for a rebinding URL")

        monkeypatch.setattr(adapter, "_run_git", fake_run)
        with pytest.raises(GitAdapterError, match="resolves"):
            await adapter.clone("https://rebind.example.com/o/r.git", str(tmp_path / "d"))
        assert ran == []
