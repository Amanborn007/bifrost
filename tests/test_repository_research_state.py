"""EntityRepository.save_research_state / load_research_state — round-trip
against real PostgreSQL.

Uses the ``db_session`` fixture from conftest.py for per-test rollback.
The conversation_research_state table is created by Base.metadata.create_all
in the session-scoped engine fixture (since the model lives in models.py).
"""

from __future__ import annotations

import pytest

from bifrost.storage.repository import EntityRepository


@pytest.fixture
def repo(db_session) -> EntityRepository:
    return EntityRepository(db_session)


async def _make_conversation(repo: EntityRepository, title: str = "test"):
    return await repo.create_conversation(
        session_id="sess-1",
        title=title,
        scope_repo_ids=[],
    )


class TestResearchState:
    async def test_save_then_load_roundtrip(self, repo: EntityRepository) -> None:
        conv = await _make_conversation(repo, "rt-1")
        bundle = {
            "conversation_id": str(conv.id),
            "last_question": "how does orders retry?",
            "last_answer": "exponential backoff queue",
            "confidence": "confirmed",
            "repos_used": ["00000000-0000-0000-0000-000000000001"],
            "repos_used_names": ["orders"],
            "scope_rationale": "single repo, deep-indexed",
            "subgraph_entity_urns": ["bifrost://repo/orders/component/X"],
            "sources": [{"kind": "jira", "ref": "SHIP-1", "title": ""}],
            "last_question_embedding": None,
            "created_at": "2026-04-25T10:00:00+00:00",
        }
        await repo.save_research_state(
            conversation_id=conv.id,
            bundle=bundle,
            last_question_embedding=[0.1, 0.2, 0.3],
        )

        loaded = await repo.load_research_state(conv.id)

        assert loaded is not None
        assert loaded.bundle["last_question"] == "how does orders retry?"
        assert loaded.bundle["repos_used_names"] == ["orders"]
        assert loaded.last_question_embedding == [0.1, 0.2, 0.3]

    async def test_load_unknown_conversation_returns_none(
        self,
        repo: EntityRepository,
    ) -> None:
        import uuid as _uuid

        loaded = await repo.load_research_state(_uuid.uuid4())
        assert loaded is None

    async def test_save_overwrites_prior_bundle(
        self,
        repo: EntityRepository,
    ) -> None:
        conv = await _make_conversation(repo, "ow")
        await repo.save_research_state(
            conversation_id=conv.id,
            bundle={"v": 1, "last_question": "first"},
            last_question_embedding=None,
        )
        await repo.save_research_state(
            conversation_id=conv.id,
            bundle={"v": 2, "last_question": "second"},
            last_question_embedding=[1.0],
        )

        loaded = await repo.load_research_state(conv.id)
        assert loaded is not None
        assert loaded.bundle["v"] == 2
        assert loaded.bundle["last_question"] == "second"
        assert loaded.last_question_embedding == [1.0]

    async def test_delete_research_state_removes_row(
        self,
        repo: EntityRepository,
    ) -> None:
        conv = await _make_conversation(repo, "del")
        await repo.save_research_state(
            conversation_id=conv.id,
            bundle={"x": 1},
            last_question_embedding=None,
        )
        assert await repo.load_research_state(conv.id) is not None

        await repo.delete_research_state(conv.id)

        assert await repo.load_research_state(conv.id) is None

    async def test_cascade_on_conversation_delete(
        self,
        repo: EntityRepository,
        db_session,
    ) -> None:
        """Dropping a conversation should drop its research state via
        the FK CASCADE."""
        from sqlalchemy import delete as _delete

        from bifrost.storage.models import Conversation

        conv = await _make_conversation(repo, "cascade")
        await repo.save_research_state(
            conversation_id=conv.id,
            bundle={"x": "y"},
            last_question_embedding=None,
        )
        # Hard-delete the conversation row.
        await db_session.execute(
            _delete(Conversation).where(Conversation.id == conv.id),
        )
        await db_session.flush()

        assert await repo.load_research_state(conv.id) is None
