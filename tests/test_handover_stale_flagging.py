"""Unit tests for ``EntityRepository.mark_handover_docs_stale_by_repo_name``.

The helper goes beyond a repo-only flip: it also flags
domain and team handover docs whose member-repo just re-extracted.
These tests use an ``AsyncMock`` session so they're DB-free; they assert
the helper:

  * issues lookup queries for domain + squads,
  * builds the OR'd UPDATE predicate with the right scope clauses, and
  * returns / logs the rowcount.

The integration coverage that exercises real PostgreSQL lives in
``tests/test_repository.py`` and is unaffected by this file.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bifrost.storage.repository import EntityRepository


def _make_session(
    *,
    domain_name: str | None,
    squads: list[str],
    rowcount: int,
) -> MagicMock:
    """Build an AsyncMock session that answers the helper's three queries.

    Order of execute() calls:
      1. SELECT Domain.name JOIN Repo  → returns ``domain_name`` (or None)
      2. SELECT OrgRepo.squad JOIN Repo → returns ``squads``
      3. UPDATE handover_docs           → returns ``rowcount``
    """
    domain_result = MagicMock()
    domain_result.scalar_one_or_none = MagicMock(return_value=domain_name)

    squads_scalars = MagicMock()
    squads_scalars.all = MagicMock(return_value=squads)
    squads_result = MagicMock()
    squads_result.scalars = MagicMock(return_value=squads_scalars)

    update_result = MagicMock()
    update_result.rowcount = rowcount

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[domain_result, squads_result, update_result],
    )
    return session


@pytest.mark.asyncio
async def test_repo_with_domain_and_squads_flags_all_three_scopes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repo with a domain AND squads → UPDATE references all three scope_kinds."""
    session = _make_session(
        domain_name="orders",
        squads=["orders-platform"],
        rowcount=3,
    )
    repo = EntityRepository(session)

    with caplog.at_level(logging.INFO, logger="bifrost.storage.repository"):
        flagged = await repo.mark_handover_docs_stale_by_repo_name("batch-orders")

    assert flagged == 3
    # Three execute() calls: domain SELECT, squads SELECT, UPDATE.
    assert session.execute.await_count == 3

    # Inspect the UPDATE statement (3rd call) — assert all three scope_kinds
    # appear in its compiled form.
    update_stmt = session.execute.await_args_list[2].args[0]
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "scope_kind = 'repo'" in sql
    assert "scope_value = 'batch-orders'" in sql
    assert "scope_kind = 'domain'" in sql
    assert "scope_value = 'orders'" in sql
    assert "scope_kind = 'team'" in sql
    assert "orders-platform" in sql
    assert "freshness_state = 'fresh'" in sql

    # Log line shape: repo / domain / squad / flagged.
    assert any(
        "auto-flag-stale" in r.message
        and "repo=batch-orders" in r.message
        and "domain=orders" in r.message
        and "squad=orders-platform" in r.message
        and "flagged=3" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_repo_with_no_domain_no_squad_falls_back_to_repo_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repo with neither domain nor squad → UPDATE only flips the repo scope.

    Preserves repo-only behavior so legacy callers / unregistered repos still
    work.
    """
    session = _make_session(domain_name=None, squads=[], rowcount=1)
    repo = EntityRepository(session)

    with caplog.at_level(logging.INFO, logger="bifrost.storage.repository"):
        flagged = await repo.mark_handover_docs_stale_by_repo_name("orphan-repo")

    assert flagged == 1
    update_stmt = session.execute.await_args_list[2].args[0]
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "scope_kind = 'repo'" in sql
    assert "scope_value = 'orphan-repo'" in sql
    # Domain / team scopes must NOT appear when the repo has neither.
    assert "scope_kind = 'domain'" not in sql
    assert "scope_kind = 'team'" not in sql

    # Log shows the dashes for missing values.
    assert any(
        "repo=orphan-repo" in r.message
        and "domain=-" in r.message
        and "squad=-" in r.message
        and "flagged=1" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_repo_in_multiple_squads_flags_all_team_handovers() -> None:
    """A repo registered under multiple ``org_repos`` squads → IN-clause hits all."""
    session = _make_session(
        domain_name=None,
        squads=["squad-a", "squad-b"],
        rowcount=2,
    )
    repo = EntityRepository(session)

    flagged = await repo.mark_handover_docs_stale_by_repo_name("multi-squad-repo")

    assert flagged == 2
    update_stmt = session.execute.await_args_list[2].args[0]
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    # Both squads land in the IN(...) clause.
    assert "squad-a" in sql
    assert "squad-b" in sql
    assert "scope_kind = 'team'" in sql


@pytest.mark.asyncio
async def test_zero_rowcount_returns_zero() -> None:
    """No matching fresh rows → return 0, no exception."""
    session = _make_session(domain_name="orders", squads=[], rowcount=0)
    repo = EntityRepository(session)

    flagged = await repo.mark_handover_docs_stale_by_repo_name("quiet-repo")

    assert flagged == 0


@pytest.mark.asyncio
async def test_negative_rowcount_coerced_to_zero() -> None:
    """Some drivers return -1 for UPDATE rowcount — must clamp to 0."""
    session = _make_session(domain_name=None, squads=[], rowcount=-1)
    repo = EntityRepository(session)

    flagged = await repo.mark_handover_docs_stale_by_repo_name("any-repo")

    assert flagged == 0
