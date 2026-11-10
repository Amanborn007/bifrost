"""Tests for KnowledgeCache — compression, token budget, invalidation."""

from __future__ import annotations

from bifrost.core.events import EntityUpdated, EventBus
from bifrost.storage.cache import KnowledgeCache, _compress, _decompress, estimate_tokens


class TestCompression:
    def test_round_trip(self) -> None:
        original = "This is a test knowledge summary about a shipment service component."
        compressed = _compress(original)
        decompressed = _decompress(compressed)
        assert decompressed == original

    def test_compression_reduces_size(self) -> None:
        # Repetitive text compresses well
        original = "knowledge " * 500
        compressed = _compress(original)
        assert len(compressed) < len(original)

    def test_decompress_non_compressed_returns_as_is(self) -> None:
        # If data isn't valid hex/zlib, return as-is
        plain = "just plain text"
        assert _decompress(plain) == plain


class TestTokenBudget:
    def test_estimate_tokens(self) -> None:
        text = "a" * 4000
        assert estimate_tokens(text) == 1000

    def test_short_text_not_truncated(self) -> None:
        from bifrost.storage.cache import _enforce_token_budget

        text = "Short text"
        assert _enforce_token_budget(text) == text

    def test_long_text_truncated_to_budget(self) -> None:
        from bifrost.storage.cache import _MAX_CHARS, _enforce_token_budget

        text = "x" * (_MAX_CHARS + 1000)
        truncated = _enforce_token_budget(text)
        assert len(truncated) == _MAX_CHARS


class TestKnowledgeCacheCRUD:
    async def test_set_and_get(self, db_session) -> None:
        cache = KnowledgeCache(db_session)
        content = "This component handles shipment processing via carrier API."
        content_hash = await cache.set("components/order-service", content)
        assert content_hash  # non-empty

        retrieved = await cache.get("components/order-service")
        assert retrieved == content

    async def test_get_missing_returns_none(self, db_session) -> None:
        cache = KnowledgeCache(db_session)
        assert await cache.get("nonexistent/path") is None

    async def test_set_overwrites_existing(self, db_session) -> None:
        cache = KnowledgeCache(db_session)
        await cache.set("components/auth", "v1 content")
        await cache.set("components/auth", "v2 content")

        retrieved = await cache.get("components/auth")
        assert retrieved == "v2 content"

    async def test_long_content_truncated(self, db_session) -> None:
        from bifrost.storage.cache import _MAX_CHARS

        cache = KnowledgeCache(db_session)
        long_content = "x" * (_MAX_CHARS + 5000)
        await cache.set("components/huge", long_content)

        retrieved = await cache.get("components/huge")
        assert retrieved is not None
        assert len(retrieved) == _MAX_CHARS

    async def test_invalidate(self, db_session) -> None:
        cache = KnowledgeCache(db_session)
        await cache.set("components/to-delete", "content")
        assert await cache.invalidate("components/to-delete") is True
        assert await cache.get("components/to-delete") is None

    async def test_invalidate_missing_returns_false(self, db_session) -> None:
        cache = KnowledgeCache(db_session)
        assert await cache.invalidate("nonexistent") is False


class TestCacheInvalidationViaEvents:
    async def test_entity_update_invalidates_cache(self, db_session) -> None:
        bus = EventBus()
        cache = KnowledgeCache(db_session, event_bus=bus)
        cache.register_invalidation_handler(bus)

        # Populate cache with an entry containing the entity URN in its path
        entity_urn = "urn:bifrost:component:order-svc"
        await cache.set(f"entities/{entity_urn}/summary", "old summary")

        # Publish an EntityUpdated event
        await bus.publish(EntityUpdated(entity_id="e1", urn=entity_urn))
        await bus.drain()

        # Cache entry should have been invalidated
        result = await cache.get(f"entities/{entity_urn}/summary")
        assert result is None
