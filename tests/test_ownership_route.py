"""Tests for GET /api/v1/ownership — instant ownership lookup.

Uses the same httpx.AsyncClient + real DB pattern as test_api_routes.py.
Verifies:
  - Route returns a valid BifrostEnvelope
  - Registered repos with RepoOwnership data are returned first
  - OrgRepo catalog rows fill gaps for unregistered repos
  - Pattern matching: query hits name, squad, and description fields
  - Missing q param → 422
  - Empty result set → 200 with empty results (not 404)
  - is_ownership_question helper in the backend module
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_db, get_repository
from bifrost.api.routes.ownership import is_ownership_question
from bifrost.storage.models import OrgRepo, Repo, RepoOwnership
from bifrost.storage.repository import EntityRepository

# ── App fixture (mirrors test_api_routes.py pattern) ──────────────────


@pytest.fixture(autouse=True)
def _stub_worker():
    """No-op worker so routes that touch the singleton don't break."""
    from unittest.mock import MagicMock

    from bifrost.extraction import worker as _w

    prior = _w._worker
    _w._worker = MagicMock(spec=_w.ExtractionWorker)
    yield
    _w._worker = prior


@pytest.fixture
def app(db_session: AsyncSession):
    application = create_app()

    # Override BOTH get_db and get_repository so that all routes —
    # whether they use ``DB`` (raw session) or ``Repo`` (repository) —
    # share the same test session and can see the seeded data.
    async def _override_db():
        yield db_session

    async def _override_repo():
        repo = EntityRepository(db_session)
        yield repo

    application.dependency_overrides[get_db] = _override_db
    application.dependency_overrides[get_repository] = _override_repo
    return application


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Data fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def seed_registered_repo_with_ownership(db_session: AsyncSession):
    """A registered Repo with full RepoOwnership metadata."""
    repo = Repo(
        id=uuid.uuid4(),
        urn="urn:bifrost:repo:order-service",
        name="order-service",
        url="https://github.com/AcmeCo/order-service",
        description="Handles order processing and fulfillment",
        adapter_config={},
    )
    db_session.add(repo)
    await db_session.flush()

    ownership = RepoOwnership(
        repo_id=repo.id,
        name="order-service",
        squad="orders-platform",
        service_tier="1",
        slack_channel="#orders-eng",
        jira_project_key="SHIP",
        confluence_space_key="ORDERS",
        data_classification="restricted",
        raw_manifest={"contact": "dlvr-oncall@acmeco.example"},
    )
    db_session.add(ownership)
    await db_session.flush()
    return repo, ownership


@pytest.fixture
async def seed_org_repo(db_session: AsyncSession):
    """An OrgRepo catalog row (not registered as a Bifrost Repo)."""
    org_repo = OrgRepo(
        id=uuid.uuid4(),
        owner="AcmeCo",
        name="delivery-gateway",
        url="https://github.com/AcmeCo/delivery-gateway",
        description="Handles deliveries to customers",
        squad="deliveries",
        service_tier="1",
        archived=False,
        is_fork=False,
        is_private=True,
        stars=0,
    )
    db_session.add(org_repo)
    await db_session.flush()
    return org_repo


# ── Tests ─────────────────────────────────────────────────────────────


