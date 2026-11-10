"""PostgresStateBackend — the Track A AgentStateBackend (TRD §2.9).

Concrete implementation of the :class:`AgentStateBackend` ABC over the
``knowledge_text_cache`` table. This is the implementer the ABC was
missing: before this, the seam had **zero** concrete backends, so the
"Track B is a config swap" invariant was structurally impossible and the
seam was untestable.

Stores path-organized agent-state documents (the S14 dual-storage layout,
``/repo/{id}/components/{name}.md`` etc.). Differences from
``KnowledgeCache`` (which shares the table as a *cache*):

  * **No token-budget truncation** — agent state must round-trip exactly.
  * **Optimistic concurrency** via ``content_hash`` + ``WritePrecondition``
    (``sha`` / ``must_not_exist``), so concurrent writers can detect
    clobbers (the ABC's whole reason for the precondition).

Content is zlib-compressed for storage parity with ``KnowledgeCache`` so
both read the same column format.
"""

from __future__ import annotations

import hashlib
import zlib

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.errors import ConflictError
from bifrost.storage.backend import (
    AgentStateBackend,
    DocumentEntry,
    WritePrecondition,
)
from bifrost.storage.models import KnowledgeTextCache


class StateBackendConflictError(ConflictError, RuntimeError):
    """Raised when a :class:`WritePrecondition` (sha / must_not_exist) fails.

    Also a ``RuntimeError`` for back-compat with pre-taxonomy ``except``
    clauses; maps to HTTP 409 via :class:`~bifrost.errors.ConflictError`.
    """

    code = "state.backend.conflict"


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compress(text: str) -> str:
    return zlib.compress(text.encode("utf-8"), level=6).hex()


def _decompress(stored: str) -> str:
    try:
        return zlib.decompress(bytes.fromhex(stored)).decode("utf-8")
    except (ValueError, zlib.error):
        return stored  # legacy uncompressed content — return as-is


class PostgresStateBackend(AgentStateBackend):
    """AgentStateBackend backed by the ``knowledge_text_cache`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read(self, path: str) -> str | None:
        row = (
            await self._session.execute(
                select(KnowledgeTextCache.content).where(KnowledgeTextCache.path == path)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _decompress(row)

    async def write(
        self,
        path: str,
        content: str,
        precondition: WritePrecondition | None = None,
    ) -> None:
        if precondition is not None:
            current = (
                await self._session.execute(
                    select(KnowledgeTextCache.content_hash).where(KnowledgeTextCache.path == path)
                )
            ).scalar_one_or_none()
            if precondition.must_not_exist and current is not None:
                raise StateBackendConflictError(
                    f"write precondition failed: {path!r} already exists"
                )
            if precondition.sha is not None and current != precondition.sha:
                raise StateBackendConflictError(
                    f"write precondition failed: {path!r} content_hash "
                    f"{current!r} != expected {precondition.sha!r}"
                )

        compressed = _compress(content)
        content_hash = _hash(content)
        stmt = (
            pg_insert(KnowledgeTextCache)
            .values(
                path=path,
                content=compressed,
                content_hash=content_hash,
                updated_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=["path"],
                set_={
                    "content": compressed,
                    "content_hash": content_hash,
                    "updated_at": func.now(),
                },
            )
        )
        await self._session.execute(stmt)

    async def list(self, path_prefix: str) -> list[DocumentEntry]:
        rows = (
            await self._session.execute(
                select(
                    KnowledgeTextCache.path,
                    KnowledgeTextCache.updated_at,
                    KnowledgeTextCache.content_hash,
                )
                .where(KnowledgeTextCache.path.startswith(path_prefix))
                .order_by(KnowledgeTextCache.path)
            )
        ).all()
        return [
            DocumentEntry(path=path, updated_at=updated_at, content_hash=content_hash)
            for (path, updated_at, content_hash) in rows
        ]

    async def delete(self, path: str) -> None:
        await self._session.execute(
            sa_delete(KnowledgeTextCache).where(KnowledgeTextCache.path == path)
        )
