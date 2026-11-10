"""CLI command groups — thin API consumers using httpx + rich."""

from __future__ import annotations

from bifrost.cli.commands.domain import domain_app
from bifrost.cli.commands.extract import extract_app
from bifrost.cli.commands.query import query_app
from bifrost.cli.commands.repo import repo_app
from bifrost.cli.commands.token import token_app

__all__ = ["domain_app", "extract_app", "query_app", "repo_app", "token_app"]
