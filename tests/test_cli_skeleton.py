"""Tests for the CLI skeleton — help output and command groups.

Uses Typer's CliRunner so no real API server is needed.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from bifrost.cli.app import app

runner = CliRunner()


class TestMainHelp:
    def test_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_help_shows_all_groups(self) -> None:
        result = runner.invoke(app, ["--help"])
        for group in ("repo", "domain", "query", "extract"):
            assert group in result.output, f"'{group}' missing from --help output"

    def test_no_args_shows_help(self) -> None:
        result = runner.invoke(app, [])
        # Typer exits with 0 or 2 for no_args_is_help — either is acceptable
        assert result.exit_code in (0, 2)
        assert "Usage" in result.output


class TestCommandGroupHelp:
    @pytest.mark.parametrize("group", ["repo", "domain", "query", "extract"])
    def test_group_help(self, group: str) -> None:
        result = runner.invoke(app, [group, "--help"])
        assert result.exit_code == 0, f"{group} --help failed: {result.output}"
        assert "Usage" in result.output


class TestCommandsWithoutServer:
    """Commands exit with code 1 when no API server is running."""

    def test_repo_list_no_server(self) -> None:
        result = runner.invoke(app, ["repo", "list"])
        assert result.exit_code == 1

    def test_domain_list_no_server(self) -> None:
        result = runner.invoke(app, ["domain", "list"])
        assert result.exit_code == 1

    def test_query_ask_no_server(self) -> None:
        result = runner.invoke(app, ["query", "ask", "what is this?"])
        assert result.exit_code == 1

    def test_extract_run_no_server(self) -> None:
        result = runner.invoke(app, ["extract", "run", "my-repo"])
        assert result.exit_code == 1
