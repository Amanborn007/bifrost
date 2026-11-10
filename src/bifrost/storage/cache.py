"""KnowledgeTextCache — compressed knowledge summaries for agent context windows.

Provides get/set with zlib compression, token budget enforcement,
and invalidation via EventBus subscription.
"""

from __future__ import annotations

import hashlib
import zlib
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from bifrost.core.events import EventBus

logger = structlog.stdlib.get_logger(__name__)

# Rough approximation: 1 token ~ 4 characters for English text
_CHARS_PER_TOKEN = 4
_MAX_TOKENS = 5000
_MAX_CHARS = _MAX_TOKENS * _CHARS_PER_TOKEN


class KnowledgeCache:
    """Manages compressed knowledge text entries in the knowledge_text_cache table.

    Each entry is zlib-compressed before storage and decompressed on retrieval.
    Entries exceeding the 5K token budget are truncated before storage.
    """

    def __init__(self, session: AsyncSession, event_bus: EventBus | None = None) -> None:
        self._session = session
        self._event_bus = event_bus

    async def get(self, path: str) -> str | None:
        """Retrieve and decompress a cached knowledge entry."""
        from sqlalchemy import select

        from bifrost.storage.models import KnowledgeTextCache

        result = await self._session.execute(
            select(KnowledgeTextCache).where(KnowledgeTextCache.path == path)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _decompress(row.content)

    async def set(
        self,
        path: str,
        content: str,
        repo_id: uuid.UUID | None = None,
        entity_type: str | None = None,
    ) -> str:
        """Compress and store a knowledge entry, enforcing the token budget.

        Returns the content_hash of the stored entry.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from bifrost.storage.models import KnowledgeTextCache

        truncated = _enforce_token_budget(content)
        compressed = _compress(truncated)
        content_hash = hashlib.sha256(truncated.encode()).hexdigest()[:16]

        stmt = (
            pg_insert(KnowledgeTextCache)
            .values(
                path=path,
                content=compressed,
                content_hash=content_hash,
                repo_id=repo_id,
                entity_type=entity_type,
            )
            .on_conflict_do_update(
                index_elements=["path"],
                set_={
                    "content": compressed,
                    "content_hash": content_hash,
                    "repo_id": repo_id,
                    "entity_type": entity_type,
                },
            )
        )
        await self._session.execute(stmt)
        return content_hash

    async def invalidate(self, path: str) -> bool:
        """Remove a cached entry. Returns True if an entry was removed."""
        from sqlalchemy import select

        from bifrost.storage.models import KnowledgeTextCache

        result = await self._session.execute(
            select(KnowledgeTextCache).where(KnowledgeTextCache.path == path)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        return True

    async def invalidate_by_entity(self, entity_urn: str) -> int:
        """Invalidate all cache entries whose path contains the entity URN."""
        from sqlalchemy import delete

        from bifrost.storage.models import KnowledgeTextCache

        stmt = delete(KnowledgeTextCache).where(KnowledgeTextCache.path.contains(entity_urn))
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    def register_invalidation_handler(self, event_bus: EventBus) -> None:
        """Subscribe to EntityUpdated events for automatic cache invalidation."""
        from bifrost.core.events import EntityUpdated

        async def _on_entity_updated(event: object) -> None:
            assert isinstance(event, EntityUpdated)
            count = await self.invalidate_by_entity(event.urn)
            if count > 0:
                logger.info(
                    "cache.invalidated",
                    urn=event.urn,
                    entries_removed=count,
                )

        event_bus.subscribe(EntityUpdated, _on_entity_updated)  # type: ignore[arg-type]


def _compress(text: str) -> str:
    """Compress text with zlib and return as latin-1 string for TEXT column storage."""
    compressed_bytes = zlib.compress(text.encode("utf-8"), level=6)
    return compressed_bytes.hex()


def _decompress(stored: str) -> str:
    """Decompress a hex-encoded zlib string back to text."""
    try:
        compressed_bytes = bytes.fromhex(stored)
        return zlib.decompress(compressed_bytes).decode("utf-8")
    except (ValueError, zlib.error):
        # Stored content is not compressed — return as-is (backwards compat)
        return stored


def _enforce_token_budget(text: str, max_chars: int = _MAX_CHARS) -> str:
    """Truncate text to fit within the token budget."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text string."""
    return len(text) // _CHARS_PER_TOKEN
