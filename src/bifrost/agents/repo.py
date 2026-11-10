"""RepoAgent — per-repo specialist that answers questions using the knowledge index.

Implements AgentRuntime ABC. Receives a question, builds context from stored
entities, calls the Anthropic SDK, and returns a structured answer with
ConfidenceLevel and source entity URNs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bifrost.agents.context import ContextBuilder
from bifrost.agents.runtime import (
    AgentRuntime,
    AgentState,
    AgentStatus,
    AgentTask,
    TaskResult,
)
from bifrost.extraction.cli_client import ClaudeLlmClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


REPO_AGENT_SYSTEM_PROMPT = """\
You are a software engineering knowledge agent for a specific code repository.
You answer questions using ONLY the knowledge context provided below.

Rules:
1. Base your answer exclusively on the provided knowledge context.
2. If the context doesn't contain enough information, say so clearly.
3. Rate your confidence: "confirmed" (direct evidence),
   "inferred" (reasoning from evidence), "uncertain" (limited
   evidence), "stale" (evidence may be outdated).
4. Cite source URNs for every claim.

When you respond, use the answer_question tool with your findings.
"""

ANSWER_TOOL_SCHEMA: dict[str, Any] = {
    "name": "answer_question",
    "description": "Provide a structured answer to the user's question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "The answer text, grounded in the knowledge context.",
            },
            "confidence_level": {
                "type": "string",
                "enum": ["confirmed", "inferred", "uncertain", "stale"],
                "description": "How confident the answer is based on available evidence.",
            },
            "source_urns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URNs of entities cited as evidence for the answer.",
            },
            "reasoning": {
                "type": "string",
                "description": "Task explanation of how the answer was derived.",
            },
        },
        "required": ["answer", "confidence_level", "source_urns", "reasoning"],
    },
}


class RepoAgent(AgentRuntime):
    """Per-repo specialist answering questions from the knowledge index.

    Usage::

        agent = RepoAgent(
            client=anthropic_client,
            session=db_session,
            repo_id=uuid.UUID("..."),
        )
        result = await agent.run(AgentTask(
            task_id="q-1",
            task_type="question",
            payload={"question": "How does order flow work?"},
        ))
    """

    def __init__(
        self,
        client: ClaudeLlmClient,
        session: AsyncSession,
        repo_id: uuid.UUID,
        *,
        model: str = "claude-sonnet-4-6",
        max_context_tokens: int = 5000,
    ) -> None:
        self._client = client
        self._session = session
        self._repo_id = repo_id
        self._model = model
        self._context_builder = ContextBuilder(max_tokens=max_context_tokens)
        self._agent_id = f"repo-agent-{repo_id.hex[:8]}"
        self._state = AgentState(agent_id=self._agent_id)

    async def run(self, task: AgentTask) -> TaskResult:
        """Answer a question about this repo using stored knowledge."""
        self._state.status = AgentStatus.RUNNING
        question = task.payload.get("question", "")
        if not question:
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=False,
                errors=["No question provided in task payload"],
            )

        try:
            # 1. Fetch entities for this repo
            entities = await self._fetch_entities()
            relationships = await self._fetch_relationships(entities)

            # 2. Build bounded context
            context = self._context_builder.build_context(
                query=question,
                entities=entities,
                relationships=relationships,
            )

            # 3. Call LLM with context + question
            answer_data = await self._ask_llm(question, context)

            self._state.status = AgentStatus.COMPLETED
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=True,
                answer=answer_data.get("answer"),
                confidence_level=answer_data.get("confidence_level", "uncertain"),
                source_urns=answer_data.get("source_urns", []),
                reasoning=answer_data.get("reasoning"),
                data={"entities_in_context": len(entities)},
            )
        except Exception as exc:
            self._state.status = AgentStatus.FAILED
            logger.exception("RepoAgent run failed for task %s", task.task_id)
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=False,
                errors=[str(exc)],
            )

    async def checkpoint(self) -> AgentState:
        self._state.last_checkpoint_at = datetime.now(UTC)
        return self._state

    async def get_state(self) -> AgentState:
        return self._state

    def get_definition_id(self) -> str:
        return "repo-agent"

    # ── Private helpers ───────────────────────────────────────────────

    async def _fetch_entities(self) -> list[dict[str, Any]]:
        """Fetch all entities for this repo from the DB."""
        from sqlalchemy import select

        from bifrost.storage.models import Entity

        stmt = (
            select(Entity)
            .where(Entity.scope_repo_id == self._repo_id)
            .order_by(Entity.name)
            .limit(200)
        )
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [
            {
                "entity_type": r.entity_type,
                "urn": r.urn,
                "name": r.name,
                "qualified_name": r.qualified_name,
                "confidence_level": r.confidence_level,
                "entity_metadata": r.entity_metadata,
            }
            for r in rows
        ]

    async def _fetch_relationships(
        self,
        entities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fetch relationships between the given entities."""
        from sqlalchemy import select

        from bifrost.storage.models import Entity, EntityRelationship

        if not entities:
            return []

        urns = [e["urn"] for e in entities]
        # Get entity IDs from URNs
        stmt = select(Entity.id, Entity.urn).where(Entity.urn.in_(urns))
        result = await self._session.execute(stmt)
        id_to_urn = {row[0]: row[1] for row in result.all()}
        entity_ids = list(id_to_urn.keys())

        if not entity_ids:
            return []

        stmt = (
            select(EntityRelationship)
            .where(
                EntityRelationship.source_id.in_(entity_ids)
                | EntityRelationship.target_id.in_(entity_ids)
            )
            .limit(100)
        )
        result = await self._session.execute(stmt)
        rels = result.scalars().all()
        return [
            {
                "source_urn": id_to_urn.get(r.source_id, str(r.source_id)),
                "target_urn": id_to_urn.get(r.target_id, str(r.target_id)),
                "rel_type": r.rel_type,
            }
            for r in rels
        ]

    async def _ask_llm(self, question: str, context: str) -> dict[str, Any]:
        """Call the LLM with context and question, expecting JSON structured output."""
        completion = await self._client.call_with_json_schema(
            system_prompt=REPO_AGENT_SYSTEM_PROMPT,
            user_message=f"Knowledge Context:\n{context}\n\nQuestion: {question}",
            model=self._model,
            json_schema=ANSWER_TOOL_SCHEMA["input_schema"],
        )
        return completion.parsed
