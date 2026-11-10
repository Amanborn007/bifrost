"""Tests for the keyword prefilter + squad-match helpers in
``bifrost.research.coordinator``.

Invariant: a term such as "dlvr" that survives stop-word filtering but
matches 0 repos by name must not be padded with unrelated repos.
Instead the query short-circuits with a clear "no match — please
rephrase" error so the user can fix it immediately.

Also covers the squad-aware filter that runs BEFORE keyword
prefilter — when "dlvr" matches a squad name in
``RepoOwnership.squad``, narrow the candidate pool to that squad's
repos so the keyword path never sees the no-match case for
team-scoped questions.

Tests use the live dev DB with an isolated URN namespace
(``urn:test:keyword-prefilter:*``) and clean up in ``finally``
blocks so multiple runs don't accumulate fixture rows.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import delete

from bifrost.api.deps import get_session_factory
from bifrost.cli.config import get_settings
from bifrost.research.coordinator import (
    _keyword_prefilter_repos,
    _question_keywords,
    _squad_match_filter,
)
from bifrost.storage.models import Repo, RepoOwnership


def _factory():
    return get_session_factory(get_settings())


# ── _question_keywords ───────────────────────────────────────────


def test_keywords_drop_stop_words() -> None:
    """Common words like "what", "does", "team" must not be returned —
    they don't discriminate."""
    out = _question_keywords("what does the dlvr team do?")
    assert "dlvr" in out
    assert "what" not in out
    assert "team" not in out
    assert "the" not in out


def test_keywords_drop_short_tokens() -> None:
    """Tokens shorter than 3 chars are noise — drop them."""
    out = _question_keywords("a b cd efg")
    assert out == ["efg"]


def test_keywords_lowercase() -> None:
    out = _question_keywords("Show me OrderRouter and checksum")
    assert "orderrouter" in out
    assert "checksum" in out


def test_keywords_handles_empty_input() -> None:
    assert _question_keywords("") == []
    assert _question_keywords(None) == []  # type: ignore[arg-type]


def test_keywords_handles_punctuation() -> None:
    """Alpha-numeric tokens only — punctuation gets stripped."""
    out = _question_keywords("dlvr's orders-flow, when does run?")
    assert "dlvr" in out
    assert "orders" in out
    assert "flow" in out
    assert "run" in out  # exactly 3 chars — at threshold
    assert "when" not in out  # stop-word
    assert "does" not in out  # stop-word


# ── _keyword_prefilter_repos: trigger conditions ─────────────────


@pytest.mark.asyncio
async def test_under_threshold_returns_input_unchanged() -> None:
    """When candidates fit under the cap, the prefilter is a no-op."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    repos = [_make_test_repo(f"under-thresh-{test_id}-{i}", i) for i in range(10)]
    async with session_factory() as session:
        for r in repos:
            session.add(r)
        await session.commit()
    try:
        async with session_factory() as session:
            out, drop, reason = await _keyword_prefilter_repos(
                session,
                "anything",
                repos,
                max_count=50,
            )
        assert reason == "under_threshold"
        assert drop == 0
        assert len(out) == 10
    finally:
        async with session_factory() as session:
            await session.execute(delete(Repo).where(Repo.id.in_([r.id for r in repos])))
            await session.commit()


@pytest.mark.asyncio
async def test_no_tokens_pads_alphabetical() -> None:
    """When the question is all stop-words, fall back to alphabetical
    padding — best we can do without signal. This path should remain;
    the matched=0 short-circuit only triggers when tokens DID exist."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    # 60 candidates so we exceed the default max_count=50 threshold.
    repos = [_make_test_repo(f"no-tokens-{test_id}-{i:03d}", i) for i in range(60)]
    async with session_factory() as session:
        for r in repos:
            session.add(r)
        await session.commit()
    try:
        async with session_factory() as session:
            out, drop, reason = await _keyword_prefilter_repos(
                session,
                "do you team how the and",  # all stop-words
                repos,
                max_count=50,
            )
        assert reason == "no_tokens"
        assert len(out) == 50
        assert drop == 10
    finally:
        async with session_factory() as session:
            await session.execute(delete(Repo).where(Repo.id.in_([r.id for r in repos])))
            await session.commit()


