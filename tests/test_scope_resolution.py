"""Scope resolution edge cases — silent fallback to org-wide.

Regression: wrong repos were picked as "main" repos for a
team. Two contributing factors:

  1. Squad-name matching between Domain row and ownership-manifest-derived
     OrgRepo.squad was case-insensitive but NOT punctuation-tolerant
     ('d-lvr' vs 'DLVR' vs 'dLvr' all needed to match).
  2. When the user picked a domain with no registered repos, the
     coordinator silently fell through to the unscoped path. The
     user thought they were querying their team; the system was
     querying the world.

These tests cover the catalog-candidate normalization. The
coordinator-side fallback messaging is exercised in run_research's
yield sequence and is hard to assert in isolation; the new UI
reducer case is covered in research-progress.test.tsx.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.research.coordinator import _catalog_candidates_for_domain
from bifrost.storage.models import Domain, OrgRepo


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_domain(session, name: str) -> Domain:
    d = Domain(id=uuid.uuid4(), name=name, urn=f"urn:bifrost:domain:{name.lower()}")
    session.add(d)
    await session.flush()
    return d


async def _make_org_repo(
    session,
    *,
    name: str,
    squad: str,
    archived: bool = False,
    is_fork: bool = False,
) -> OrgRepo:
    r = OrgRepo(
        id=uuid.uuid4(),
        owner="AcmeCo",
        name=name,
        url=f"https://github.com/AcmeCo/{name}",
        squad=squad,
        archived=archived,
        is_fork=is_fork,
    )
    session.add(r)
    await session.flush()
    return r


@pytest.mark.asyncio
async def test_squad_match_is_case_insensitive(
    session_factory_for,
    db_session,
) -> None:
    domain = await _make_domain(db_session, "dlvr")
    await _make_org_repo(db_session, name="dlvr-svc-1", squad="DLVR")
    await _make_org_repo(db_session, name="dlvr-svc-2", squad="dLvr")
    await _make_org_repo(db_session, name="other", squad="other-team")
    await db_session.flush()

    rows = await _catalog_candidates_for_domain(
        db_session,
        domain,
        registered_names=set(),
    )
    names = sorted(r.name for r in rows)
    assert names == ["dlvr-svc-1", "dlvr-svc-2"]


@pytest.mark.asyncio
async def test_squad_match_handles_hyphen_underscore_space(
    session_factory_for,
    db_session,
) -> None:
    domain = await _make_domain(db_session, "d-lvr")
    await _make_org_repo(db_session, name="r1", squad="dlvr")
    await _make_org_repo(db_session, name="r2", squad="d_lvr")
    await _make_org_repo(db_session, name="r3", squad="d lvr")
    await _make_org_repo(db_session, name="r4", squad="D-LVR")
    await db_session.flush()

    rows = await _catalog_candidates_for_domain(
        db_session,
        domain,
        registered_names=set(),
    )
    names = sorted(r.name for r in rows)
    assert names == ["r1", "r2", "r3", "r4"]


@pytest.mark.asyncio
async def test_excludes_already_registered_repos(
    session_factory_for,
    db_session,
) -> None:
    domain = await _make_domain(db_session, "orders-x")
    await _make_org_repo(db_session, name="order-1", squad="orders-x")
    await _make_org_repo(db_session, name="order-2", squad="orders-x")
    await db_session.flush()

    rows = await _catalog_candidates_for_domain(
        db_session,
        domain,
        registered_names={"order-1"},
    )
    names = [r.name for r in rows]
    assert names == ["order-2"]


@pytest.mark.asyncio
async def test_excludes_archived_and_forks(
    session_factory_for,
    db_session,
) -> None:
    domain = await _make_domain(db_session, "dlvr-arch")
    await _make_org_repo(db_session, name="alive", squad="dlvr-arch")
    await _make_org_repo(db_session, name="dead", squad="dlvr-arch", archived=True)
    await _make_org_repo(db_session, name="fork", squad="dlvr-arch", is_fork=True)
    await db_session.flush()

    rows = await _catalog_candidates_for_domain(
        db_session,
        domain,
        registered_names=set(),
    )
    assert [r.name for r in rows] == ["alive"]


@pytest.mark.asyncio
async def test_returns_empty_for_no_squad_match(
    session_factory_for,
    db_session,
) -> None:
    domain = await _make_domain(db_session, "no-team")
    await _make_org_repo(db_session, name="someone", squad="other-team")
    await db_session.flush()

    rows = await _catalog_candidates_for_domain(
        db_session,
        domain,
        registered_names=set(),
    )
    assert rows == []


@pytest.mark.asyncio
async def test_returns_empty_when_no_domain(db_session) -> None:
    rows = await _catalog_candidates_for_domain(
        db_session,
        None,
        registered_names=set(),
    )
    assert rows == []
