"""apply_graph_plan / build_service_graph against a real Postgres (skipped without one)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import ConventionsConfig, ManifestConfig
from bifrost.manifest.files import LocalRepoFiles
from bifrost.manifest.graph_apply import apply_graph_plan, build_service_graph
from bifrost.manifest.graph_plan import GraphPlan, plan_service_graph
from bifrost.manifest.loader import load_manifest
from bifrost.storage.models import Domain, Entity, EntityRelationship, Repo

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("BIFROST_SKIP_DB_MIGRATIONS")),
    reason="needs a real Postgres (BIFROST_SKIP_DB_MIGRATIONS is set)",
)

ACME = Path(__file__).resolve().parents[1] / "examples" / "acmeco"
REPOS = ["delivery-gateway", "dispatcher", "signing-keys", "usage-metering", "webhook-api"]
SERVICE_URNS = [f"urn:bifrost:service:{r}" for r in REPOS]
DOMAIN_URNS = ["urn:bifrost:domain:dlvr", "urn:bifrost:domain:plat"]
OTHER_DOMAIN_URN = "urn:bifrost:domain:apply-test-other"
# depends_on 4 + calls_service 2 + shares_topic 2 + publishes_to 0 (tests/test_service_graph_plan.py)
GOLDEN_EDGES = 8


@pytest.fixture
async def sf(engine) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    yield factory
    await _cleanup(factory)


async def _cleanup(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as s:
        await s.execute(delete(Entity).where(Entity.urn.in_(SERVICE_URNS)))
        await s.execute(delete(Repo).where(Repo.name.in_(REPOS)))
        await s.execute(delete(Domain).where(Domain.urn.in_([*DOMAIN_URNS, OTHER_DOMAIN_URN])))
        await s.commit()


async def _plan() -> GraphPlan:
    cfg = ManifestConfig(providers=["backstage", "codeowners"])
    manifests = {}
    for repo in REPOS:
        files = LocalRepoFiles(ACME / repo, max_bytes=cfg.max_file_bytes)
        manifests[repo] = await load_manifest(
            files, repo_name=repo, cfg=cfg, conventions=ConventionsConfig()
        )
    return plan_service_graph(manifests)


async def _register(
    factory: async_sessionmaker[AsyncSession],
    *,
    domain_for: dict[str, uuid.UUID] | None = None,
    local: bool = False,
) -> None:
    async with factory() as s:
        for repo in REPOS:
            s.add(
                Repo(
                    id=uuid.uuid4(),
                    urn=f"urn:bifrost:repo:{repo}",
                    name=repo,
                    url=str(ACME / repo),
                    local_clone_path=str(ACME / repo) if local else None,
                    domain_id=(domain_for or {}).get(repo),
                )
            )
        await s.commit()


async def _counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as s:
        entities = (
            await s.execute(select(func.count()).where(Entity.urn.in_(SERVICE_URNS)))
        ).scalar_one()
        ids = select(Entity.id).where(Entity.urn.in_(SERVICE_URNS))
        edges = (
            await s.execute(select(func.count()).where(EntityRelationship.source_id.in_(ids)))
        ).scalar_one()
    return entities, edges


async def test_two_applies_are_idempotent(sf: async_sessionmaker[AsyncSession]) -> None:
    await _register(sf)
    plan = await _plan()
    first = await apply_graph_plan(sf, plan)
    after_first = await _counts(sf)
    second = await apply_graph_plan(sf, plan)
    assert await _counts(sf) == after_first == (5, GOLDEN_EDGES)
    assert first.edges_created == GOLDEN_EDGES
    assert second.edges_created == 0
    assert second.services == first.services == 5


async def test_concurrent_applies_do_not_collide(sf: async_sessionmaker[AsyncSession]) -> None:
    await _register(sf)
    plan = await _plan()
    results = await asyncio.gather(
        apply_graph_plan(sf, plan), apply_graph_plan(sf, plan), apply_graph_plan(sf, plan)
    )
    assert sum(r.edges_created for r in results) == GOLDEN_EDGES
    assert await _counts(sf) == (5, GOLDEN_EDGES)


async def test_domain_id_is_only_set_where_null(sf: async_sessionmaker[AsyncSession]) -> None:
    other = Domain(id=uuid.uuid4(), urn=OTHER_DOMAIN_URN, name="apply-test-other")
    async with sf() as s:
        s.add(other)
        await s.commit()
    await _register(sf, domain_for={"webhook-api": other.id})
    await apply_graph_plan(sf, await _plan())
    async with sf() as s:
        by_name = {r.name: r.domain_id for r in (await s.execute(select(Repo))).scalars()}
        dlvr = (
            await s.execute(select(Domain.id).where(Domain.urn == "urn:bifrost:domain:dlvr"))
        ).scalar_one()
    assert by_name["webhook-api"] == other.id
    assert by_name["dispatcher"] == dlvr


async def test_end_to_end_from_registered_local_repos(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    await _register(sf, local=True)
    settings = BifrostSettings(manifest=ManifestConfig(providers=["backstage", "codeowners"]))
    plan = await build_service_graph(sf, include_catalog=False, settings=settings)
    assert sorted(s.urn for s in plan.services) == SERVICE_URNS
    await apply_graph_plan(sf, plan)
    assert await _counts(sf) == (5, GOLDEN_EDGES)
    async with sf() as s:
        scoped = (
            await s.execute(
                select(func.count()).where(
                    Entity.urn.in_(SERVICE_URNS), Entity.scope_repo_id.is_not(None)
                )
            )
        ).scalar_one()
    assert scoped == 5
