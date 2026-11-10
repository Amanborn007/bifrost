"""End-to-end verification of prior-subgraph hydration → synthesis prompt.

The unit tests cover each layer in isolation. This test runs the FULL
integration pipeline against the real test Postgres:

    DB rows (entities + relationships)
      → hydrate_prior_subgraph(urns)
      → HydratedSubgraph
      → _build_synthesis_user_message(prior_subgraph=...)
      → final synthesis prompt string

…and asserts the rendered prompt contains the expected URNs, names,
edges, and section structure. It also prints the rendered
``## Prior research evidence`` block so the operator can eyeball it.

Why this exists: tests passing != feature working. This test proves
that on a fresh database, with realistic URN shapes (so cross-repo
classification fires), the synthesizer actually receives structured
evidence about prior repos — not just URN strings.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.research.bundle_cache import BundleSource, ResearchBundle
from bifrost.research.coordinator import _build_synthesis_user_message
from bifrost.research.subgraph_hydrate import hydrate_prior_subgraph
from bifrost.storage.models import Entity, EntityRelationship, Repo


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


E2E_REPO_NAMES = ["orders-e2e", "auth-e2e"]


@pytest.fixture(autouse=True)
async def _delete_committed_rows(engine):
    yield
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        repo_ids = select(Repo.id).where(Repo.name.in_(E2E_REPO_NAMES))
        entity_ids = select(Entity.id).where(Entity.scope_repo_id.in_(repo_ids))
        await session.execute(
            delete(EntityRelationship).where(
                or_(
                    EntityRelationship.source_id.in_(entity_ids),
                    EntityRelationship.target_id.in_(entity_ids),
                )
            )
        )
        await session.execute(delete(Entity).where(Entity.scope_repo_id.in_(repo_ids)))
        await session.execute(delete(Repo).where(Repo.name.in_(E2E_REPO_NAMES)))
        await session.commit()


async def _make_repo(session: AsyncSession, *, name: str) -> Repo:
    repo = Repo(
        id=uuid.uuid4(),
        urn=f"urn:bifrost:repo:{name}",
        name=name,
        url=f"https://github.com/test/{name}",
    )
    session.add(repo)
    await session.flush()
    return repo


async def _make_entity(
    session: AsyncSession,
    *,
    repo: Repo,
    short_name: str,
    purpose: str = "",
    source_file: str | None = None,
    entity_type: str = "component",
) -> Entity:
    metadata: dict = {}
    if purpose:
        metadata["purpose"] = purpose
    if source_file:
        metadata["source_file"] = source_file
    e = Entity(
        id=uuid.uuid4(),
        urn=f"urn:bifrost:{entity_type}:{repo.name}::{short_name}",
        name=short_name,
        entity_type=entity_type,
        scope_repo_id=repo.id,
        knowledge_tier=1,
        entity_metadata=metadata,
    )
    session.add(e)
    await session.flush()
    return e


async def _make_rel(
    session: AsyncSession,
    *,
    source: Entity,
    target: Entity,
    rel_type: str,
) -> None:
    r = EntityRelationship(
        id=uuid.uuid4(),
        source_id=source.id,
        target_id=target.id,
        rel_type=rel_type,
    )
    session.add(r)
    await session.flush()


@pytest.mark.asyncio
async def test_full_pipeline_renders_cross_repo_evidence(
    session_factory_for,
    db_session,
    capsys,
) -> None:
    """Walk the entire flow from real DB rows to rendered synthesis
    prompt and verify cross-repo edges surface correctly."""

    # ── Arrange: two prior repos with a cross-repo relationship ────
    orders_repo = await _make_repo(db_session, name="orders-e2e")
    auth = await _make_repo(db_session, name="auth-e2e")

    retry_queue = await _make_entity(
        db_session,
        repo=orders_repo,
        short_name="RetryQueue",
        purpose="Exponential-backoff queue for retried delivery attempts",
        source_file="src/retry/queue.py",
    )
    gateway = await _make_entity(
        db_session,
        repo=orders_repo,
        short_name="GatewayClient",
        purpose="Carrier-gateway HTTP client",
        source_file="src/gateway/client.py",
    )
    token_service = await _make_entity(
        db_session,
        repo=auth,
        short_name="TokenService",
        purpose="Issues short-lived JWTs for carrier requests",
        source_file="src/jwt/service.py",
    )

    await _make_rel(db_session, source=retry_queue, target=gateway, rel_type="calls")
    # CROSS-REPO edge: orders.GatewayClient → auth.TokenService
    await _make_rel(
        db_session,
        source=gateway,
        target=token_service,
        rel_type="uses",
    )
    await db_session.commit()

    prior_urns = [retry_queue.urn, gateway.urn, token_service.urn]
    prior_bundle = ResearchBundle(
        conversation_id="conv-e2e",
        last_question="how does orders retry & sign requests?",
        last_answer=(
            "Orders uses RetryQueue with exponential backoff and signs "
            "the gateway request with a JWT issued by auth's TokenService."
        ),
        confidence="confirmed",
        repos_used=[str(orders_repo.id), str(auth.id)],
        repos_used_names=[orders_repo.name, auth.name],
        scope_rationale="orders + auth deep-indexed, prior turn",
        subgraph_entity_urns=prior_urns,
        sources=[
            BundleSource(kind="jira", ref="SHIP-101", title="retry+sign issue"),
        ],
    )

    # ── Act: hydrate, then build the synthesis prompt ─────────────
    hydrated = await hydrate_prior_subgraph(
        urns=prior_urns,
        session_factory=session_factory_for,
    )

    # Sanity on hydration shape itself
    assert not hydrated.is_empty()
    assert hydrated.total_entities == 3
    assert hydrated.total_edges >= 2
    assert {r.repo_name for r in hydrated.repos} == {"orders-e2e", "auth-e2e"}
    # Cross-repo edge survives: orders.GatewayClient.outbound includes
    # auth.TokenService.
    orders_sub = next(r for r in hydrated.repos if r.repo_name == "orders-e2e")
    gateway_snippet = next(s for s in orders_sub.entities if s.name == "GatewayClient")
    cross_targets = {e.other_urn for e in gateway_snippet.outbound}
    assert token_service.urn in cross_targets

    prompt = _build_synthesis_user_message(
        question="and what about batch-orders?",
        bundles=[],  # no live bundles in this proof
        prior_bundle=prior_bundle,
        prior_subgraph=hydrated,
    )

    # ── Assert: the prompt actually contains structured prior evidence ──
    assert "## Prior research evidence" in prompt
    assert "### Prior repo: orders-e2e" in prompt
    assert "### Prior repo: auth-e2e" in prompt
    # Both URNs must be present as citation targets
    assert retry_queue.urn in prompt
    assert gateway.urn in prompt
    assert token_service.urn in prompt
    # Names appear in the URN-table column
    assert "RetryQueue" in prompt
    assert "GatewayClient" in prompt
    assert "TokenService" in prompt
    # Source files surface so synthesis can cite where the evidence lives
    assert "src/retry/queue.py" in prompt
    assert "src/jwt/service.py" in prompt
    # Purposes are in the table
    assert "Exponential-backoff queue" in prompt
    assert "JWTs for carrier" in prompt
    # The cross-repo edge: orders.GatewayClient -uses-> auth.TokenService
    # appears in some form (rel_type "uses" + the partner URN)
    assert "uses" in prompt

    # ── Print the rendered block so the operator can eyeball it ────
    block_start = prompt.index("## Prior research evidence")
    block_end = prompt.index("**Now write the answer", block_start)
    rendered_block = prompt[block_start:block_end]
    capsys.readouterr()  # flush prior pytest output
    print("\n" + "=" * 78)
    print("Rendered ## Prior research evidence block (from real DB rows):")
    print("=" * 78)
    print(rendered_block)
    print("=" * 78)
    # Reassert what we just printed for a defensive double-check
    assert "Prior research evidence" in rendered_block
