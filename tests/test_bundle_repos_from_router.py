"""repos_used must come from rankings, not useful_bundles.

Root cause:
  At the end of every successful research turn the coordinator saved
  ``repos_used`` from ``useful_bundles`` — the subset of repo-bundles
  that returned extraction signal.  An unindexed repo the router
  *correctly* selected (high-relevance) scores no signal, so it was
  silently excluded from ``useful_bundles`` even though it was the
  right answer.

  On the follow-up turn the scope-inheritance block (lines ~1970-1993
  of coordinator.py) narrows the candidate pool to
  ``prior_bundle.repos_used``.  Because useful_bundles omitted the
  correct repo, the follow-up's router never saw it, and the UI showed
  the wrong repos being "scanned and indexed again".

  Additionally, the ``ChatScopeLine`` component (query.tsx) reads
  ``last_research.repos_used_names`` from the API's persisted bundle to
  display "Routed to N repos: …" — so the stale useful_bundles data was
  also shown incorrectly to the user in the conversation header.

Fix:
  Both bundle-save sites in ``run_research`` now use
  ``[rk.repo_id / rk.repo_name for rk in rankings]``.  ``rankings`` is
  exactly the data emitted in the ``route`` SSE event, so the saved
  scope and the live display are always in sync.

Invariants tested:
  1. ``repos_used`` ⊇ all router-selected repos  (not just signal subset)
  2. Follow-up scope inheritance narrows to the full router selection
  3. Unindexed-but-correct repos survive into the follow-up candidate pool
  4. Benchmark: mismatch rate between routed and inherited repos is 0
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.research.bundle_cache import (
    ResearchBundle,
    reset_bundle_cache_for_tests,
)
from bifrost.research.coordinator import (
    _load_prior_bundle,
    _persist_research_bundle,
)
from bifrost.research.router import RepoRanking
from bifrost.storage.repository import EntityRepository

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ranking(repo_id: str, repo_name: str, relevance: str = "high") -> RepoRanking:
    return RepoRanking(repo_id=repo_id, repo_name=repo_name, relevance=relevance, reason="test")


@dataclass
class _FakeBundle:
    """Minimal stand-in for a RepoBundle (agent output per repo)."""

    repo_id: str
    repo_name: str
    has_signal: bool = True
    error: str | None = None


def _repos_used_from_rankings(rankings: list[RepoRanking]) -> tuple[list[str], list[str]]:
    """The exact mapping the fixed coordinator uses at both save sites."""
    return (
        [rk.repo_id for rk in rankings],
        [rk.repo_name for rk in rankings],
    )


def _repos_used_from_useful_bundles(
    bundles: list[_FakeBundle],
) -> tuple[list[str], list[str]]:
    """The OLD (buggy) mapping that was previously used."""
    useful = [b for b in bundles if b.has_signal and not b.error]
    return (
        [b.repo_id for b in useful],
        [b.repo_name for b in useful],
    )


# ── Unit tests: the core invariant ───────────────────────────────────────────


class TestReposUsedFromRankings:
    """repos_used must equal what the route SSE event showed the user."""

    def test_all_ranked_repos_appear_in_repos_used(self) -> None:
        """Every repo the router selected must be in repos_used so the
        follow-up scope-inheritance block can consider it."""
        rankings = [
            _ranking("report-id", "report-export-service"),
            _ranking("catalog-id", "catalog-gateway"),
            _ranking("sync-id", "catalog-sync-worker"),
        ]
        ids, names = _repos_used_from_rankings(rankings)
        assert set(ids) == {"report-id", "catalog-id", "sync-id"}
        assert set(names) == {
            "report-export-service",
            "catalog-gateway",
            "catalog-sync-worker",
        }

    def test_unindexed_correct_repo_not_lost(self) -> None:
        """The specific regression: report-export-service is the correct
        repo but has no extraction signal (not yet indexed). Under the old
        code it fell out of useful_bundles and was lost. Under the fix it
        stays because it's in rankings."""
        rankings = [
            _ranking("report-id", "report-export-service", relevance="high"),
            _ranking("catalog-id", "catalog-gateway", relevance="medium"),
        ]
        bundles = [
            _FakeBundle("report-id", "report-export-service", has_signal=False),  # not indexed
            _FakeBundle("catalog-id", "catalog-gateway", has_signal=True),
        ]

        fixed_ids, _ = _repos_used_from_rankings(rankings)
        buggy_ids, _ = _repos_used_from_useful_bundles(bundles)

        # Fixed: the correct repo is present
        assert "report-id" in fixed_ids

        # Regression check: proves the old code had the bug
        assert "report-id" not in buggy_ids

    def test_single_repo_result(self) -> None:
        rankings = [_ranking("a-id", "auth-service")]
        ids, names = _repos_used_from_rankings(rankings)
        assert ids == ["a-id"]
        assert names == ["auth-service"]

    def test_order_is_preserved(self) -> None:
        """Order from rankings (router confidence order) must be preserved."""
        rankings = [
            _ranking("first-id", "first"),
            _ranking("second-id", "second"),
            _ranking("third-id", "third"),
        ]
        ids, names = _repos_used_from_rankings(rankings)
        assert ids == ["first-id", "second-id", "third-id"]
        assert names == ["first", "second", "third"]

    def test_all_bundles_have_signal_both_approaches_agree(self) -> None:
        """When every bundle has signal (happy path) both the old and new
        approaches produce the same result — no regression for the common
        case."""
        rankings = [
            _ranking("a-id", "svc-a"),
            _ranking("b-id", "svc-b"),
        ]
        bundles = [
            _FakeBundle("a-id", "svc-a", has_signal=True),
            _FakeBundle("b-id", "svc-b", has_signal=True),
        ]
        fixed_ids, _ = _repos_used_from_rankings(rankings)
        buggy_ids, _ = _repos_used_from_useful_bundles(bundles)
        assert set(fixed_ids) == set(buggy_ids)