@pytest.mark.asyncio
async def test_no_keyword_match_returns_empty_with_reason() -> None:
    """The CRITICAL fix: a question with real keywords that match 0
    repos must return ``([], len(candidates), "no_keyword_match")``
    so the caller can emit a "rephrase please" error and STOP — NOT
    pad with alphabetical-first 50 like the buggy old version."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    # 60 candidates whose names contain NONE of the keywords below.
    repos = [_make_test_repo(f"alpha-svc-{test_id}-{i:03d}", i) for i in range(60)]
    async with session_factory() as session:
        for r in repos:
            session.add(r)
        await session.commit()
    try:
        async with session_factory() as session:
            out, drop, reason = await _keyword_prefilter_repos(
                session,
                "what does dlvr team do",
                repos,
                max_count=50,
            )
        assert reason == "no_keyword_match"
        assert out == []
        assert drop == len(repos)
    finally:
        async with session_factory() as session:
            await session.execute(delete(Repo).where(Repo.id.in_([r.id for r in repos])))
            await session.commit()


@pytest.mark.asyncio
async def test_partial_match_pads_with_non_matched() -> None:
    """When 1 ≤ matched < max_count, pad with non-matched repos so
    the router sees breadth. Reason is ``matched_padded``."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    matching = [_make_test_repo(f"orders-svc-{test_id}-{i}", i) for i in range(3)]
    other = [_make_test_repo(f"other-svc-{test_id}-{i:03d}", 100 + i) for i in range(60)]
    repos = matching + other
    async with session_factory() as session:
        for r in repos:
            session.add(r)
        await session.commit()
    try:
        async with session_factory() as session:
            out, drop, reason = await _keyword_prefilter_repos(
                session,
                "how does orders work",
                repos,
                max_count=50,
            )
        assert reason == "matched_padded"
        assert len(out) == 50
        # Matching repos must appear first — the LIKE query orders
        # by is_primary then name, but our test repos are all
        # is_primary=False, so name-asc sort kicks in. The
        # "orders-svc-" prefix sorts before "other-svc-" only if
        # 'p' < 'o' (false). The contract is that ALL 3 matching
        # are present in the output; ordering within bucket isn't
        # what we're testing here.
        out_names = {r.name for r in out}
        for m in matching:
            assert m.name in out_names
    finally:
        async with session_factory() as session:
            await session.execute(delete(Repo).where(Repo.id.in_([r.id for r in repos])))
            await session.commit()


@pytest.mark.asyncio
async def test_full_match_capped_at_max() -> None:
    """When matched > max_count, return exactly max_count with reason
    ``matched_capped``."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    # 60 repos all with "orders" in the name → full match.
    repos = [_make_test_repo(f"orders-svc-{test_id}-{i:03d}", i) for i in range(60)]
    async with session_factory() as session:
        for r in repos:
            session.add(r)
        await session.commit()
    try:
        async with session_factory() as session:
            out, drop, reason = await _keyword_prefilter_repos(
                session,
                "how does orders work",
                repos,
                max_count=50,
            )
        assert reason == "matched_capped"
        assert len(out) == 50
        assert drop == 10
    finally:
        async with session_factory() as session:
            await session.execute(delete(Repo).where(Repo.id.in_([r.id for r in repos])))
            await session.commit()


# ── _squad_match_filter ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_squad_match_narrows_to_squad_repos() -> None:
    """The dlvr-team fix: when a question token matches a squad name
    in RepoOwnership, narrow candidates to that squad's repos."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    dlvr_repos = [_make_test_repo(f"random-name-{test_id}-dlvr-{i}", i) for i in range(3)]
    other_repos = [_make_test_repo(f"other-{test_id}-{i:03d}", 100 + i) for i in range(57)]
    repos = dlvr_repos + other_repos
    async with session_factory() as session:
        for r in repos:
            session.add(r)
        await session.flush()
        # Tag dlvr_repos as owned by squad="dlvr".
        for r in dlvr_repos:
            session.add(
                RepoOwnership(
                    repo_id=r.id,
                    squad="dlvr",
                )
            )
        await session.commit()
    try:
        async with session_factory() as session:
            out, squads = await _squad_match_filter(
                session,
                repos,
                "what does dlvr team do",
            )
        assert squads == ["dlvr"]
        assert sorted(r.id for r in out) == sorted(r.id for r in dlvr_repos)
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(RepoOwnership).where(RepoOwnership.repo_id.in_([r.id for r in dlvr_repos]))
            )
            await session.execute(delete(Repo).where(Repo.id.in_([r.id for r in repos])))
            await session.commit()


