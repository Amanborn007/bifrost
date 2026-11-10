"""Tests for CLI command groups — all commands use mocked httpx responses.

Uses Typer's CliRunner and patches get_client at the use site in each command module.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from bifrost.cli.app import app

runner = CliRunner()

# Patch paths — must match where get_client is referenced, not where defined
_REPO_CLIENT = "bifrost.cli.commands.repo.get_client"
_DOMAIN_CLIENT = "bifrost.cli.commands.domain.get_client"
_QUERY_CLIENT = "bifrost.cli.commands.query.get_client"
_EXTRACT_CLIENT = "bifrost.cli.commands.extract.get_client"


def _envelope(data: Any) -> dict[str, Any]:
    """Wrap data in BifrostEnvelope shape."""
    return {
        "data": data,
        "meta": {"request_id": "test-req", "timestamp": "2026-04-14T00:00:00Z", "track": "A"},
    }


def _mock_response(data: Any, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx response with envelope-wrapped data."""
    return httpx.Response(
        status_code=status_code,
        json=_envelope(data),
        request=httpx.Request("GET", "http://test"),
    )


def _mock_error(status_code: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json={"error": {"code": code, "message": message, "retryable": False}},
        request=httpx.Request("GET", "http://test"),
    )


# ── Repo commands ────────────────────────────────────────────────────