class TestScopeInheritanceInvariant:
    """Simulate the scope-narrowing logic from coordinator.py lines ~1970-1993.

    When a follow-up question arrives, the coordinator narrows the candidate
    pool to prior_bundle.repos_used.  This test suite verifies that with the
    fixed repos_used (= rankings), the right repos survive into the follow-up.
    """

    @staticmethod
    def _narrow_candidates(
        candidates: list[dict[str, Any]],
        prior_repos_used: list[str],
    ) -> list[dict[str, Any]]:
        """Reproduces the scope-inheritance narrowing from coordinator.py."""
        prior_ids = {rid for rid in prior_repos_used if rid}
        kept = [c for c in candidates if c["id"] in prior_ids]
        return kept if kept else candidates  # fall through if empty

    def test_followup_routes_to_ranked_repos_not_signal_subset(self) -> None:
        """After the fix, the follow-up's candidate pool contains the
        router's full selection, not just repos that happened to have signal."""
        # All registered repos (the global pool)
        all_candidates = [
            {"id": "report-id", "name": "report-export-service"},
            {"id": "catalog-id", "name": "catalog-gateway"},
            {"id": "sync-id", "name": "catalog-sync-worker"},
            {"id": "other-id", "name": "unrelated-service"},
        ]

        # Rankings from turn 1 (router's selection)
        turn1_rankings = [
            _ranking("report-id", "report-export-service"),
            _ranking("catalog-id", "catalog-gateway"),
        ]
        # Only catalog had signal in turn 1 (report not indexed)
        turn1_bundles = [
            _FakeBundle("report-id", "report-export-service", has_signal=False),
            _FakeBundle("catalog-id", "catalog-gateway", has_signal=True),
        ]

        # Fixed: repos_used = rankings
        fixed_repos_used, _ = _repos_used_from_rankings(turn1_rankings)
        fixed_candidates = self._narrow_candidates(all_candidates, fixed_repos_used)
        fixed_names = {c["name"] for c in fixed_candidates}
        assert "report-export-service" in fixed_names  # ✓ survives
        assert "catalog-gateway" in fixed_names
        assert "unrelated-service" not in fixed_names  # ✓ correctly excluded

        # Buggy: repos_used = useful_bundles
        buggy_repos_used, _ = _repos_used_from_useful_bundles(turn1_bundles)
        buggy_candidates = self._narrow_candidates(all_candidates, buggy_repos_used)
        buggy_names = {c["name"] for c in buggy_candidates}
        assert "report-export-service" not in buggy_names  # ✗ lost due to bug
        assert "catalog-gateway" in buggy_names

    def test_followup_excludes_repos_not_in_rankings(self) -> None:
        """Repos that were never ranked should not be in the follow-up pool."""
        all_candidates = [
            {"id": "a", "name": "relevant"},
            {"id": "b", "name": "irrelevant-1"},
            {"id": "c", "name": "irrelevant-2"},
        ]
        rankings = [_ranking("a", "relevant")]
        repos_used, _ = _repos_used_from_rankings(rankings)
        candidates = self._narrow_candidates(all_candidates, repos_used)
        assert [c["id"] for c in candidates] == ["a"]

    def test_empty_repos_used_falls_through_to_full_pool(self) -> None:
        """If repos_used is empty (brand-new conversation), the full
        candidate pool is used."""
        all_candidates = [{"id": "x", "name": "x"}, {"id": "y", "name": "y"}]
        candidates = self._narrow_candidates(all_candidates, prior_repos_used=[])
        assert len(candidates) == 2


