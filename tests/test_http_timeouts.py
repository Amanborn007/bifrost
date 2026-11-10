"""Every outbound HTTP client gets an explicit timeout from ``http_timeout_seconds``."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import bifrost
from bifrost.cli.config import BifrostSettings
from bifrost.core.http import http_timeout

SRC = Path(bifrost.__file__).parent

_CLIENT_CTORS = {
    ("httpx", "Client"),
    ("httpx", "AsyncClient"),
    ("httpx", "get"),
    ("httpx", "post"),
    ("httpx", "request"),
    ("httpx", "stream"),
    ("aiohttp", "ClientSession"),
    ("aiohttp", "ClientTimeout"),
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "request"),
    ("requests", "Session"),
    ("urllib.request", "urlopen"),
}


def _dotted(node: ast.expr) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute):
        parts: list[str] = [node.attr]
        cur = node.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            mod = ".".join([cur.id, *reversed(parts[1:])])
            return mod, parts[0]
    return None


def _client_calls() -> list[tuple[Path, ast.Call]]:
    found: list[tuple[Path, ast.Call]] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func) in _CLIENT_CTORS:
                found.append((path, node))
    return found


def test_there_are_client_sites_to_check() -> None:
    assert len(_client_calls()) >= 5


def test_every_outbound_client_has_a_setting_driven_timeout() -> None:
    bad: list[str] = []
    for path, call in _client_calls():
        if _dotted(call.func) == ("aiohttp", "ClientTimeout"):
            continue
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        value = kw.get("timeout")
        where = f"{path.relative_to(SRC)}:{call.lineno}"
        if value is None:
            bad.append(f"{where} has no timeout=")
        elif "http_timeout" not in ast.unparse(value):
            bad.append(f"{where} timeout={ast.unparse(value)} is not from http_timeout_seconds")
    assert not bad, "\n".join(bad)


class TestFactory:
    def test_default_is_30s_everywhere(self) -> None:
        t = http_timeout(BifrostSettings())
        assert isinstance(t, httpx.Timeout)
        assert t.connect == t.read == t.write == t.pool == 30.0

    def test_setting_drives_value(self) -> None:
        t = http_timeout(BifrostSettings(http_timeout_seconds=7))
        assert t.connect == t.read == 7.0

    def test_cap_lowers_but_never_raises(self) -> None:
        assert http_timeout(BifrostSettings(http_timeout_seconds=30), cap=2.0).read == 2.0
        assert http_timeout(BifrostSettings(http_timeout_seconds=1), cap=2.0).read == 1.0

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_rejected(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            BifrostSettings(http_timeout_seconds=bad)


def test_cli_client_uses_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost.cli.commands import _http

    monkeypatch.setattr(_http, "get_settings", lambda: BifrostSettings(http_timeout_seconds=9))
    with _http.get_client() as client:
        assert client.timeout.read == 9.0
        assert client.timeout.connect == 9.0
