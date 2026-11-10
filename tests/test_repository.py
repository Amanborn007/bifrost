"""Integration tests for EntityRepository — full CRUD against real PostgreSQL.

Requires a running PostgreSQL with pgvector. Uses the db_session fixture
from conftest.py for per-test rollback isolation.
"""

from __future__ import annotations

import uuid

import pytest

from bifrost.storage.repository import EntityRepository


@pytest.fixture
def repo(db_session) -> EntityRepository:
    return EntityRepository(db_session)


class TestRepoCRUD:
    async def test_upsert_and_get_repo(self, repo: EntityRepository) -> None:
        rid = uuid.uuid4()
        created = await repo.upsert_repo(
            id=rid, urn="urn:bifrost:repo:test-1", name="test-repo", url="https://github.com/test"
        )
        assert created.id == rid
        assert created.name == "test-repo"

        fetched = await repo.get_repo(rid)
        assert fetched is not None
        assert fetched.urn == "urn:bifrost:repo:test-1"

    async def test_get_repo_by_urn(self, repo: EntityRepository) -> None:
        await repo.upsert_repo(
            id=uuid.uuid4(), urn="urn:bifrost:repo:by-urn", name="by-urn", url="https://x.com"
        )
        fetched = await repo.get_repo_by_urn("urn:bifrost:repo:by-urn")
        assert fetched is not None
        assert fetched.name == "by-urn"

    async def test_upsert_updates_existing_repo(self, repo: EntityRepository) -> None:
        rid = uuid.uuid4()
        await repo.upsert_repo(
            id=rid, urn="urn:bifrost:repo:update", name="old-name", url="https://old.com"
        )
        updated = await repo.upsert_repo(
            id=uuid.uuid4(), urn="urn:bifrost:repo:update", name="new-name", url="https://new.com"
        )
        assert updated.name == "new-name"
        assert updated.url == "https://new.com"

    async def test_list_repos(self, repo: EntityRepository) -> None:
        await repo.upsert_repo(
            id=uuid.uuid4(), urn="urn:bifrost:repo:a", name="alpha", url="https://a.com"
        )
        await repo.upsert_repo(
            id=uuid.uuid4(), urn="urn:bifrost:repo:b", name="beta", url="https://b.com"
        )
        repos = await repo.list_repos()
        assert len(repos) >= 2


class TestEntityCRUD:
    async def test_upsert_and_get_entity(self, repo: EntityRepository) -> None:
        eid = uuid.uuid4()
        created = await repo.upsert_entity(
            id=eid,
            entity_type="component",
            urn="urn:bifrost:component:test-1",
            name="TestComponent",
            knowledge_tier=1,
            confidence=0.8,
            confidence_level="inferred",
        )
        assert created.name == "TestComponent"

        fetched = await repo.get_entity(eid)
        assert fetched is not None
        assert fetched.entity_type == "component"

    async def test_get_entity_by_urn(self, repo: EntityRepository) -> None:
        await repo.upsert_entity(
            id=uuid.uuid4(),
            entity_type="interface",
            urn="urn:bifrost:interface:by-urn",
            name="ByUrn",
        )
        fetched = await repo.get_entity_by_urn("urn:bifrost:interface:by-urn")
        assert fetched is not None
        assert fetched.name == "ByUrn"

    async def test_list_entities_with_type_filter(self, repo: EntityRepository) -> None:
        for i in range(3):
            await repo.upsert_entity(
                id=uuid.uuid4(),
                entity_type="component",
                urn=f"urn:bifrost:component:list-{i}",
                name=f"Comp-{i}",
            )
        await repo.upsert_entity(
            id=uuid.uuid4(),
            entity_type="flow",
            urn="urn:bifrost:flow:list-0",
            name="Flow-0",
        )
        components = await repo.list_entities(entity_type="component")
        flows = await repo.list_entities(entity_type="flow")
        assert len(components) >= 3
        assert len(flows) >= 1

    async def test_batch_upsert_entities(self, repo: EntityRepository) -> None:
        entities = [
            {
                "id": uuid.uuid4(),
                "entity_type": "component",
                "urn": f"urn:bifrost:component:batch-{i}",
                "name": f"Batch-{i}",
                "knowledge_tier": 1,
                "confidence": 0.7,
                "confidence_level": "inferred",
            }
            for i in range(5)
        ]
        count = await repo.batch_upsert_entities(entities)
        assert count == 5

        # Verify all were inserted
        for e in entities:
            fetched = await repo.get_entity_by_urn(e["urn"])
            assert fetched is not None

    async def test_batch_upsert_empty_list(self, repo: EntityRepository) -> None:
        count = await repo.batch_upsert_entities([])
        assert count == 0


class TestRelationshipCRUD:
    async def test_upsert_and_get_relationship(self, repo: EntityRepository) -> None:
        src_id = uuid.uuid4()
        tgt_id = uuid.uuid4()
        await repo.upsert_entity(
            id=src_id, entity_type="component", urn="urn:bifrost:c:rel-src", name="Source"
        )
        await repo.upsert_entity(
            id=tgt_id, entity_type="component", urn="urn:bifrost:c:rel-tgt", name="Target"
        )

        rel = await repo.upsert_relationship(
            id=uuid.uuid4(),
            source_id=src_id,
            target_id=tgt_id,
            rel_type="depends_on",
            confidence=0.9,
        )
        assert rel.rel_type == "depends_on"

        outgoing = await repo.get_relationships(src_id, direction="outgoing")
        assert len(outgoing) == 1
        assert outgoing[0].target_id == tgt_id

        incoming = await repo.get_relationships(tgt_id, direction="incoming")
        assert len(incoming) == 1
        assert incoming[0].source_id == src_id

    async def test_get_relationships_both_directions(self, repo: EntityRepository) -> None:
        a_id = uuid.uuid4()
        b_id = uuid.uuid4()
        c_id = uuid.uuid4()
        for eid, name in [(a_id, "A"), (b_id, "B"), (c_id, "C")]:
            await repo.upsert_entity(
                id=eid, entity_type="component", urn=f"urn:bifrost:c:dir-{name}", name=name
            )

        await repo.upsert_relationship(
            id=uuid.uuid4(), source_id=a_id, target_id=b_id, rel_type="calls"
        )
        await repo.upsert_relationship(
            id=uuid.uuid4(), source_id=c_id, target_id=b_id, rel_type="imports"
        )

        both = await repo.get_relationships(b_id, direction="both")
        assert len(both) == 2


class TestSearchEntities:
    async def test_search_by_embedding(self, repo: EntityRepository) -> None:
        eid = uuid.uuid4()
        embedding = [0.1] * 1536
        await repo.upsert_entity(
            id=eid,
            entity_type="component",
            urn="urn:bifrost:c:embed-1",
            name="EmbedComp",
            embedding=embedding,
        )

        results = await repo.search_entities_by_embedding(embedding, limit=5)
        assert len(results) >= 1
        entity, distance = results[0]
        assert entity.name == "EmbedComp"
        assert distance < 0.01  # Same vector should be very close

    async def test_search_excludes_null_embeddings(self, repo: EntityRepository) -> None:
        await repo.upsert_entity(
            id=uuid.uuid4(),
            entity_type="component",
            urn="urn:bifrost:c:no-embed",
            name="NoEmbed",
            # No embedding set
        )

        results = await repo.search_entities_by_embedding([0.1] * 1536, limit=10)
        urns = [e.urn for e, _ in results]
        assert "urn:bifrost:c:no-embed" not in urns