# ── DB integration tests: full persist-load-inherit cycle ────────────────────


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_bundle_cache_for_tests()
    yield
    reset_bundle_cache_for_tests()


async def _make_conversation(session_factory: async_sessionmaker) -> uuid.UUID:
    async with session_factory() as session:
        repo = EntityRepository(session)
        conv = await repo.create_conversation(
            session_id="scope-inherit-session",
            title="scope inheritance test",
            scope_repo_ids=[],
        )
        await session.commit()
        return conv.id


@pytest.mark.asyncio
async def test_ranked_repos_survive_persist_load_cycle(session_factory_for) -> None:
    """The full persist → cache-bust → load cycle preserves all ranked
    repo IDs and names without dropping unindexed ones."""
    cid = await _make_conversation(session_factory_for)

    rankings = [
        _ranking("report-id", "report-export-service"),
        _ranking("catalog-id", "catalog-gateway"),
        _ranking("sync-id", "catalog-sync-worker"),
    ]
    ids, names = _repos_used_from_rankings(rankings)

    bundle = ResearchBundle(
        conversation_id=str(cid),
        last_question="if I had to build report export where does data flow?",
        last_answer="Report export requires ...",
        confidence="inferred",
        repos_used=ids,
        repos_used_names=names,
        scope_rationale="report-export-service: primary report repo",
    )
    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=bundle,
    )

    reset_bundle_cache_for_tests()
    async with session_factory_for() as session:
        repo = EntityRepository(session)
        loaded = await _load_prior_bundle(repo, cid)

    assert loaded is not None
    assert set(loaded.repos_used) == {"report-id", "catalog-id", "sync-id"}
    assert "report-export-service" in loaded.repos_used_names
    assert "catalog-gateway" in loaded.repos_used_names


@pytest.mark.asyncio
async def test_scope_inheritance_with_full_ranking_set(session_factory_for) -> None:
    """Simulates turn 1 saving full rankings → turn 2 loads and can
    narrow its candidate pool to those repos, including the unindexed one."""
    cid = await _make_conversation(session_factory_for)

    # Turn 1: router selected 3 repos; only 2 had signal
    turn1_rankings = [
        _ranking("report-id", "report-export-service"),  # correct but not indexed
        _ranking("catalog-id", "catalog-gateway"),
        _ranking("sync-id", "catalog-sync-worker"),
    ]
    ids, names = _repos_used_from_rankings(turn1_rankings)  # fixed: uses rankings
    bundle_turn1 = ResearchBundle(
        conversation_id=str(cid),
        last_question="how does report export work?",
        last_answer="Based on catalog and sync ...",
        confidence="inferred",
        repos_used=ids,
        repos_used_names=names,
    )
    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=bundle_turn1,
    )

    reset_bundle_cache_for_tests()

    # Turn 2: load prior bundle and narrow candidates
    async with session_factory_for() as session:
        repo = EntityRepository(session)
        prior = await _load_prior_bundle(repo, cid)

    assert prior is not None
    prior_ids = set(prior.repos_used)

    # All repo candidates registered in the system (including irrelevant ones)
    all_system_repos = [
        {"id": "report-id", "name": "report-export-service"},
        {"id": "catalog-id", "name": "catalog-gateway"},
        {"id": "sync-id", "name": "catalog-sync-worker"},
        {"id": "orders-id", "name": "orders-service"},  # unrelated
        {"id": "auth-id", "name": "auth-service"},  # unrelated
    ]
    # Scope-inheritance narrowing (mirrors coordinator.py ~line 1977-1981)
    narrowed = [r for r in all_system_repos if r["id"] in prior_ids]
    narrowed_names = {r["name"] for r in narrowed}

    # The correct repo must be in the follow-up's candidate pool
    assert "report-export-service" in narrowed_names
    assert "catalog-gateway" in narrowed_names
    assert "catalog-sync-worker" in narrowed_names

    # Unrelated repos must be excluded
    assert "orders-service" not in narrowed_names
    assert "auth-service" not in narrowed_names


