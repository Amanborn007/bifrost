"""BundleCache — in-memory store for follow-up planner state.

Covers: round-trip, size cap (LRU eviction), TTL expiry, invalidate,
trim caps, dict round-trip.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from bifrost.research.bundle_cache import (
    BundleCache,
    BundleSource,
    ResearchBundle,
    get_bundle_cache,
    reset_bundle_cache_for_tests,
)


def _make_bundle(cid: str = "conv-1", **overrides) -> ResearchBundle:
    base = dict(
        conversation_id=cid,
        last_question="how does orders retry work?",
        last_answer="It uses an exponential-backoff queue.",
        confidence="confirmed",
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=["orders"],
        scope_rationale="single repo deep-indexed",
        subgraph_entity_urns=["bifrost://repo/orders/component/RetryQueue"],
        sources=[BundleSource(kind="jira", ref="SHIP-123", title="retry bug")],
        last_question_embedding=None,
    )
    base.update(overrides)
    return ResearchBundle(**base)


@pytest.mark.asyncio
async def test_save_and_load_roundtrip() -> None:
    cache = BundleCache()
    await cache.save(_make_bundle("c1"))

    out = await cache.load("c1")

    assert out is not None
    assert out.conversation_id == "c1"
    assert out.last_question == "how does orders retry work?"
    assert out.repos_used_names == ["orders"]
    assert out.sources[0].ref == "SHIP-123"


@pytest.mark.asyncio
async def test_load_unknown_conversation_returns_none() -> None:
    cache = BundleCache()
    assert await cache.load("nope") is None


@pytest.mark.asyncio
async def test_invalidate_removes_entry() -> None:
    cache = BundleCache()
    await cache.save(_make_bundle("c2"))
    await cache.invalidate("c2")

    assert await cache.load("c2") is None


@pytest.mark.asyncio
async def test_lru_evicts_oldest_when_over_cap() -> None:
    cache = BundleCache(max_size=3, ttl_seconds=3600)
    await cache.save(_make_bundle("a"))
    await cache.save(_make_bundle("b"))
    await cache.save(_make_bundle("c"))
    # Access b → marks it as most-recent
    await cache.load("b")
    await cache.save(_make_bundle("d"))  # 4th — evict 'a' (oldest)

    assert await cache.load("a") is None
    assert await cache.load("b") is not None
    assert await cache.load("c") is not None
    assert await cache.load("d") is not None


@pytest.mark.asyncio
async def test_ttl_expires_lazily() -> None:
    cache = BundleCache(max_size=10, ttl_seconds=1)
    await cache.save(_make_bundle("ttl"))
    # Force time to advance past TTL
    cache._store["ttl"] = (time.monotonic() - 5.0, cache._store["ttl"][1])

    assert await cache.load("ttl") is None
    # And the lazy delete actually fired.
    assert await cache.size() == 0


@pytest.mark.asyncio
async def test_trim_caps_subgraph_and_sources() -> None:
    big = _make_bundle(
        "big",
        subgraph_entity_urns=[f"u{i}" for i in range(500)],
        sources=[BundleSource(kind="jira", ref=f"X-{i}") for i in range(200)],
    )
    cache = BundleCache()
    await cache.save(big)

    out = await cache.load("big")
    assert out is not None
    assert len(out.subgraph_entity_urns) <= 200
    assert len(out.sources) <= 50


@pytest.mark.asyncio
async def test_dict_roundtrip_preserves_fields() -> None:
    bundle = _make_bundle(
        "rt",
        last_question_embedding=[0.1, 0.2, 0.3],
    )

    raw = bundle.to_dict()
    restored = ResearchBundle.from_dict(raw)

    assert restored.conversation_id == bundle.conversation_id
    assert restored.last_question_embedding == [0.1, 0.2, 0.3]
    assert restored.sources[0].ref == "SHIP-123"


@pytest.mark.asyncio
async def test_save_overwrites_prior_entry() -> None:
    cache = BundleCache()
    await cache.save(_make_bundle("ow", last_question="first"))
    await cache.save(_make_bundle("ow", last_question="second"))

    out = await cache.load("ow")
    assert out is not None
    assert out.last_question == "second"


def test_module_singleton_resettable() -> None:
    reset_bundle_cache_for_tests()
    a = get_bundle_cache()
    b = get_bundle_cache()
    assert a is b
    reset_bundle_cache_for_tests()
    c = get_bundle_cache()
    assert c is not a


@pytest.mark.asyncio
async def test_concurrent_saves_dont_corrupt() -> None:
    """asyncio.Lock should serialize concurrent saves on the same conv."""
    cache = BundleCache()

    async def _writer(i: int) -> None:
        await cache.save(_make_bundle("conc", last_question=f"q{i}"))

    await asyncio.gather(*[_writer(i) for i in range(20)])

    out = await cache.load("conc")
    assert out is not None
    # The last writer wins; we just need the cache to be readable.
    assert out.last_question.startswith("q")
