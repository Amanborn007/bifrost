"""Integration test: MockAdapter -> entity creation -> EntityRepository -> DB round-trip.

Verifies the full adapter-to-repository pipeline: read files from the MockAdapter,
create entities from their content, store via EntityRepository, and retrieve to
verify data integrity.
"""

from __future__ import annotations

import uuid

from bifrost.adapters.base import AdapterScope
from bifrost.adapters.mock import MockAdapter
from bifrost.storage.repository import EntityRepository

MOCK_FILES = {
    "src/services/shipment.py": (
        "class ShipmentService:\n    def book(self, weight: float) -> bool:\n        return True\n"
    ),
    "src/services/auth.py": (
        "class AuthService:\n    def login(self, username: str) -> str:\n        return 'token'\n"
    ),
    "src/models/user.py": ("class User:\n    name: str\n    email: str\n"),
}


class TestAdapterToRepositoryRoundTrip:
    async def test_extract_and_store_entities(self, db_session) -> None:
        adapter = MockAdapter(files=MOCK_FILES, instance="test-repo")
        repo = EntityRepository(db_session)

        # 1. Discover files via adapter
        scope = AdapterScope(source_scope={"repo": "test"})
        refs = await adapter.identify(scope)
        assert len(refs) == 3

        # 2. Extract content for each file
        entities_to_store: list[dict] = []
        for ref in refs:
            extracted = await adapter.extract(ref)
            assert extracted.content.text is not None

            entity_id = uuid.uuid4()
            entities_to_store.append(
                {
                    "id": entity_id,
                    "entity_type": "component",
                    "urn": f"urn:bifrost:component:{ref.object_id.replace('/', '-')}",
                    "name": ref.object_id.split("/")[-1].replace(".py", ""),
                    "knowledge_tier": 1,
                    "confidence": 0.8,
                    "confidence_level": "inferred",
                    "source_basis": [
                        {
                            "source_id": ref.urn,
                            "adapter_id": adapter.adapter_id,
                            "extracted_at": extracted.extracted_at.isoformat(),
                        }
                    ],
                }
            )

        # 3. Store via batch upsert
        count = await repo.batch_upsert_entities(entities_to_store)
        assert count == 3

        # 4. Retrieve and verify
        for stored in entities_to_store:
            entity = await repo.get_entity_by_urn(stored["urn"])
            assert entity is not None
            assert entity.name == stored["name"]
            assert entity.entity_type == "component"
            assert entity.confidence == 0.8
            assert entity.confidence_level == "inferred"

    async def test_extract_store_and_list(self, db_session) -> None:
        adapter = MockAdapter(
            files={"src/handler.py": "class Handler: pass"},
            instance="list-test",
        )
        repo = EntityRepository(db_session)

        scope = AdapterScope(source_scope={})
        refs = await adapter.identify(scope)
        await adapter.extract(refs[0])

        repo_id = uuid.uuid4()
        await repo.upsert_repo(
            id=repo_id,
            urn="urn:bifrost:repo:list-test",
            name="list-test",
            url="mock://list-test",
        )

        entity_id = uuid.uuid4()
        await repo.upsert_entity(
            id=entity_id,
            entity_type="component",
            urn="urn:bifrost:component:handler",
            name="handler",
            scope_repo_id=repo_id,
            knowledge_tier=1,
            confidence=0.9,
            confidence_level="confirmed",
        )

        entities = await repo.list_entities(repo_id=repo_id)
        assert len(entities) >= 1
        assert any(e.name == "handler" for e in entities)

    async def test_adapter_to_relationship(self, db_session) -> None:
        """Extract two components and create a dependency relationship between them."""
        adapter = MockAdapter(
            files={
                "src/a.py": "import b",
                "src/b.py": "def helper(): pass",
            }
        )
        repo = EntityRepository(db_session)

        scope = AdapterScope(source_scope={})
        refs = await adapter.identify(scope)
        assert len(refs) == 2

        a_id = uuid.uuid4()
        b_id = uuid.uuid4()

        await repo.upsert_entity(
            id=a_id,
            entity_type="component",
            urn="urn:bifrost:component:int-a",
            name="a",
        )
        await repo.upsert_entity(
            id=b_id,
            entity_type="component",
            urn="urn:bifrost:component:int-b",
            name="b",
        )

        rel = await repo.upsert_relationship(
            id=uuid.uuid4(),
            source_id=a_id,
            target_id=b_id,
            rel_type="imports",
            confidence=0.9,
        )
        assert rel.rel_type == "imports"

        outgoing = await repo.get_relationships(a_id, direction="outgoing")
        assert len(outgoing) == 1
        assert outgoing[0].target_id == b_id
