"""`bifrost extract run` must surface task failure and the worker's real count keys."""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from rich.console import Console
from rich.progress import Progress
from typer.testing import CliRunner

from bifrost.cli.app import app
from bifrost.cli.commands import extract as extract_cmd

runner = CliRunner()


def _post_response() -> httpx.Response:
    return httpx.Response(
        status_code=202,
        json={
            "data": {"task_id": "task-1"},
            "meta": {"request_id": "r", "timestamp": "2026-04-14T00:00:00Z", "track": "A"},
        },
        request=httpx.Request("POST", "http://test"),
    )


def _run_with_events(events: list[tuple[str, dict[str, Any]]]):
    def fake_parse(_stream: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        yield from events

    with (
        patch("bifrost.cli.commands.extract.get_client") as mock_client,
        patch("bifrost.cli.commands.extract.httpx.Client", MagicMock()),
        patch("bifrost.cli.commands.extract._parse_sse", fake_parse),
    ):
        mock_client.return_value.__enter__.return_value.post.return_value = _post_response()
        return runner.invoke(app, ["extract", "run", "repo-1"])


def test_run_exits_nonzero_on_task_failed() -> None:
    result = _run_with_events([("task_failed", {"error": "Task x not found"})])
    assert result.exit_code == 1, result.output


def test_run_exits_zero_on_task_complete() -> None:
    result = _run_with_events(
        [("task_complete", {"components": 1, "flows": 1, "dependencies": 1, "relationships": 0})]
    )
    assert result.exit_code == 0, result.output


def test_complete_panel_reads_worker_count_keys() -> None:
    buf = io.StringIO()
    progress = Progress()
    task = progress.add_task("x", total=100)
    with patch.object(extract_cmd, "console", Console(file=buf, width=80)):
        extract_cmd._handle_event(
            progress,
            task,
            "task_complete",
            {"components": 7, "flows": 5, "dependencies": 3, "relationships": 2},
        )
    out = buf.getvalue()
    assert "Components:   7" in out
    assert "Flows:        5" in out
    assert "Dependencies: 3" in out
