"""Tests for all REST API routes and SSE streaming.

Uses httpx.AsyncClient with the real FastAPI app + test database.
Tests verify correct status codes, response envelope structure,
and SSE event format.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_repository
from bifrost.api.sse import SSEEvent, event_bus
from bifrost.storage.models import Domain, ExtractionTask, Repo
from bifrost.storage.repository import EntityRepository


@pytest.fixture(autouse=True)
def _stub_worker():
    """Install a no-op ExtractionWorker singleton for routes that
    submit to it (e.g., POST /repos/{id}/extract). The real worker
    requires the FastAPI lifespan to wire it up — which the test
    AsyncClient doesn't run. Stubbing the singleton keeps route-level
    tests focused on HTTP contract, not worker startup.
    """
    from unittest.mock import MagicMock

    from bifrost.extraction import worker as _w

    prior = _w._worker
    _w._worker = MagicMock(spec=_w.ExtractionWorker)
    yield
    _w._worker = prior


@pytest.fixture
def app(db_session: AsyncSession):
    """Create app with test DB session injected."""
    application = create_app()
    db_session.commit = db_session.flush  # routes now commit; keep the test rollback-only

    async def _override_repo():
        repo = EntityRepository(db_session)
        yield repo
        # No commit/rollback — conftest handles rollback

    application.dependency_overrides[get_repository] = _override_repo
    return application


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def seed_repo(db_session: AsyncSession) -> Repo:
    """Seed a test repo."""
    repo = Repo(
        id=uuid.uuid4(),
        urn="urn:bifrost:repo:test-repo",
        name="test-repo",
        url="https://github.com/test/repo",
        adapter_config={},
    )
    db_session.add(repo)
    await db_session.flush()
    return repo


@pytest.fixture
async def seed_domain(db_session: AsyncSession) -> Domain:
    """Seed a test domain."""
    domain = Domain(
        id=uuid.uuid4(),
        urn="urn:bifrost:domain:test-domain",
        name="test-domain",
        description="A test domain",
    )
    db_session.add(domain)
    await db_session.flush()
    return domain


@pytest.fixture
async def seed_task(db_session: AsyncSession) -> ExtractionTask:
    """Seed a test task."""
    task = ExtractionTask(
        id=uuid.uuid4(),
        task_type="question",
        query="What does service X do?",
        scope_json={"repo_ids": []},
        budget_json={},
        status="received",
    )
    db_session.add(task)
    await db_session.flush()
    return task


# ── Health ────────────────────────────────────────────────────────────


class TestHealth:
    async def test_simple_health(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_detailed_health(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/health/detailed")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["version"] == "0.1.0"
        assert "checks" in body


# ── Repos ─────────────────────────────────────────────────────────────


class TestRepos:
    async def test_register_repo(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/repos",
            json={"name": "new-repo", "url": "https://github.com/org/new-repo"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        assert body["data"]["status"] == "registered"
        assert body["data"]["urn"] == "urn:bifrost:repo:new-repo"

    async def test_list_repos(self, client: AsyncClient, seed_repo: Repo) -> None:
        resp = await client.get("/api/v1/repos")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 1

    async def test_get_repo(self, client: AsyncClient, seed_repo: Repo) -> None:
        resp = await client.get(f"/api/v1/repos/{seed_repo.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["name"] == "test-repo"
        assert "freshness" in body["data"]
        assert "stats" in body["data"]

    async def test_get_repo_not_found(self, client: AsyncClient) -> None:
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/repos/{fake_id}")
        assert resp.status_code == 404

    async def test_deregister_repo(self, client: AsyncClient, seed_repo: Repo) -> None:
        resp = await client.delete(f"/api/v1/repos/{seed_repo.id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "deregistered"

    async def test_trigger_extraction(self, client: AsyncClient, seed_repo: Repo) -> None:
        resp = await client.post(
            f"/api/v1/repos/{seed_repo.id}/extract",
            json={"force_full": False},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["data"]["status"] == "queued"
        assert "task_id" in body["data"]

    async def test_register_duplicate_urn(self, client: AsyncClient, seed_repo: Repo) -> None:
        resp = await client.post(
            "/api/v1/repos",
            json={"name": "test-repo", "url": "https://github.com/test/repo2"},
        )
        assert resp.status_code == 409


# ── Domains ───────────────────────────────────────────────────────────


class TestDomains:
    async def test_create_domain(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/domains",
            json={"name": "orders", "description": "Shipment services"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["urn"] == "urn:bifrost:domain:orders"

    async def test_list_domains(self, client: AsyncClient, seed_domain: Domain) -> None:
        resp = await client.get("/api/v1/domains")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) >= 1

    async def test_get_domain(self, client: AsyncClient, seed_domain: Domain) -> None:
        resp = await client.get(f"/api/v1/domains/{seed_domain.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["name"] == "test-domain"
        assert "repos" in body["data"]

    async def test_get_domain_not_found(self, client: AsyncClient) -> None:
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/domains/{fake_id}")
        assert resp.status_code == 404

    async def test_update_domain(self, client: AsyncClient, seed_domain: Domain) -> None:
        resp = await client.put(
            f"/api/v1/domains/{seed_domain.id}",
            json={"name": "updated-domain"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["updated"] is True

    async def test_delete_domain(self, client: AsyncClient, seed_domain: Domain) -> None:
        resp = await client.delete(f"/api/v1/domains/{seed_domain.id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "deleted"


# ── Entities ──────────────────────────────────────────────────────────


class TestEntities:
    async def test_list_entities_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/entities")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    async def test_search_entities(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/entities/search?q=auth")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)

    async def test_get_entity_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/entities/urn:bifrost:entity:nonexistent")
        assert resp.status_code == 404


# ── Blast radius (S6 dependency-impact REST API) ──────────────────────


class TestBlastRadius:
    async def _seed_graph(self, db_session: AsyncSession) -> dict[str, str]:
        """Seed seed→callee + caller→seed so downstream/upstream differ."""
        import uuid as _uuid

        from bifrost.storage.models import Entity, EntityRelationship

        ids = {k: _uuid.uuid4() for k in ("seed", "callee", "caller")}
        # Source-file + line metadata so the reason string can cite
        # file:line, proving the path is code-grounded (not a ramble).
        meta = {
            "seed": {"source_file": "svc/seed.py", "line": 10, "qualified_name": "svc.seed.Seed"},
            "callee": {
                "source_file": "svc/callee.py",
                "line": 42,
                "qualified_name": "svc.callee.Callee",
            },
            "caller": {
                "source_file": "svc/caller.py",
                "line": 7,
                "qualified_name": "svc.caller.Caller",
            },
        }
        for label, eid in ids.items():
            db_session.add(
                Entity(
                    id=eid,
                    entity_type="component",
                    urn=f"urn:test:blast:{label}",
                    name=f"blast-{label}",
                    qualified_name=f"blast.{label}",
                    knowledge_tier=1,
                    entity_metadata=meta[label],
                    confidence=0.9,
                    confidence_level="confirmed",
                    source_basis=[],
                    freshness_state="fresh",
                )
            )
        # seed --calls--> callee  (downstream of seed)
        # caller --calls--> seed  (upstream of seed)
        for src, dst in (("seed", "callee"), ("caller", "seed")):
            db_session.add(
                EntityRelationship(
                    id=_uuid.uuid4(),
                    source_id=ids[src],
                    target_id=ids[dst],
                    rel_type="calls",
                    rel_metadata={},
                    confidence=0.95,
                    source_basis=[{"source_type": "code_verified", "tier": 1}],
                    freshness_state="fresh",
                )
            )
        await db_session.flush()
        return {k: str(v) for k, v in ids.items()}

    async def test_blast_radius_downstream(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        await self._seed_graph(db_session)
        resp = await client.post(
            "/api/v1/dependencies/blast-radius",
            json={"urn": "urn:test:blast:seed", "direction": "downstream", "depth": 2},
        )
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["seed_urn"] == "urn:test:blast:seed"
        urns = {n["urn"] for n in data["nodes"]}
        # Downstream walk reaches the callee, not the caller.
        assert "urn:test:blast:callee" in urns
        assert "urn:test:blast:caller" not in urns
        assert data["impacted_count"] >= 1
        # Trust contract: weakest-path confidence surfaced in meta.
        assert "weakest path confidence" in body["meta"]["evidence_summary"]

        # ── Granular reasoning: each impact carries the WHY ──
        impacts = data["impacts"]
        assert len(impacts) >= 1
        callee_impact = next(im for im in impacts if im["urn"] == "urn:test:blast:callee")
        # Direct (1-hop) callee ranks first and is flagged as direct.
        assert callee_impact["distance_from_seed"] == 1
        # The path is the concrete edge chain seed → callee.
        assert len(callee_impact["path"]) == 1
        hop = callee_impact["path"][0]
        assert hop["rel_type"] == "calls"
        assert hop["from_urn"] == "urn:test:blast:seed"
        assert hop["to_urn"] == "urn:test:blast:callee"
        # The receiving end's code location is cited on the hop.
        assert hop["to_source_file"] == "svc/callee.py"
        assert hop["to_line"] == 42
        # Per-impact weakest-link confidence reflects the 0.95 edge.
        assert callee_impact["path_confidence"] == 0.95
        # Deterministic reason cites the relationship + file:line — no ramble.
        assert "calls" in callee_impact["reason"]
        assert "svc/callee.py:42" in callee_impact["reason"]
        # Node grounding is surfaced too.
        callee_node = next(n for n in data["nodes"] if n["urn"] == "urn:test:blast:callee")
        assert callee_node["source_file"] == "svc/callee.py"
        assert callee_node["line"] == 42
        # Headline distinguishes direct vs indirect impact.
        assert "direct" in body["meta"]["evidence_summary"]

    async def test_blast_radius_upstream(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        await self._seed_graph(db_session)
        resp = await client.post(
            "/api/v1/dependencies/blast-radius",
            json={"urn": "urn:test:blast:seed", "direction": "upstream", "depth": 2},
        )
        assert resp.status_code == 200
        urns = {n["urn"] for n in resp.json()["data"]["nodes"]}
        # Upstream walk reaches the caller, not the callee.
        assert "urn:test:blast:caller" in urns
        assert "urn:test:blast:callee" not in urns

    async def test_blast_radius_unknown_urn_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/dependencies/blast-radius",
            json={"urn": "urn:test:blast:does-not-exist"},
        )
        assert resp.status_code == 404


# ── Entity relationships (per-entity inbound/outbound edges) ──────────


class TestEntityRelationships:
    async def _seed(self, db_session: AsyncSession) -> dict[str, str]:
        """seed --calls--> callee (outbound) and caller --calls--> seed (inbound)."""
        import uuid as _uuid

        from bifrost.storage.models import Entity, EntityRelationship

        ids = {k: _uuid.uuid4() for k in ("seed", "callee", "caller")}
        for label, eid in ids.items():
            db_session.add(
                Entity(
                    id=eid,
                    entity_type="component",
                    urn=f"urn:test:rel:{label}",
                    name=f"rel-{label}",
                    qualified_name=f"rel.{label}",
                    knowledge_tier=1,
                    entity_metadata={},
                    confidence=0.9,
                    confidence_level="confirmed",
                    source_basis=[],
                    freshness_state="fresh",
                )
            )
        for src, dst in (("seed", "callee"), ("caller", "seed")):
            db_session.add(
                EntityRelationship(
                    id=_uuid.uuid4(),
                    source_id=ids[src],
                    target_id=ids[dst],
                    rel_type="calls",
                    rel_metadata={},
                    confidence=0.95,  # → "confirmed" via _rel_confidence_label
                    source_basis=[{"source_type": "code_verified", "tier": 1}],
                    freshness_state="fresh",
                )
            )
        await db_session.flush()
        return {k: str(v) for k, v in ids.items()}

    async def test_relationships_returns_inbound_and_outbound(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Regression: this endpoint 500'd because it read the nonexistent
        # EntityRelationship.confidence_level (rels only carry a float
        # ``confidence``) — so EVERY connected entity rendered "No
        # relationships" in the UI.
        ids = await self._seed(db_session)
        resp = await client.get(f"/api/v1/entities/by-id/{ids['seed']}/relationships")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        out_targets = {e["target_urn"] for e in data["outbound"]}
        in_sources = {e["source_urn"] for e in data["inbound"]}
        assert "urn:test:rel:callee" in out_targets
        assert "urn:test:rel:caller" in in_sources
        # Float confidence is mapped to a level label (0.95 → confirmed).
        assert data["outbound"][0]["confidence"] == "confirmed"
        assert data["inbound"][0]["confidence"] == "confirmed"

    async def test_relationships_empty_for_unconnected_entity(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        import uuid as _uuid

        from bifrost.storage.models import Entity

        eid = _uuid.uuid4()
        db_session.add(
            Entity(
                id=eid,
                entity_type="component",
                urn="urn:test:rel:lonely",
                name="lonely",
                qualified_name="rel.lonely",
                knowledge_tier=1,
                entity_metadata={},
                confidence=0.9,
                confidence_level="confirmed",
                source_basis=[],
                freshness_state="fresh",
            )
        )
        await db_session.flush()
        resp = await client.get(f"/api/v1/entities/by-id/{eid}/relationships")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["outbound"] == [] and data["inbound"] == []


# ── Tasks ─────────────────────────────────────────────────────────────


class TestTasks:
    async def test_submit_task(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/extraction-tasks",
            json={
                "type": "question",
                "query": "How does auth work?",
                "scope": {"repo_ids": []},
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["data"]["status"] == "received"
        assert "stream_url" in body["data"]

    async def test_list_tasks(self, client: AsyncClient, seed_task: ExtractionTask) -> None:
        resp = await client.get("/api/v1/extraction-tasks")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) >= 1

    async def test_get_task(self, client: AsyncClient, seed_task: ExtractionTask) -> None:
        resp = await client.get(f"/api/v1/extraction-tasks/{seed_task.id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["query"] == "What does service X do?"

    async def test_get_task_not_found(self, client: AsyncClient) -> None:
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/extraction-tasks/{fake_id}")
        assert resp.status_code == 404


# ── Query ─────────────────────────────────────────────────────────────


class TestQuery:
    async def test_submit_query(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "What is the shipment flow?"},
        )
        # /api/v1/query returns 200 (synchronous response); the
        # async-stream-style 202 path is /api/v1/extraction-tasks.
        assert resp.status_code == 200
        body = resp.json()
        assert "answer" in body["data"]
        assert "confidence" in body["data"]
        assert "sources" in body["data"]
        assert "task_id" in body["data"]
        # Trust contract (S10): confidence must also ride the uniform
        # `meta` location, with a canonical ConfidenceLevel value.
        from bifrost.core.enums import ConfidenceLevel

        meta = body["meta"]
        assert meta["confidence"] in {c.value for c in ConfidenceLevel}
        assert meta["task_id"] == body["data"]["task_id"]
        assert meta["data_source"] == "extraction"


# ── Feedback ──────────────────────────────────────────────────────────


class TestFeedback:
    async def test_submit_feedback(self, client: AsyncClient, seed_task: ExtractionTask) -> None:
        resp = await client.post(
            "/api/v1/feedback",
            json={
                "task_id": str(seed_task.id),
                "quality_signal": "helpful",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "feedback_id" in body["data"]

    async def test_feedback_task_not_found(self, client: AsyncClient) -> None:
        fake_id = str(uuid.uuid4())
        resp = await client.post(
            "/api/v1/feedback",
            json={
                "task_id": fake_id,
                "quality_signal": "unhelpful",
            },
        )
        assert resp.status_code == 404


# ── SSE ───────────────────────────────────────────────────────────────


class TestSSE:
    async def test_sse_event_bus_publish_subscribe(self) -> None:
        """Test the event bus directly — subscribe, publish, receive."""
        bus = event_bus
        task_id = "test-task-123"
        queue = bus.subscribe(task_id)

        event = SSEEvent(
            event_type="task.created",
            data={"task_id": task_id, "status": "received"},
        )
        await bus.publish(task_id, event)

        received = queue.get_nowait()
        assert received is not None
        assert received.event_type == "task.created"
        assert received.data["task_id"] == task_id

        bus.unsubscribe(task_id, queue)

    async def test_sse_stream_endpoint(
        self, client: AsyncClient, seed_task: ExtractionTask
    ) -> None:
        """Test the SSE streaming endpoint returns text/event-stream."""
        # The stream only serves ids that name an extraction task row.
        task_id = str(seed_task.id)

        # We can't easily test a long-lived SSE stream with AsyncClient,
        # but we can verify the endpoint exists and returns the right content type.
        # The stream will hang waiting for events, so we just check it starts.
        import asyncio

        async def publish_and_close():
            await asyncio.sleep(0.1)
            await event_bus.publish(
                task_id,
                SSEEvent(
                    event_type="task.created",
                    data={"task_id": task_id, "status": "received"},
                ),
            )
            await event_bus.close(task_id)

        # Subscribe first so the publish has a target
        event_bus.subscribe(task_id)

        # Start the publisher in the background
        asyncio.create_task(publish_and_close())

        resp = await client.get(
            f"/api/v1/tasks/{task_id}/stream",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


# ── Handover-doc search ───────────────────────────────────────────────


class TestHandoverDocSearch:
    async def test_missing_q_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/handover-docs/search")
        assert resp.status_code == 400

    async def test_empty_q_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/handover-docs/search?q=")
        assert resp.status_code == 400

    async def test_whitespace_q_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/handover-docs/search?q=%20%20")
        assert resp.status_code == 400

    async def test_no_matches_returns_empty(self, client: AsyncClient) -> None:
        # Empty corpus / no docs match — 200 + empty list, never 500.
        resp = await client.get(
            "/api/v1/handover-docs/search?q=nonexistent-term-xyz",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []

    async def test_matches_scope_value(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        from bifrost.storage.models import HandoverDoc

        doc = HandoverDoc(
            id=uuid.uuid4(),
            scope_kind="repo",
            scope_value="checkout-service",
            fs_path=None,
            freshness_state="fresh",
        )
        db_session.add(doc)
        await db_session.flush()

        resp = await client.get("/api/v1/handover-docs/search?q=checkout")
        assert resp.status_code == 200
        hits = resp.json()["data"]
        assert any(h["id"] == str(doc.id) for h in hits)
        match = next(h for h in hits if h["id"] == str(doc.id))
        # ``score`` is raw Postgres ``ts_rank``. Magnitudes are
        # corpus-dependent but a real match must be strictly positive.
        assert match["score"] > 0.0
        # No body → snippet is None.
        assert match["snippet"] is None

    async def test_session_scope_excluded(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        from bifrost.storage.models import HandoverDoc

        # Session-scoped doc that contains the term — must NOT show up.
        doc = HandoverDoc(
            id=uuid.uuid4(),
            scope_kind="session",
            scope_value="zzzunique-session-term",
            fs_path=None,
            freshness_state="fresh",
        )
        db_session.add(doc)
        await db_session.flush()

        resp = await client.get(
            "/api/v1/handover-docs/search?q=zzzunique-session-term",
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == []


# ── Response Envelope ─────────────────────────────────────────────────


class TestEnvelope:
    async def test_envelope_has_meta(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/repos")
        body = resp.json()
        assert "meta" in body
        assert "request_id" in body["meta"]
        assert "timestamp" in body["meta"]
        assert body["meta"]["track"] == "A"
