"""E2E shared fixtures: PostgreSQL container, FastAPI test client, test repo.

Spins up a real PostgreSQL via testcontainers (no host Postgres needed).
Mounts the FastAPI app with ASGITransport for in-process HTTP testing.
Creates a small deterministic Git repo as the extraction target.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from bifrost.api.app import create_app
from bifrost.api.deps import get_db, get_repository
from bifrost.cli.config import reset_settings
from bifrost.storage.models import Base
from bifrost.storage.repository import EntityRepository

# ---------------------------------------------------------------------------
# PostgreSQL container — one per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL container with pgvector for the entire test session."""
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="bifrost_test",
        password="bifrost_test",
        dbname="bifrost_e2e",
    ) as pg:
        yield pg


@pytest.fixture(scope="session")
def database_url(pg_container) -> str:
    """Async database URL pointing at the test container."""
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://bifrost_test:bifrost_test@{host}:{port}/bifrost_e2e"


@pytest.fixture(scope="session")
async def engine(database_url):
    """Create async engine, install pgvector extension, and create all tables."""
    eng = create_async_engine(database_url, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    """Session factory bound to the test engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Per-test DB session
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session(session_factory) -> AsyncGenerator[AsyncSession]:
    """Yield a per-test async session with rollback on teardown."""
    async with session_factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# FastAPI test client — overrides DB deps to point at test container
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(session_factory) -> AsyncGenerator[httpx.AsyncClient]:
    """HTTP client backed by the real FastAPI app with test DB.

    Uses httpx.ASGITransport — no subprocess, no open port.
    """
    app = create_app()

    async def _override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_get_repository():
        async with session_factory() as session:
            repo = EntityRepository(session)
            try:
                yield repo
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_repository] = _override_get_repository

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # Clean overrides so other tests aren't affected
    app.dependency_overrides.clear()
    reset_settings()


# ---------------------------------------------------------------------------
# Test Git repo — deterministic content for extraction verification
# ---------------------------------------------------------------------------

# Known file contents so extraction results are deterministic.
_TEST_REPO_FILES = {
    "README.md": "# Test Repo\nA small test repository for Bifrost E2E tests.\n",
    "src/main.py": (
        '"""Main entry point for the test application."""\n'
        "\n"
        "from src.utils import helper\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        '    """Run the application."""\n'
        "    result = helper()\n"
        '    print(f"Result: {result}")\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    ),
    "src/utils.py": (
        '"""Utility functions."""\n'
        "\n"
        "\n"
        "def helper() -> str:\n"
        '    """Return a greeting string."""\n'
        '    return "hello from bifrost test repo"\n'
    ),
    "src/models.py": (
        '"""Data models for the test app."""\n'
        "\n"
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Item:\n"
        '    """A simple item."""\n'
        "\n"
        "    name: str\n"
        "    value: int\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Config:\n"
        '    """Application configuration."""\n'
        "\n"
        "    debug: bool = False\n"
        '    db_url: str = "sqlite:///test.db"\n'
    ),
}


@pytest.fixture(scope="session")
def test_repo_path() -> Generator[Path]:
    """Create a small Git repo on disk with deterministic contents.

    Returns the path to the repo. Cleaned up at session end.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="bifrost_e2e_repo_"))
    for relpath, content in _TEST_REPO_FILES.items():
        filepath = tmpdir / relpath
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)

    # Initialize as a Git repo so GitAdapter can read it
    subprocess.run(["git", "init", "-b", "main"], cwd=tmpdir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmpdir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmpdir,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Bifrost Test",
            "GIT_AUTHOR_EMAIL": "test@bifrost.dev",
            "GIT_COMMITTER_NAME": "Bifrost Test",
            "GIT_COMMITTER_EMAIL": "test@bifrost.dev",
        },
    )

    yield tmpdir

    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def test_repo_url(test_repo_path: Path) -> str:
    """Return the file:// URL of the test repo (for registering via API)."""
    return f"file://{test_repo_path}"


@pytest.fixture
def make_uuid():
    """Factory for deterministic UUIDs in tests."""

    def _make(n: int = 0) -> uuid.UUID:
        return uuid.UUID(f"00000000-0000-0000-0000-{n:012d}")

    return _make
