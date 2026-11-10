"""Unit tests for bidirectional staleness propagation.

Two helpers in :mod:`bifrost.storage.repository`:

  * ``mark_parent_handovers_enriched(repo_name)`` — when a child repo
    handover regenerates, flag fresh parent (domain/team/region)
    handovers as ``enriched_by_children``.
  * ``mark_children_validated_by_parent(parent_doc_id)`` — when a
    parent (domain/team/region) handover regenerates, bump
    ``last_validated_at`` on every child repo handover under it.

These tests use ``AsyncMock`` sessions so they're DB-free; they assert
the helper:

  * issues the right lookup queries,
  * builds the UPDATE predicate with the right scope clauses,
  * returns / logs the rowcount,
  * preserves "stale wins" by only filtering on ``freshness_state == 'fresh'``,
  * skips non-parent scopes for the children helper.

The integration coverage that exercises real PostgreSQL lives in
``tests/test_repository.py`` and is unaffected by this file.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from bifrost.storage.repository import EntityRepository

# ── mark_parent_handovers_enriched ────────────────────────────────────


def _make_enriched_session(
    *,
    domain_name: str | None,
    squads: list[str],
    regions: list[str],
    rowcount: int,
) -> MagicMock:
    """Build an AsyncMock session that answers the helper's four queries.

    Order of execute() calls inside ``mark_parent_handovers_enriched``:
      1. SELECT Domain.name JOIN Repo  → ``domain_name`` (or None)
      2. SELECT OrgRepo.squad JOIN Repo → ``squads``
      3. SELECT OrgRepo.region JOIN Repo → ``regions``
      4. UPDATE handover_docs           → ``rowcount``
    """
    domain_result = MagicMock()
    domain_result.scalar_one_or_none = MagicMock(return_value=domain_name)

    squads_scalars = MagicMock()
    squads_scalars.all = MagicMock(return_value=squads)
    squads_result = MagicMock()
    squads_result.scalars = MagicMock(return_value=squads_scalars)

    regions_scalars = MagicMock()
    regions_scalars.all = MagicMock(return_value=regions)
    regions_result = MagicMock()
    regions_result.scalars = MagicMock(return_value=regions_scalars)

    update_result = MagicMock()
    update_result.rowcount = rowcount

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            domain_result,
            squads_result,
            regions_result,
            update_result,
        ],
    )
    return session


@pytest.mark.asyncio
async def test_enriched_with_domain_squad_region_flags_all_three_parent_scopes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repo with domain + squad + region → UPDATE references all three parent
    scopes and sets ``enriched_by_children``.

    Critically, the UPDATE filters ``freshness_state == 'fresh'`` — already-
    stale parents are not downgraded (stale wins).
    """
    session = _make_enriched_session(
        domain_name="orders",
        squads=["orders-platform"],
        regions=["eu-west"],
        rowcount=3,
    )
    repo = EntityRepository(session)

    with caplog.at_level(logging.INFO, logger="bifrost.storage.repository"):
        flagged = await repo.mark_parent_handovers_enriched("batch-orders")

    assert flagged == 3
    # Four execute() calls: domain, squads, regions, UPDATE.
    assert session.execute.await_count == 4

    update_stmt = session.execute.await_args_list[3].args[0]
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    # Parent scopes appear.
    assert "scope_kind = 'domain'" in sql
    assert "scope_value = 'orders'" in sql
    assert "scope_kind = 'team'" in sql
    assert "orders-platform" in sql
    assert "scope_kind = 'region'" in sql
    assert "eu-west" in sql
    # Repo-scope is NOT touched here — that's the by-repo-name path's job.
    assert "scope_kind = 'repo'" not in sql
    # Stale-wins guard: only fresh rows flip.
    assert "freshness_state = 'fresh'" in sql
    # Target state is the soft "enriched" hint. SQLAlchemy compiles the
    # SET clause without spaces around ``=`` so we match the raw token.
    assert "'enriched_by_children'" in sql

    assert any(
        "auto-flag-enriched" in r.message
        and "repo=batch-orders" in r.message
        and "domain=orders" in r.message
        and "squad=orders-platform" in r.message
        and "region=eu-west" in r.message
        and "flagged=3" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_enriched_orphan_repo_is_silent_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repo with no domain, no squad, no region → no UPDATE issued, returns 0.

    This is the orphan-repo case (e.g., a repo registered without a manifest
    or domain linkage). No parent handovers can possibly cover it, so we
    don't even compile an UPDATE.
    """
    session = _make_enriched_session(
        domain_name=None,
        squads=[],
        regions=[],
        rowcount=0,
    )
    repo = EntityRepository(session)

    with caplog.at_level(logging.INFO, logger="bifrost.storage.repository"):
        flagged = await repo.mark_parent_handovers_enriched("orphan-repo")

    assert flagged == 0
    # Three SELECTs ran; the UPDATE is short-circuited.
    assert session.execute.await_count == 3

    assert any(
        "auto-flag-enriched" in r.message
        and "repo=orphan-repo" in r.message
        and "flagged=0" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_enriched_negative_rowcount_clamped() -> None:
    """Some drivers return -1 — clamp to 0."""
    session = _make_enriched_session(
        domain_name="orders",
        squads=[],
        regions=[],
        rowcount=-1,
    )
    repo = EntityRepository(session)
    flagged = await repo.mark_parent_handovers_enriched("any-repo")
    assert flagged == 0


# ── mark_children_validated_by_parent ────────────────────────────────


def _make_validate_children_session(
    *,
    parent_doc: MagicMock | None,
    child_repo_names: list[str],
    rowcount: int,
) -> MagicMock:
    """Build an AsyncMock session for the validate-children helper.

    Order of session calls:
      1. session.get(HandoverDoc, parent_doc_id) → ``parent_doc`` (or None)
      2. session.execute(SELECT child repo names)
      3. session.execute(UPDATE last_validated_at on child repo handovers)
    """
    child_scalars = MagicMock()
    child_scalars.all = MagicMock(return_value=child_repo_names)
    child_select_result = MagicMock()
    child_select_result.scalars = MagicMock(return_value=child_scalars)

    update_result = MagicMock()
    update_result.rowcount = rowcount

    session = MagicMock()
    session.get = AsyncMock(return_value=parent_doc)
    session.execute = AsyncMock(
        side_effect=[child_select_result, update_result],
    )
    return session


@pytest.mark.asyncio
async def test_validate_children_domain_parent_bumps_member_repos(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Domain parent handover → bump last_validated_at on all member repo handovers."""
    parent = MagicMock()
    parent.scope_kind = "domain"
    parent.scope_value = "orders"

    session = _make_validate_children_session(
        parent_doc=parent,
        child_repo_names=["batch-orders", "labels-service"],
        rowcount=2,
    )
    repo = EntityRepository(session)
    parent_id = uuid.uuid4()

    with caplog.at_level(logging.INFO, logger="bifrost.storage.repository"):
        flagged = await repo.mark_children_validated_by_parent(parent_id)

    assert flagged == 2
    assert session.execute.await_count == 2

    update_stmt = session.execute.await_args_list[1].args[0]
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    # Targets repo-scope rows for the resolved repo names.
    assert "scope_kind = 'repo'" in sql
    assert "batch-orders" in sql
    assert "labels-service" in sql
    # Sets the timestamp; doesn't touch freshness_state.
    assert "last_validated_at" in sql
    assert "freshness_state" not in sql.split("SET")[1] if "SET" in sql else True

    assert any(
        "validate-children" in r.message
        and "scope=domain/orders" in r.message
        and "repos=2" in r.message
        and "flagged=2" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_validate_children_repo_scope_parent_is_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Parent doc with scope_kind='repo' has no children — short-circuit."""
    parent = MagicMock()
    parent.scope_kind = "repo"
    parent.scope_value = "batch-orders"

    session = MagicMock()
    session.get = AsyncMock(return_value=parent)
    session.execute = AsyncMock()
    repo = EntityRepository(session)
    parent_id = uuid.uuid4()

    with caplog.at_level(logging.INFO, logger="bifrost.storage.repository"):
        flagged = await repo.mark_children_validated_by_parent(parent_id)

    assert flagged == 0
    # No SELECT/UPDATE issued — repo-scope short-circuits before the query.
    assert session.execute.await_count == 0

    assert any("non_parent_scope" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_validate_children_missing_parent_returns_zero() -> None:
    """Parent doc id doesn't exist → silent no-op, return 0."""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    repo = EntityRepository(session)

    flagged = await repo.mark_children_validated_by_parent(uuid.uuid4())

    assert flagged == 0
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_validate_children_empty_scope_returns_zero() -> None:
    """Domain has zero member repos → return 0, no UPDATE issued."""
    parent = MagicMock()
    parent.scope_kind = "team"
    parent.scope_value = "ghost-squad"

    session = _make_validate_children_session(
        parent_doc=parent,
        child_repo_names=[],
        rowcount=0,
    )
    repo = EntityRepository(session)

    flagged = await repo.mark_children_validated_by_parent(uuid.uuid4())

    assert flagged == 0
    # Only the SELECT for child repo names ran; UPDATE is skipped.
    assert session.execute.await_count == 1
