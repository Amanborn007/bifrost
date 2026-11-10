"""Tests for PostgresStateBackend — the Track A AgentStateBackend.

The ABC previously had zero implementers; these tests give the seam its
first real coverage: round-trip read/write/list/delete + optimistic
concurrency via WritePrecondition (sha / must_not_exist).

Uses the real-Postgres ``db_session`` fixture; writes use ``flush`` so the
per-test rollback in conftest keeps isolation.
"""

from __future__ import annotations

import pytest

from bifrost.storage.backend import AgentStateBackend, WritePrecondition
from bifrost.storage.state_backend import (
    PostgresStateBackend,
    StateBackendConflictError,
    _hash,
)


@pytest.mark.asyncio
async def test_is_agent_state_backend(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    assert isinstance(backend, AgentStateBackend)


@pytest.mark.asyncio
async def test_write_read_roundtrip_exact(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    # Content larger than the KnowledgeCache 5K-token budget must NOT be
    # truncated by the state backend — agent state round-trips exactly.
    big = "x" * 50_000
    await backend.write("/repo/r1/components/Foo.md", big)
    await db_session.flush()
    assert await backend.read("/repo/r1/components/Foo.md") == big


@pytest.mark.asyncio
async def test_read_missing_returns_none(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    assert await backend.read("/repo/r1/nope.md") is None


@pytest.mark.asyncio
async def test_list_by_prefix(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    await backend.write("/repo/r1/components/A.md", "a")
    await backend.write("/repo/r1/components/B.md", "b")
    await backend.write("/repo/r2/components/C.md", "c")
    await db_session.flush()

    entries = await backend.list("/repo/r1/")
    paths = {e.path for e in entries}
    assert paths == {"/repo/r1/components/A.md", "/repo/r1/components/B.md"}
    # DocumentEntry carries the content_hash used for preconditions.
    for e in entries:
        assert e.content_hash and e.updated_at is not None


@pytest.mark.asyncio
async def test_delete(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    await backend.write("/repo/r1/x.md", "data")
    await db_session.flush()
    await backend.delete("/repo/r1/x.md")
    await db_session.flush()
    assert await backend.read("/repo/r1/x.md") is None


@pytest.mark.asyncio
async def test_must_not_exist_precondition(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    await backend.write("/repo/r1/once.md", "first")
    await db_session.flush()
    with pytest.raises(StateBackendConflictError):
        await backend.write(
            "/repo/r1/once.md",
            "second",
            precondition=WritePrecondition(must_not_exist=True),
        )


@pytest.mark.asyncio
async def test_sha_precondition_blocks_stale_overwrite(db_session) -> None:
    backend = PostgresStateBackend(db_session)
    await backend.write("/repo/r1/cas.md", "v1")
    await db_session.flush()

    # Correct sha (of current content) → write succeeds.
    await backend.write(
        "/repo/r1/cas.md",
        "v2",
        precondition=WritePrecondition(sha=_hash("v1")),
    )
    await db_session.flush()
    assert await backend.read("/repo/r1/cas.md") == "v2"

    # Stale sha (still pointing at v1) → conflict.
    with pytest.raises(StateBackendConflictError):
        await backend.write(
            "/repo/r1/cas.md",
            "v3",
            precondition=WritePrecondition(sha=_hash("v1")),
        )


@pytest.mark.asyncio
async def test_factory_builds_state_backend() -> None:
    from bifrost.boot import create_agent_infrastructure

    infra = create_agent_infrastructure()
    backend = infra.make_state_backend(session=None)  # type: ignore[arg-type]
    assert isinstance(backend, PostgresStateBackend)
