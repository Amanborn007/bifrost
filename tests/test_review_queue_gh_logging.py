"""build_review_queue must not raise from its logger calls when gh misbehaves."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bifrost.actions.review_queue import build_review_queue


class _Rows:
    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return []


class _FakeSession:
    async def execute(self, *_a: Any, **_k: Any) -> _Rows:
        return _Rows()


class _FakeProc:
    def __init__(self, rc: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = rc
        self._out = (stdout, stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out


def _patch_gh(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    async def fake_exec(*args: str, **_kw: Any) -> _FakeProc:
        assert args[0] == "gh"
        return handler(args[1:])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


async def test_unauthenticated_gh_sets_note_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gh(monkeypatch, lambda _args: _FakeProc(1, b"", b"not logged in"))

    queue = await build_review_queue(_FakeSession(), email="a@example.com")  # type: ignore[arg-type]

    assert "gh is not authenticated" in queue.note
    assert queue.items == []


async def test_other_gh_auth_failure_sets_check_failed_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gh(monkeypatch, lambda _args: _FakeProc(2, b"", b"boom"))

    queue = await build_review_queue(_FakeSession(), email="a@example.com")  # type: ignore[arg-type]

    assert "gh check failed" in queue.note


async def test_pr_list_parse_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(args: tuple[str, ...]) -> _FakeProc:
        if args[0] == "auth":
            return _FakeProc(0, b"", b"")
        return _FakeProc(0, b"not json", b"")

    _patch_gh(monkeypatch, handler)

    with caplog.at_level("WARNING"):
        queue = await build_review_queue(_FakeSession(), email="a@example.com")  # type: ignore[arg-type]

    assert queue.items == []
    assert any("gh_review_requested_failed" in r.getMessage() for r in caplog.records)
    assert any("gh_authored_failed" in r.getMessage() for r in caplog.records)
