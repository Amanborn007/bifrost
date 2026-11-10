"""Tests covering the four bug fixes in bundle_cache.py.

BUG 1 (HIGH):  embedding-lost-on-from-dict   — int/str elements must be cast to float
BUG 2 (MEDIUM): sources-none-loses-citations  — BundleSource not created when ref is None/empty
BUG 3 (MEDIUM): bundle-from-dict-none-crash  — str(None)="None" when DB has NULL
BUG 4 (MEDIUM): cache-after-pop-race         — pop() before re-insert creates missing-key gap
"""

from __future__ import annotations

import asyncio

import pytest

from bifrost.research.bundle_cache import (
    BundleCache,
    ResearchBundle,
)

# ---------------------------------------------------------------------------
# BUG 1: embedding elements must be coerced to float
# ---------------------------------------------------------------------------


class TestEmbeddingCoercion:
    """from_dict() must cast every element of last_question_embedding to float."""

    def test_int_elements_become_float(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "last_question_embedding": [1, 2, 3],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question_embedding is not None
        assert all(isinstance(v, float) for v in bundle.last_question_embedding), (
            "All embedding values must be float, even when DB stores ints"
        )

    def test_string_elements_become_float(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "last_question_embedding": ["0.1", "0.2", "0.3"],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question_embedding == pytest.approx([0.1, 0.2, 0.3])

    def test_none_embedding_stays_none(self):
        raw = {"conversation_id": "c1", "last_question": "q", "last_question_embedding": None}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question_embedding is None

    def test_missing_embedding_stays_none(self):
        raw = {"conversation_id": "c1", "last_question": "q"}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question_embedding is None

    def test_empty_list_embedding_stays_none(self):
        raw = {"conversation_id": "c1", "last_question": "q", "last_question_embedding": []}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question_embedding is None

    def test_float_elements_unchanged(self):
        emb = [0.5, 1.0, -0.25]
        raw = {"conversation_id": "c1", "last_question": "q", "last_question_embedding": emb}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question_embedding == pytest.approx(emb)


# ---------------------------------------------------------------------------
# BUG 2: sources with None/empty ref must be skipped
# ---------------------------------------------------------------------------


class TestSourcesSkipEmptyRef:
    """BundleSource entries with a falsy ref must not be created."""

    def test_none_ref_skipped(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "sources": [{"kind": "jira", "ref": None, "title": "ISSUE-1"}],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.sources == [], "Source with ref=None must be dropped"

    def test_empty_string_ref_skipped(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "sources": [{"kind": "jira", "ref": "", "title": "ISSUE-1"}],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.sources == [], "Source with ref='' must be dropped"

    def test_missing_ref_skipped(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "sources": [{"kind": "jira", "title": "ISSUE-1"}],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.sources == [], "Source with missing ref must be dropped"

    def test_valid_ref_kept(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "sources": [
                {"kind": "jira", "ref": "ISSUE-42", "title": "A bug"},
                {"kind": "confluence", "ref": "", "title": "empty ref"},
                {"kind": "github_pr", "ref": "https://github.com/org/repo/pull/1"},
            ],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert len(bundle.sources) == 2
        refs = [s.ref for s in bundle.sources]
        assert "ISSUE-42" in refs
        assert "https://github.com/org/repo/pull/1" in refs

    def test_mixed_none_and_valid(self):
        raw = {
            "conversation_id": "c1",
            "last_question": "q",
            "sources": [
                {"kind": "jira", "ref": None},
                {"kind": "jira", "ref": "VALID-1"},
                {"kind": "jira", "ref": ""},
                {"kind": "jira", "ref": "VALID-2"},
            ],
        }
        bundle = ResearchBundle.from_dict(raw)
        assert len(bundle.sources) == 2
        assert [s.ref for s in bundle.sources] == ["VALID-1", "VALID-2"]


# ---------------------------------------------------------------------------
# BUG 3: NULL DB values must not become the string "None"
# ---------------------------------------------------------------------------


class TestNullDbValues:
    """str(None) produces 'None'; we must use `or ''` to get empty string."""

    def test_null_conversation_id_is_empty_string(self):
        raw = {"conversation_id": None, "last_question": "q"}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.conversation_id == "", "NULL conversation_id must become '' not 'None'"

    def test_null_last_question_is_empty_string(self):
        raw = {"conversation_id": "c1", "last_question": None}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_question == ""

    def test_null_last_answer_is_empty_string(self):
        raw = {"conversation_id": "c1", "last_question": "q", "last_answer": None}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.last_answer == ""

    def test_null_confidence_is_uncertain(self):
        raw = {"conversation_id": "c1", "last_question": "q", "confidence": None}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.confidence == "uncertain"

    def test_null_scope_rationale_is_empty_string(self):
        raw = {"conversation_id": "c1", "last_question": "q", "scope_rationale": None}
        bundle = ResearchBundle.from_dict(raw)
        assert bundle.scope_rationale == ""

    def test_all_nulls_do_not_produce_none_strings(self):
        raw = {
            "conversation_id": None,
            "last_question": None,
            "last_answer": None,
            "confidence": None,
            "scope_rationale": None,
        }
        bundle = ResearchBundle.from_dict(raw)
        for field_name in ("conversation_id", "last_question", "last_answer", "scope_rationale"):
            value = getattr(bundle, field_name)
            assert value != "None", f"{field_name} must not be the string 'None'"
        assert bundle.confidence != "None"


# ---------------------------------------------------------------------------
# BUG 4: save() must not create a missing-key gap via pop() before re-insert
# ---------------------------------------------------------------------------


class TestCacheSaveNoGap:
    """After save(), the key must always be present — never transiently absent."""

    def _make_cache(self, max_size: int = 10, ttl: int = 3600) -> BundleCache:
        return BundleCache(max_size=max_size, ttl_seconds=ttl)

    def _make_bundle(self, cid: str = "conv-1") -> ResearchBundle:
        return ResearchBundle(conversation_id=cid, last_question="hello")

    def test_save_then_load_same_key(self):
        cache = self._make_cache()
        bundle = self._make_bundle()

        async def run():
            await cache.save(bundle)
            return await cache.load("conv-1")

        result = asyncio.run(run())
        assert result is not None
        assert result.conversation_id == "conv-1"

    def test_repeated_save_overwrites_not_duplicates(self):
        cache = self._make_cache()
        b1 = ResearchBundle(conversation_id="conv-1", last_question="first")
        b2 = ResearchBundle(conversation_id="conv-1", last_question="second")

        async def run():
            await cache.save(b1)
            await cache.save(b2)
            size = await cache.size()
            loaded = await cache.load("conv-1")
            return size, loaded

        size, loaded = asyncio.run(run())
        assert size == 1, "Repeated saves to same key must not grow the cache"
        assert loaded is not None
        assert loaded.last_question == "second", "Second save must overwrite first"

    def test_key_present_immediately_after_save(self):
        """Internal _store must contain the key right after the insert, before eviction."""
        cache = self._make_cache()
        bundle = self._make_bundle("conv-x")

        async def run():
            await cache.save(bundle)
            return "conv-x" in cache._store

        present = asyncio.run(run())
        assert present, "Key must exist in _store immediately after save()"

    def test_save_order_is_most_recent_last(self):
        """move_to_end() must place the saved key at the END (most-recently-used)."""
        cache = self._make_cache(max_size=5)

        async def run():
            for i in range(3):
                b = ResearchBundle(conversation_id=f"conv-{i}", last_question="q")
                await cache.save(b)
            # Re-save conv-0 — it should move to the end
            b0 = ResearchBundle(conversation_id="conv-0", last_question="updated")
            await cache.save(b0)
            return list(cache._store.keys())

        keys = asyncio.run(run())
        assert keys[-1] == "conv-0", "Re-saved key must be at the end (most-recently-used position)"

    def test_eviction_removes_oldest_not_newest(self):
        """With max_size=2, saving a third key must evict the least-recently-used."""
        cache = self._make_cache(max_size=2)

        async def run():
            await cache.save(ResearchBundle(conversation_id="a", last_question="q"))
            await cache.save(ResearchBundle(conversation_id="b", last_question="q"))
            await cache.save(ResearchBundle(conversation_id="c", last_question="q"))
            size = await cache.size()
            a_gone = await cache.load("a")
            c_present = await cache.load("c")
            return size, a_gone, c_present

        size, a_gone, c_present = asyncio.run(run())
        assert size == 2
        assert a_gone is None, "Oldest entry 'a' must have been evicted"
        assert c_present is not None, "Newest entry 'c' must still be present"
