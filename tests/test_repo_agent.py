"""Tests for agent runtime, context builder, RepoAgent, ExtractionAgent."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bifrost.agents.context import CHARS_PER_TOKEN, ContextBuilder
from bifrost.agents.extraction import ExtractionAgent
from bifrost.agents.repo import RepoAgent
from bifrost.agents.runtime import (
    AgentRuntime,
    AgentState,
    AgentStatus,
    AgentTask,
    TaskResult,
)

# ═══════════════════════════════════════════════════════════════════════
# AgentRuntime ABC
# ═══════════════════════════════════════════════════════════════════════


class TestAgentRuntimeABC:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            AgentRuntime()  # type: ignore[abstract]

    def test_abc_has_required_methods(self) -> None:
        assert hasattr(AgentRuntime, "run")
        assert hasattr(AgentRuntime, "checkpoint")
        assert hasattr(AgentRuntime, "get_state")


class TestAgentState:
    def test_default_state(self) -> None:
        state = AgentState(agent_id="test-1")
        assert state.status == AgentStatus.IDLE
        assert state.last_checkpoint_at is None
        assert state.metadata == {}

    def test_task_result_fields(self) -> None:
        result = TaskResult(
            agent_id="a-1",
            task_id="t-1",
            success=True,
            answer="42",
            confidence_level="confirmed",
            source_urns=["urn:bifrost:entity:comp:x"],
            reasoning="Direct evidence",
        )
        assert result.success
        assert result.answer == "42"
        assert result.confidence_level == "confirmed"
        assert len(result.source_urns) == 1


# ═══════════════════════════════════════════════════════════════════════
# ContextBuilder
# ═══════════════════════════════════════════════════════════════════════


def _make_entity(
    name: str,
    entity_type: str = "component",
    urn: str | None = None,
    summary: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "entity_type": entity_type,
        "urn": urn or f"urn:bifrost:entity:{entity_type}:{name}",
        "qualified_name": f"src.{name}",
        "confidence_level": "confirmed",
        "entity_metadata": {"purpose": summary} if summary else {},
    }


class TestContextBuilder:
    def test_builds_context_string(self) -> None:
        builder = ContextBuilder(max_tokens=5000)
        entities = [_make_entity("ShipmentService", summary="Handles orders")]
        context = builder.build_context("How does shipment work?", entities, [])
        assert "ShipmentService" in context
        assert "Handles orders" in context

    def test_respects_5k_token_budget(self) -> None:
        builder = ContextBuilder(max_tokens=5000)
        # Create many large entities
        entities = [_make_entity(f"Entity{i}", summary="x" * 500) for i in range(50)]
        context = builder.build_context("test query", entities, [])
        max_chars = 5000 * CHARS_PER_TOKEN
        assert len(context) <= max_chars, f"Context {len(context)} chars > budget {max_chars} chars"

    def test_capability_based_routing_flow_query(self) -> None:
        builder = ContextBuilder(max_tokens=5000)
        entities = [
            _make_entity("ShipmentFlow", entity_type="flow", summary="Shipment flow"),
            _make_entity("AuthComponent", entity_type="component", summary="Auth"),
            _make_entity("DBDep", entity_type="dependency", summary="DB"),
        ]
        context = builder.build_context("How does the flow work?", entities, [])
        # Flow query should prioritize flow entities
        assert "ShipmentFlow" in context

    def test_relevance_scoring_prioritizes_matches(self) -> None:
        builder = ContextBuilder(max_tokens=5000)
        entities = [
            _make_entity("Unrelated", summary="Handles logging"),
            _make_entity("ShipmentService", summary="Handles shipment processing"),
        ]
        context = builder.build_context("shipment processing", entities, [])
        # ShipmentService should appear (it matches query terms)
        assert "ShipmentService" in context

    def test_includes_relationships_if_budget_allows(self) -> None:
        builder = ContextBuilder(max_tokens=5000)
        entities = [_make_entity("A")]
        rels = [{"source_urn": "urn:a", "target_urn": "urn:b", "rel_type": "depends_on"}]
        context = builder.build_context("test", entities, rels)
        assert "depends_on" in context

    def test_empty_entities_returns_header_only(self) -> None:
        builder = ContextBuilder(max_tokens=5000)
        context = builder.build_context("test", [], [])
        assert "Knowledge Context" in context


# ═══════════════════════════════════════════════════════════════════════
# RepoAgent
# ═══════════════════════════════════════════════════════════════════════


def _mock_tool_response(
    answer: str = "Test answer",
    confidence: str = "confirmed",
    urns: list[str] | None = None,
    reasoning: str = "test reasoning",
) -> MagicMock:
    """Create a mock Anthropic API response with tool_use block."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "answer_question"
    tool_block.input = {
        "answer": answer,
        "confidence_level": confidence,
        "source_urns": urns or ["urn:bifrost:entity:comp:test"],
        "reasoning": reasoning,
    }
    response = MagicMock()
    response.content = [tool_block]
    return response