@pytest.mark.asyncio
async def test_squad_match_normalises_separators() -> None:
    """Squad "d-lvr", "d_lvr", "D LVR", "dLvr", "dlvr" all match the
    token "dlvr" (and variants). Without this, casing/punctuation
    drift between the ownership manifest and questions silently misses repos."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    repo = _make_test_repo(f"some-repo-{test_id}", 0)
    async with session_factory() as session:
        session.add(repo)
        await session.flush()
        session.add(
            RepoOwnership(
                repo_id=repo.id,
                squad="d-lvr",  # ownership manifests may use kebab-case
            )
        )
        await session.commit()
    try:
        async with session_factory() as session:
            out, squads = await _squad_match_filter(
                session,
                [repo],
                "what does dlvr team do",
            )
        assert squads == ["d-lvr"]
        assert len(out) == 1
    finally:
        async with session_factory() as session:
            await session.execute(delete(RepoOwnership).where(RepoOwnership.repo_id == repo.id))
            await session.execute(delete(Repo).where(Repo.id == repo.id))
            await session.commit()


@pytest.mark.asyncio
async def test_squad_match_no_match_returns_input_unchanged() -> None:
    """When no question token matches any squad, return the input
    candidates list verbatim + an empty squad list. The caller
    proceeds to keyword prefilter as usual."""
    session_factory = _factory()
    test_id = _uuid.uuid4()
    repo = _make_test_repo(f"plain-repo-{test_id}", 0)
    async with session_factory() as session:
        session.add(repo)
        await session.flush()
        session.add(
            RepoOwnership(
                repo_id=repo.id,
                squad="orders",
            )
        )
        await session.commit()
    try:
        async with session_factory() as session:
            out, squads = await _squad_match_filter(
                session,
                [repo],
                "totally unrelated question",
            )
        assert squads == []
        assert out is not None
        assert len(out) == 1
    finally:
        async with session_factory() as session:
            await session.execute(delete(RepoOwnership).where(RepoOwnership.repo_id == repo.id))
            await session.execute(delete(Repo).where(Repo.id == repo.id))
            await session.commit()


@pytest.mark.asyncio
async def test_squad_match_empty_question_is_noop() -> None:
    out, squads = await _squad_match_filter(None, [], "")  # type: ignore[arg-type]
    assert out == []
    assert squads == []


# ── Helpers ──────────────────────────────────────────────────────


def _make_test_repo(name: str, sort_idx: int) -> Repo:
    """Build a Repo row that belongs to the keyword-prefilter tests'
    isolated namespace. Callers are responsible for cleanup."""
    return Repo(
        id=_uuid.uuid4(),
        urn=f"urn:bifrost:repo/{name}",
        name=name,
        url=f"https://example.invalid/{name}",
        adapter_config={"description": ""},
        is_primary=False,
    )
