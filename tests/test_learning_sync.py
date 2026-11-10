"""Tests for cross-instance learning-weights export/import.

These tests run against a THROWAWAY database (default
``bifrost_test_sync``) so they never touch the shared test DB. They
build only the tables the sync path needs (entities,
entity_relationships, confidence_deltas) via raw DDL matching the live
schema, because confidence_deltas is created by the baseline
Alembic migration and is NOT in ``Base.metadata`` (so conftest's create_all wouldn't make
it).

Run standalone:
    TEST_DATABASE_URL=postgresql+asyncpg://bifrost:bifrost_dev@localhost:5433/bifrost_test_sync \
        uv run pytest tests/test_learning_sync.py -q
"""

from __future__ import annotations

import os
import uuid as _uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bifrost.actions import learning_sync as sync

SYNC_DB_URL = os.environ.get(
    "BIFROST_SYNC_TEST_URL",
    "postgresql+asyncpg://bifrost:bifrost_dev@localhost:5433/bifrost_test_sync",
)

_DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS entities (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        entity_type text NOT NULL DEFAULT 'component',
        urn text NOT NULL UNIQUE,
        name text NOT NULL DEFAULT '',
        confidence double precision NOT NULL DEFAULT 0.5,
        confidence_level text NOT NULL DEFAULT 'uncertain',
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_relationships (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id uuid NOT NULL REFERENCES entities(id),
        target_id uuid NOT NULL REFERENCES entities(id),
        rel_type text NOT NULL,
        confidence double precision NOT NULL DEFAULT 0.5,
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (source_id, target_id, rel_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confidence_deltas (
        id uuid PRIMARY KEY,
        target_kind text NOT NULL,
        target_id uuid NOT NULL,
        old_confidence numeric(3,2),
        new_confidence numeric(3,2) NOT NULL,
        old_level text,
        new_level text NOT NULL,
        old_tier integer,
        new_tier integer NOT NULL,
        reason text NOT NULL,
        task_id uuid,
        assumption_id uuid,
        plan_id uuid,
        conversation_id uuid,
        research_run_id uuid,
        applied_at timestamptz NOT NULL DEFAULT now(),
        applied_by text NOT NULL
    )
    """,
]


async def _ensure_sync_db() -> None:
    """Create the throwaway sync DB if it doesn't exist yet.

    Self-healing so the suite runs on a fresh machine / CI without a
    manual ``createdb`` — connects to the server's default ``postgres``
    DB (AUTOCOMMIT, since CREATE DATABASE can't run in a txn) and creates
    the target DB on the ``InvalidCatalogName`` path.
    """
    from sqlalchemy.engine import make_url

    url = make_url(SYNC_DB_URL)
    db_name = url.database
    admin = create_async_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        echo=False,
    )
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": db_name},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await admin.dispose()


@pytest_asyncio.fixture(scope="module")
async def engine():
    try:
        await _ensure_sync_db()
        eng = create_async_engine(SYNC_DB_URL, echo=False)
        async with eng.begin() as conn:
            for stmt in _DDL:
                await conn.execute(text(stmt))
    except Exception as e:  # pragma: no cover - infra guard
        pytest.skip(f"throwaway sync DB unavailable: {e}")
    yield eng
    async with eng.begin() as conn:
        await conn.execute(
            text("DROP TABLE IF EXISTS confidence_deltas, entity_relationships, entities CASCADE")
        )
    await eng.dispose()


@pytest_asyncio.fixture
async def sf(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_entity(s, urn: str, conf: float) -> str:
    eid = str(_uuid.uuid4())
    await s.execute(
        text(
            "INSERT INTO entities (id, urn, name, confidence, confidence_level)"
            " VALUES (:id, :u, :n, :c, 'inferred')"
        ),
        {"id": eid, "u": urn, "n": urn.split("/")[-1], "c": conf},
    )
    return eid


# ── Export round-trips the learned state ─────────────────────────────


@pytest.mark.asyncio
async def test_export_roundtrips_learned_weights(sf):
    src = "src-instance"
    urn = f"urn:bifrost:repo/sync-test-{_uuid.uuid4().hex[:8]}/component/Svc"
    async with sf() as s:
        eid = await _seed_entity(s, urn, 0.5)
        # Two distinct-conversation confirmations -> auto_upgrade to 0.80.
        for _ in range(2):
            await sync._learning.record_delta(
                s,
                target_kind="entity",
                target_id=eid,
                proposed_confidence=0.80,
                reason="audit_confirmed",
                conversation_id=str(_uuid.uuid4()),
                actor="user",
            )
        await s.commit()

    async with sf() as s:
        bundle = await sync.export_learning_weights(s, instance_id=src)

    assert bundle["schema_version"] == sync.BUNDLE_SCHEMA_VERSION
    assert bundle["source_instance_id"] == src
    assert bundle["weight_count"] == 1
    w = bundle["weights"][0]
    assert w["target_kind"] == "entity"
    assert w["urn"] == urn
    assert w["confidence"] == pytest.approx(0.80)
    assert w["confirmations"] >= 2
    assert "audit_confirmed" in w["reasons"]


# ── Import is non-destructive: a single peer never auto-upgrades ──────


@pytest.mark.asyncio
async def test_import_single_peer_stages_pending_not_applied(sf):
    urn = f"urn:bifrost:repo/sync-test-{_uuid.uuid4().hex[:8]}/component/Svc"
    async with sf() as s:
        await _seed_entity(s, urn, 0.50)
        await s.commit()

    bundle = {
        "schema_version": 1,
        "source_instance_id": "peer-A",
        "exported_at": "2026-06-02T00:00:00+00:00",
        "weight_count": 1,
        "weights": [
            {
                "target_kind": "entity",
                "urn": urn,
                "confidence": 0.90,
                "confirmations": 5,
                "reasons": ["audit_confirmed"],
            }
        ],
    }
    async with sf() as s:
        report = await sync.import_learning_weights(s, bundle)
        await s.commit()

    assert report.total == 1
    assert report.pending == 1  # staged, NOT applied
    assert report.applied_up == 0

    # Local confidence MUST be unchanged — no destructive overwrite.
    async with sf() as s:
        row = (
            await s.execute(text("SELECT confidence FROM entities WHERE urn = :u"), {"u": urn})
        ).first()
    assert float(row[0]) == pytest.approx(0.50)


# ── Import is idempotent: re-importing never advances the target ──────


@pytest.mark.asyncio
async def test_import_is_idempotent(sf):
    urn = f"urn:bifrost:repo/sync-test-{_uuid.uuid4().hex[:8]}/component/Svc"
    async with sf() as s:
        await _seed_entity(s, urn, 0.50)
        await s.commit()

    bundle = {
        "schema_version": 1,
        "source_instance_id": "peer-A",
        "exported_at": "2026-06-02T00:00:00+00:00",
        "weight_count": 1,
        "weights": [
            {
                "target_kind": "entity",
                "urn": urn,
                "confidence": 0.90,
                "confirmations": 5,
                "reasons": ["audit_confirmed"],
            }
        ],
    }
    # Import the same bundle 3x.
    for _ in range(3):
        async with sf() as s:
            await sync.import_learning_weights(s, bundle)
            await s.commit()

    async with sf() as s:
        conf = (
            await s.execute(text("SELECT confidence FROM entities WHERE urn = :u"), {"u": urn})
        ).first()[0]
        # No distinct local origin ever materialised -> still pending, target flat.
        pending = (
            await s.execute(
                text(
                    "SELECT count(*) FROM confidence_deltas "
                    "WHERE applied_by LIKE 'pending_upgrade%'"
                )
            )
        ).scalar()
    assert float(conf) == pytest.approx(0.50)  # never moved
    assert pending >= 1  # recorded as evidence


# ── Downgrade from a peer applies immediately + then no-ops ───────────


@pytest.mark.asyncio
async def test_import_downgrade_applies_then_noops(sf):
    urn = f"urn:bifrost:repo/sync-test-{_uuid.uuid4().hex[:8]}/component/Svc"
    async with sf() as s:
        await _seed_entity(s, urn, 0.90)
        await s.commit()

    bundle = {
        "schema_version": 1,
        "source_instance_id": "peer-B",
        "exported_at": "2026-06-02T00:00:00+00:00",
        "weight_count": 1,
        "weights": [
            {
                "target_kind": "entity",
                "urn": urn,
                "confidence": 0.40,
                "confirmations": 3,
                "reasons": ["audit_contradicted"],
            }
        ],
    }
    async with sf() as s:
        r1 = await sync.import_learning_weights(s, bundle)
        await s.commit()
    assert r1.applied_down == 1

    async with sf() as s:
        conf = (
            await s.execute(text("SELECT confidence FROM entities WHERE urn = :u"), {"u": urn})
        ).first()[0]
    assert float(conf) == pytest.approx(0.40)  # downgrade applied

    # Re-import the same lower value -> now within noise floor -> no-op.
    async with sf() as s:
        r2 = await sync.import_learning_weights(s, bundle)
        await s.commit()
    assert r2.noise == 1
    assert r2.applied_down == 0


# ── Unknown URN is skipped, never fabricated (no parallel store) ──────


@pytest.mark.asyncio
async def test_import_unknown_urn_skipped(sf):
    bundle = {
        "schema_version": 1,
        "source_instance_id": "peer-C",
        "exported_at": "2026-06-02T00:00:00+00:00",
        "weight_count": 1,
        "weights": [
            {
                "target_kind": "entity",
                "urn": "urn:bifrost:repo/never-seen/component/Ghost",
                "confidence": 0.95,
                "confirmations": 9,
                "reasons": ["audit_confirmed"],
            }
        ],
    }
    async with sf() as s:
        report = await sync.import_learning_weights(s, bundle)
        await s.commit()
    assert report.skipped_unresolved == 1
    assert report.applied_up == 0


# ── Wrong schema version is rejected ─────────────────────────────────


@pytest.mark.asyncio
async def test_import_rejects_bad_schema_version(sf):
    async with sf() as s:
        with pytest.raises(ValueError, match="schema_version"):
            await sync.import_learning_weights(s, {"schema_version": 999, "weights": []})
