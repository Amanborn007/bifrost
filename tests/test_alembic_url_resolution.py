"""Resolution order for the URL alembic migrates against."""

from __future__ import annotations

import pytest

from bifrost.cli.config import resolve_migration_database_url

ENV_VARS = ("DATABASE_URL", "BIFROST_DATABASE_URL")


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_dotenv_value_is_used_when_no_env(clean):
    (clean / ".env").write_text("BIFROST_DATABASE_URL=postgresql+asyncpg://u:p@h:5439/d\n")
    assert resolve_migration_database_url() == "postgresql+asyncpg://u:p@h:5439/d"


def test_process_database_url_beats_dotenv(clean, monkeypatch):
    (clean / ".env").write_text("BIFROST_DATABASE_URL=postgresql+asyncpg://dotenv@h/d\n")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://proc@h/d")
    assert resolve_migration_database_url() == "postgresql+asyncpg://proc@h/d"


def test_process_bifrost_url_beats_dotenv(clean, monkeypatch):
    (clean / ".env").write_text("BIFROST_DATABASE_URL=postgresql+asyncpg://dotenv@h/d\n")
    monkeypatch.setenv("BIFROST_DATABASE_URL", "postgresql+asyncpg://proc@h/d")
    assert resolve_migration_database_url() == "postgresql+asyncpg://proc@h/d"


def test_database_url_beats_bifrost_url(clean, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a@h/d")
    monkeypatch.setenv("BIFROST_DATABASE_URL", "postgresql+asyncpg://b@h/d")
    assert resolve_migration_database_url() == "postgresql+asyncpg://a@h/d"


def test_nothing_set_returns_none(clean):
    assert resolve_migration_database_url() is None


def test_unrelated_invalid_dotenv_line_does_not_raise(clean):
    (clean / ".env").write_text(
        "BIFROST_LOG_LEVEL=not-a-level\nBIFROST_DATABASE_URL=postgresql+asyncpg://u@h/d\n"
    )
    assert resolve_migration_database_url() == "postgresql+asyncpg://u@h/d"