class TestRepoAgent:
    @pytest.mark.asyncio
    async def test_answer_includes_confidence_and_urns(self, db_session) -> None:
        """RepoAgent answer includes confidence level and source URNs."""
        repo_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_tool_response(
                answer="ShipmentService handles all shipment logic",
                confidence="confirmed",
                urns=["urn:bifrost:entity:component:ShipmentService"],
                reasoning="Found in component metadata",
            )
        )

        agent = RepoAgent(
            client=mock_client,
            session=db_session,
            repo_id=repo_id,
        )

        task = AgentTask(
            task_id="q-1",
            task_type="question",
            payload={"question": "What does ShipmentService do?"},
        )
        result = await agent.run(task)

        assert result.success
        assert result.answer is not None
        assert "ShipmentService" in result.answer
        assert result.confidence_level == "confirmed"
        assert len(result.source_urns) > 0
        assert result.reasoning is not None

    @pytest.mark.asyncio
    async def test_no_question_returns_error(self, db_session) -> None:
        repo_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mock_client = AsyncMock()
        agent = RepoAgent(client=mock_client, session=db_session, repo_id=repo_id)

        task = AgentTask(task_id="q-2", task_type="question", payload={})
        result = await agent.run(task)

        assert not result.success
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_checkpoint_updates_state(self, db_session) -> None:
        repo_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mock_client = AsyncMock()
        agent = RepoAgent(client=mock_client, session=db_session, repo_id=repo_id)

        state = await agent.checkpoint()
        assert state.last_checkpoint_at is not None
        assert state.agent_id.startswith("repo-agent-")


# ═══════════════════════════════════════════════════════════════════════
# ExtractionAgent
# ═══════════════════════════════════════════════════════════════════════


class TestExtractionAgent:
    @pytest.mark.asyncio
    async def test_delegates_to_pipeline(self) -> None:
        """ExtractionAgent is a thin wrapper — delegates to pipeline.run()."""
        mock_output = MagicMock()
        mock_output.components = [MagicMock(), MagicMock()]
        mock_output.flows = [MagicMock()]
        mock_output.dependencies = []
        mock_output.relationships = []
        mock_output.knowledge_index = []
        mock_output.files_analyzed = 5
        mock_output.errors = []
        mock_output.partial = False

        mock_pipeline = AsyncMock()
        mock_pipeline.run = AsyncMock(return_value=mock_output)

        agent = ExtractionAgent(pipeline=mock_pipeline)
        read_fn = AsyncMock(return_value="file content")

        task = AgentTask(
            task_id="ext-1",
            task_type="extraction",
            payload={
                "file_paths": ["a.py", "b.py"],
                "read_file": read_fn,
            },
        )
        result = await agent.run(task)

        assert result.success
        mock_pipeline.run.assert_called_once()
        assert result.data["components"] == 2
        assert result.data["flows"] == 1
        assert result.data["files_analyzed"] == 5

    @pytest.mark.asyncio
    async def test_missing_payload_returns_error(self) -> None:
        mock_pipeline = AsyncMock()
        agent = ExtractionAgent(pipeline=mock_pipeline)

        task = AgentTask(task_id="ext-2", task_type="extraction", payload={})
        result = await agent.run(task)

        assert not result.success
        assert len(result.errors) > 0
        mock_pipeline.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_error_returns_failure(self) -> None:
        mock_pipeline = AsyncMock()
        mock_pipeline.run = AsyncMock(side_effect=RuntimeError("LLM down"))

        agent = ExtractionAgent(pipeline=mock_pipeline)
        read_fn = AsyncMock(return_value="content")

        task = AgentTask(
            task_id="ext-3",
            task_type="extraction",
            payload={"file_paths": ["a.py"], "read_file": read_fn},
        )
        result = await agent.run(task)

        assert not result.success
        assert "LLM down" in result.errors[0]

    @pytest.mark.asyncio
    async def test_checkpoint(self) -> None:
        mock_pipeline = AsyncMock()
        agent = ExtractionAgent(pipeline=mock_pipeline, agent_id="ext-test")

        state = await agent.checkpoint()
        assert state.agent_id == "ext-test"
        assert state.last_checkpoint_at is not None