class TestRepoList:
    def test_list_empty(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.return_value = _mock_response([])
            result = runner.invoke(app, ["repo", "list"])
        assert result.exit_code == 0
        assert "No repositories" in result.output

    def test_list_with_repos(self) -> None:
        repos = [
            {
                "id": "aaaaaaaa-1111-2222-3333-444444444444",
                "urn": "urn:bifrost:repo:test",
                "name": "test-repo",
                "url": "https://github.com/org/test",
                "domain_id": None,
                "freshness_state": "current",
                "index_status": "indexed",
                "last_indexed_at": "2026-04-14T00:00:00Z",
            }
        ]
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.return_value = _mock_response(repos)
            result = runner.invoke(app, ["repo", "list"])
        assert result.exit_code == 0
        assert "test-repo" in result.output
        assert "current" in result.output


class TestRepoAdd:
    def test_add_repo(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(
                {"id": "abc123", "urn": "urn:bifrost:repo:my-app", "status": "registered"},
                status_code=201,
            )
            result = runner.invoke(app, ["repo", "add", "https://github.com/org/my-app"])
        assert result.exit_code == 0
        assert "Registered" in result.output
        assert "urn:bifrost:repo:my-app" in result.output


class TestRepoRemove:
    def test_remove_repo(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.delete.return_value = _mock_response({"id": "abc123", "status": "deregistered"})
            result = runner.invoke(app, ["repo", "remove", "abc123"])
        assert result.exit_code == 0
        assert "Deregistered" in result.output


class TestRepoStatus:
    def test_status_detail(self) -> None:
        detail = {
            "id": "abc123",
            "urn": "urn:bifrost:repo:test",
            "name": "test-repo",
            "url": "https://github.com/org/test",
            "domain_id": None,
            "freshness": {
                "state": "current",
                "last_commit_indexed": "abc123",
                "last_indexed_at": "2026-04-14T00:00:00Z",
                "pending_commits": 0,
            },
            "stats": {"component_count": 5, "flow_count": 3, "dependency_count": 12},
        }
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.return_value = _mock_response(detail)
            result = runner.invoke(app, ["repo", "status", "abc123"])
        assert result.exit_code == 0
        assert "test-repo" in result.output
        assert "5 components" in result.output


# ── Domain commands ──────────────────────────────────────────────────


class TestDomainCreate:
    def test_create_domain(self) -> None:
        with patch(_DOMAIN_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(
                {"id": "dom-1", "urn": "urn:bifrost:domain:orders"},
                status_code=201,
            )
            result = runner.invoke(app, ["domain", "create", "orders"])
        assert result.exit_code == 0
        assert "Created" in result.output


class TestDomainList:
    def test_list_empty(self) -> None:
        with patch(_DOMAIN_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.return_value = _mock_response([])
            result = runner.invoke(app, ["domain", "list"])
        assert result.exit_code == 0
        assert "No domains" in result.output

    def test_list_with_domains(self) -> None:
        domains = [
            {"id": "aaaaaaaa-1111-2222-3333-444444444444", "name": "orders", "repo_count": 3},
        ]
        with patch(_DOMAIN_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.return_value = _mock_response(domains)
            result = runner.invoke(app, ["domain", "list"])
        assert result.exit_code == 0
        assert "orders" in result.output


class TestDomainInfo:
    def test_info_detail(self) -> None:
        detail = {
            "id": "dom-1",
            "name": "orders",
            "description": "Shipment processing domain",
            "repos": [
                {
                    "id": "aaaaaaaa-1111-2222-3333-444444444444",
                    "name": "order-api",
                    "freshness_state": "current",
                },
            ],
            "cross_repo_flow_count": 2,
        }
        with patch(_DOMAIN_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.return_value = _mock_response(detail)
            result = runner.invoke(app, ["domain", "info", "dom-1"])
        assert result.exit_code == 0
        assert "orders" in result.output
        assert "order-api" in result.output


# ── Query commands ───────────────────────────────────────────────────


class TestQueryAsk:
    def test_ask_text_output(self) -> None:
        answer = {
            "answer": "The auth flow uses OAuth2 with PKCE.",
            "confidence": "high",
            "sources": [
                {
                    "urn": "urn:bifrost:component:auth-service",
                    "name": "AuthService",
                    "entity_type": "component",
                    "relevance": 0.95,
                },
            ],
            "gaps": [],
            "conflicts": [],
            "task_id": "task-abc",
        }
        with patch(_QUERY_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(answer, status_code=202)
            result = runner.invoke(app, ["query", "ask", "How does auth work?"])
        assert result.exit_code == 0
        assert "OAuth2" in result.output
        assert "high" in result.output

    def test_ask_json_output(self) -> None:
        answer = {
            "answer": "Test answer",
            "confidence": "medium",
            "sources": [],
            "gaps": ["Missing integration tests"],
            "conflicts": [],
            "task_id": "task-xyz",
        }
        with patch(_QUERY_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(answer, status_code=202)
            result = runner.invoke(app, ["query", "ask", "test?", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["answer"] == "Test answer"
        assert parsed["confidence"] == "medium"

    def test_ask_with_repo_scope(self) -> None:
        answer = {
            "answer": "Scoped answer",
            "confidence": "low",
            "sources": [],
            "gaps": [],
            "conflicts": [],
            "task_id": "task-scoped",
        }
        with patch(_QUERY_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(answer, status_code=202)
            result = runner.invoke(app, ["query", "ask", "test?", "--repo", "repo-1"])
        assert result.exit_code == 0
        # Verify the POST was called with repo_ids
        call_args = client.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert body["repo_ids"] == ["repo-1"]


# ── Extract commands ─────────────────────────────────────────────────


class TestExtractRun:
    def test_trigger_extraction(self) -> None:
        with patch(_EXTRACT_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(
                {"task_id": "task-ext-1", "status": "queued"},
                status_code=202,
            )
            # Patch _stream_progress to avoid actually connecting SSE
            with patch("bifrost.cli.commands.extract._stream_progress"):
                result = runner.invoke(app, ["extract", "run", "repo-123"])
        assert result.exit_code == 0
        assert "task-ext-1" in result.output


# ── Help commands ────────────────────────────────────────────────────


class TestHelpOutputs:
    @pytest.mark.parametrize(
        "cmd",
        [
            ["repo", "--help"],
            ["domain", "--help"],
            ["query", "--help"],
            ["extract", "--help"],
        ],
    )
    def test_help_exits_zero(self, cmd: list[str]) -> None:
        result = runner.invoke(app, cmd)
        assert result.exit_code == 0
        assert "Usage" in result.output


# ── Error handling ───────────────────────────────────────────────────


class TestErrorHandling:
    def test_connection_refused(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.get.side_effect = httpx.ConnectError("Connection refused")
            result = runner.invoke(app, ["repo", "list"])
        assert result.exit_code == 1

    def test_404_error(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            resp = _mock_error(404, "NOT_FOUND", "Repo not found")
            client.get.return_value = resp
            result = runner.invoke(app, ["repo", "status", "nonexistent"])
        assert result.exit_code == 1

    def test_422_error(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            resp = _mock_error(422, "VALIDATION_ERROR", "Invalid URL format")
            client.post.return_value = resp
            result = runner.invoke(app, ["repo", "add", "not-a-url"])
        assert result.exit_code == 1

    def test_500_error(self) -> None:
        with patch(_REPO_CLIENT) as mock_client:
            client = mock_client.return_value.__enter__.return_value
            resp = _mock_error(500, "INTERNAL_ERROR", "Database connection failed")
            client.get.return_value = resp
            result = runner.invoke(app, ["repo", "list"])
        assert result.exit_code == 1