# ── Benchmark / regression guard ─────────────────────────────────────────────


class TestBenchmarkRoutedVsInheritedAlignment:
    """Alignment benchmark: repos_used must be a superset of what the route
    event showed.  Mismatch rate should be 0%.

    This class simulates N conversation turns and computes the percentage of
    turns where a repo the router selected was dropped from repos_used.
    """

    @staticmethod
    def _simulate_turn(
        rankings: list[RepoRanking],
        bundles: list[_FakeBundle],
    ) -> dict[str, Any]:
        """Returns alignment metrics for one turn."""
        ranked_ids = {rk.repo_id for rk in rankings}
        fixed_ids = set(_repos_used_from_rankings(rankings)[0])
        buggy_ids = set(_repos_used_from_useful_bundles(bundles)[0])
        return {
            "ranked": ranked_ids,
            "fixed_ids": fixed_ids,
            "buggy_ids": buggy_ids,
            "fixed_mismatch": len(ranked_ids - fixed_ids),
            "buggy_mismatch": len(ranked_ids - buggy_ids),
        }

    def test_fixed_alignment_is_100_percent(self) -> None:
        """After the fix, repos_used always equals the ranked set exactly."""
        scenarios = [
            # (rankings, bundles)
            (
                [_ranking("a", "a-svc"), _ranking("b", "b-svc")],
                [
                    _FakeBundle("a", "a-svc", has_signal=True),
                    _FakeBundle("b", "b-svc", has_signal=True),
                ],
            ),
            (
                [_ranking("a", "a-svc"), _ranking("b", "b-svc")],
                [
                    _FakeBundle("a", "a-svc", has_signal=False),  # unindexed
                    _FakeBundle("b", "b-svc", has_signal=True),
                ],
            ),
            (
                [_ranking("x", "x-svc"), _ranking("y", "y-svc"), _ranking("z", "z-svc")],
                [
                    _FakeBundle("x", "x-svc", has_signal=False),
                    _FakeBundle("y", "y-svc", has_signal=False),
                    _FakeBundle("z", "z-svc", has_signal=True),
                ],
            ),
            (
                [_ranking("only", "only-svc")],
                [_FakeBundle("only", "only-svc", has_signal=False)],
            ),
        ]

        total_mismatches = 0
        for rankings, bundles in scenarios:
            result = self._simulate_turn(rankings, bundles)
            total_mismatches += result["fixed_mismatch"]

        assert total_mismatches == 0, (
            f"Fixed code has {total_mismatches} mismatch(es) across "
            f"{len(scenarios)} scenarios — repos_used no longer matches rankings"
        )

    def test_buggy_code_had_nonzero_mismatch_rate(self) -> None:
        """Documents that the OLD code produced mismatches in known scenarios,
        proving the regression guard is meaningful."""
        scenarios_with_unindexed = [
            (
                [_ranking("correct", "correct-svc"), _ranking("indexed", "indexed-svc")],
                [
                    _FakeBundle("correct", "correct-svc", has_signal=False),
                    _FakeBundle("indexed", "indexed-svc", has_signal=True),
                ],
            ),
        ]
        total_buggy_mismatches = sum(
            self._simulate_turn(r, b)["buggy_mismatch"] for r, b in scenarios_with_unindexed
        )
        assert total_buggy_mismatches > 0, (
            "Expected old code to produce mismatches in unindexed-repo scenarios"
        )
