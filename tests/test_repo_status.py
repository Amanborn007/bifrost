"""Repo status classifier tests.

Pure-data tests for ``bifrost.research.status.classify_repo_status``
— no DB. The classifier takes already-loaded ``code_entity_counts``
and ``fresh_by_id`` so we can exercise every branch from in-memory
fixtures.

Coverage targets the four status values:

  * deep_indexed     — entities present, freshness clean
  * manifest_only       — zero entities, freshness clean
  * not_cloned       — freshness error (regardless of entity count)
  * auto_registered  — id in ``auto_registered_ids`` set

Plus the helpers (``status_summary``, ``manifest_only_ids``,
``deep_indexed_ids``) and the order-preservation contract.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

from bifrost.research.freshness import RepoFreshness as RepoFreshState
from bifrost.research.status import (
    classify_repo_status,
    deep_indexed_ids,
    manifest_only_ids,
    status_summary,
)


# Tiny stand-in for the ORM Repo row — classifier only reads .id and .name.
@dataclass
class _StubRepo:
    id: object
    name: str


def _repo(name: str) -> _StubRepo:
    return _StubRepo(id=_uuid.uuid4(), name=name)


def _ok_fresh(rid: str) -> RepoFreshState:
    return RepoFreshState(
        repo_id=rid,
        repo_name=f"repo-{rid[:6]}",
        clone_path="/tmp/x",
        remote_head="abc",
        local_head="abc",
        last_extracted_sha=None,
        metadata_stale=False,
        commits_behind=0,
        ensured_cloned=False,
        error=None,
    )


def _bad_fresh(rid: str, err: str) -> RepoFreshState:
    return RepoFreshState(
        repo_id=rid,
        repo_name=f"repo-{rid[:6]}",
        clone_path="/tmp/x",
        remote_head="",
        local_head="",
        last_extracted_sha=None,
        metadata_stale=True,
        commits_behind=0,
        ensured_cloned=False,
        error=err,
    )


# ── Status branches ──────────────────────────────────────────────


def test_deep_indexed_when_entities_present_and_clone_clean() -> None:
    r = _repo("svc-orders")
    rid = str(r.id)
    out = classify_repo_status(
        [r],
        code_entity_counts={rid: 42},
        fresh_by_id={rid: _ok_fresh(rid)},
    )
    info = out[rid]
    assert info.status == "deep_indexed"
    assert info.code_entity_count == 42
    assert info.clone_error is None


def test_manifest_only_when_zero_entities_and_clone_clean() -> None:
    r = _repo("svc-untouched")
    rid = str(r.id)
    out = classify_repo_status(
        [r],
        code_entity_counts={},
        fresh_by_id={rid: _ok_fresh(rid)},
    )
    assert out[rid].status == "manifest_only"
    assert out[rid].code_entity_count == 0


def test_not_cloned_takes_precedence_over_entity_count() -> None:
    """A clone error means we can't trust the repo regardless of how
    many entities we'd previously extracted. This matters when a repo
    was deleted upstream after extraction."""
    r = _repo("svc-deleted-upstream")
    rid = str(r.id)
    out = classify_repo_status(
        [r],
        code_entity_counts={rid: 99},  # had data
        fresh_by_id={rid: _bad_fresh(rid, "remote not found")},
    )
    assert out[rid].status == "not_cloned"
    assert out[rid].clone_error == "remote not found"


def test_auto_registered_takes_precedence_over_manifest_only() -> None:
    """auto_registered is a *fresher* signal than manifest_only — both
    have zero entities, but auto_registered tells the UI 'we just
    discovered this one in the org catalog this turn'."""
    r = _repo("svc-just-registered")
    rid = str(r.id)
    out = classify_repo_status(
        [r],
        code_entity_counts={},
        fresh_by_id={rid: _ok_fresh(rid)},
        auto_registered_ids={rid},
    )
    assert out[rid].status == "auto_registered"
    assert out[rid].auto_registered is True


def test_auto_registered_with_clone_error_stays_not_cloned() -> None:
    """If the auto-registered repo can't be cloned (e.g. private repo
    we don't have a token for), the status is ``not_cloned`` — that's
    the actionable signal. The UI can still show the `auto_registered`
    flag separately via ``RepoStatusInfo.auto_registered``."""
    r = _repo("svc-private")
    rid = str(r.id)
    out = classify_repo_status(
        [r],
        code_entity_counts={},
        fresh_by_id={rid: _bad_fresh(rid, "permission denied")},
        auto_registered_ids={rid},
    )
    assert out[rid].status == "not_cloned"
    assert out[rid].auto_registered is True
    assert out[rid].clone_error == "permission denied"


def test_no_freshness_data_skips_clone_check() -> None:
    """When called before freshness is computed, no clone-state
    classification — entity count alone decides."""
    r = _repo("svc-pre-freshness")
    rid = str(r.id)
    out = classify_repo_status(
        [r],
        code_entity_counts={rid: 5},
        fresh_by_id=None,
    )
    assert out[rid].status == "deep_indexed"


# ── Multi-repo summary + helpers ─────────────────────────────────


def test_classify_three_kinds_in_one_call() -> None:
    """Realistic mixed input: 2 deep_indexed + 2 manifest_only + 1
    auto_registered + 1 not_cloned. Counts should match exactly; the
    helper getters should pull the right ids."""
    deep = [_repo("d1"), _repo("d2")]
    manifest_only_repos = [_repo("f1"), _repo("f2")]
    new_one = _repo("n1")
    bad = _repo("b1")

    fresh = {str(r.id): _ok_fresh(str(r.id)) for r in deep + manifest_only_repos + [new_one]}
    fresh[str(bad.id)] = _bad_fresh(str(bad.id), "auth")

    counts = {str(r.id): 10 for r in deep}  # manifest-only + new + bad have zero

    out = classify_repo_status(
        deep + manifest_only_repos + [new_one, bad],
        code_entity_counts=counts,
        fresh_by_id=fresh,
        auto_registered_ids={str(new_one.id)},
    )
    summary = status_summary(out)
    assert summary == {
        "deep_indexed": 2,
        "manifest_only": 2,
        "not_cloned": 1,
        "auto_registered": 1,
    }
    assert set(deep_indexed_ids(out)) == {str(r.id) for r in deep}
    # manifest_only_ids includes BOTH manifest_only and auto_registered —
    # that's the auto-extract eligible set.
    assert set(manifest_only_ids(out)) == {str(r.id) for r in manifest_only_repos + [new_one]}


def test_status_summary_zero_when_no_repos() -> None:
    out: dict = {}
    assert status_summary(out) == {
        "deep_indexed": 0,
        "manifest_only": 0,
        "not_cloned": 0,
        "auto_registered": 0,
    }


def test_classify_preserves_input_order() -> None:
    """UI shows repos in discovery order — make sure the dict
    iteration order matches the ``repos`` argument."""
    repos = [_repo("a"), _repo("b"), _repo("c")]
    counts = {str(r.id): 1 for r in repos}
    fresh = {str(r.id): _ok_fresh(str(r.id)) for r in repos}
    out = classify_repo_status(
        repos,
        code_entity_counts=counts,
        fresh_by_id=fresh,
    )
    assert list(out.keys()) == [str(r.id) for r in repos]


def test_classify_handles_empty_input() -> None:
    out = classify_repo_status(
        [],
        code_entity_counts={},
        fresh_by_id={},
    )
    assert out == {}
    assert deep_indexed_ids(out) == []
    assert manifest_only_ids(out) == []