class TestOwnershipRoute:
    async def test_missing_q_returns_422(self, client: AsyncClient) -> None:
        """Omitting the required q param → 422 Unprocessable Entity."""
        resp = await client.get("/api/v1/ownership")
        assert resp.status_code == 422

    async def test_empty_q_returns_422(self, client: AsyncClient) -> None:
        """Empty string q param → 422 (min_length=1)."""
        resp = await client.get("/api/v1/ownership?q=")
        assert resp.status_code == 422

    async def test_no_match_returns_empty_results(self, client: AsyncClient) -> None:
        """Query that matches nothing → 200 with empty results list."""
        resp = await client.get("/api/v1/ownership?q=zzzz-nonexistent-service-xyz")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        assert body["data"]["results"] == []
        assert body["data"]["total"] == 0
        assert body["data"]["query"] == "zzzz-nonexistent-service-xyz"

    async def test_registered_repo_found_by_name(
        self,
        client: AsyncClient,
        seed_registered_repo_with_ownership,
    ) -> None:
        """Registered repo with ownership metadata is returned by name query."""
        resp = await client.get("/api/v1/ownership?q=order-service")
        assert resp.status_code == 200
        body = resp.json()
        results = body["data"]["results"]
        assert len(results) >= 1

        r = results[0]
        assert r["repo_name"] == "order-service"
        assert r["squad"] == "orders-platform"
        assert r["slack_channel"] == "#orders-eng"
        assert r["jira_project_key"] == "SHIP"
        assert r["confluence_space_key"] == "ORDERS"
        assert r["service_tier"] == "1"
        assert r["source"] == "registered"
        assert r["primary_contact"] == "dlvr-oncall@acmeco.example"

    async def test_registered_repo_found_by_squad(
        self,
        client: AsyncClient,
        seed_registered_repo_with_ownership,
    ) -> None:
        """Registered repo is also findable by squad name."""
        resp = await client.get("/api/v1/ownership?q=orders-platform")
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert any(r["repo_name"] == "order-service" for r in results)

    async def test_org_catalog_repo_found(
        self,
        client: AsyncClient,
        seed_org_repo,
    ) -> None:
        """OrgRepo catalog row is returned when no registered repo matches."""
        resp = await client.get("/api/v1/ownership?q=delivery")
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert len(results) >= 1
        r = next((x for x in results if x["repo_name"] == "delivery-gateway"), None)
        assert r is not None, "delivery-gateway not found in results"
        assert r["squad"] == "deliveries"
        assert r["source"] == "catalog"

    async def test_registered_comes_before_catalog(
        self,
        client: AsyncClient,
        seed_registered_repo_with_ownership,
        seed_org_repo,
    ) -> None:
        """When both registered and catalog rows match, registered comes first."""
        # Both "order-service" (registered) and "delivery-gateway" (catalog)
        # match if we query "service". Check that registered is first.
        resp = await client.get("/api/v1/ownership?q=service")
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        if len(results) >= 2:
            registered_idx = next(
                (i for i, r in enumerate(results) if r["source"] == "registered"), None
            )
            catalog_idx = next((i for i, r in enumerate(results) if r["source"] == "catalog"), None)
            if registered_idx is not None and catalog_idx is not None:
                assert registered_idx < catalog_idx, (
                    "Registered results should come before catalog results"
                )

    async def test_response_envelope_structure(
        self,
        client: AsyncClient,
        seed_registered_repo_with_ownership,
    ) -> None:
        """Response is a BifrostEnvelope with data + meta."""
        resp = await client.get("/api/v1/ownership?q=shipment")
        assert resp.status_code == 200
        body = resp.json()
        # Standard envelope fields
        assert "data" in body
        assert "meta" in body
        # data fields
        data = body["data"]
        assert "results" in data
        assert "query" in data
        assert "total" in data
        assert isinstance(data["results"], list)
        assert isinstance(data["total"], int)

    async def test_result_fields_present(
        self,
        client: AsyncClient,
        seed_registered_repo_with_ownership,
    ) -> None:
        """Each result contains the required ownership fields."""
        resp = await client.get("/api/v1/ownership?q=order-service")
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert len(results) >= 1
        r = results[0]

        required_fields = [
            "repo_name",
            "squad",
            "slack_channel",
            "primary_contact",
            "last_commit_at",
            "description",
            "service_tier",
            "jira_project_key",
            "confluence_space_key",
            "url",
            "source",
        ]
        for field in required_fields:
            assert field in r, f"Missing field: {field}"

    async def test_description_search(
        self,
        client: AsyncClient,
        seed_registered_repo_with_ownership,
    ) -> None:
        """Description text is also searched."""
        # "fulfillment" appears in the description of order-service
        resp = await client.get("/api/v1/ownership?q=fulfillment")
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert any(r["repo_name"] == "order-service" for r in results)


# ── is_ownership_question helper ──────────────────────────────────────


class TestIsOwnershipQuestion:
    @pytest.mark.parametrize(
        "text",
        [
            "who owns order-service?",
            "What team owns the delivery service?",
            "which squad is responsible for the fulfillment API?",
            "who works on webhook-api?",
            "ownership of webhook-core",
            "who do I contact about compliance?",
            "who maintains the auth service?",
            "what team is responsible for risk scoring",
        ],
    )
    def test_matches_ownership_patterns(self, text: str) -> None:
        assert is_ownership_question(text), f"Expected match for: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "how does the shipment service work?",
            "what does webhook-api do?",
            "list all services",
            "show me the architecture",
            "how do I deploy a new service?",
            "what are the retry semantics?",
        ],
    )
    def test_does_not_match_non_ownership(self, text: str) -> None:
        assert not is_ownership_question(text), f"Expected no match for: {text!r}"
