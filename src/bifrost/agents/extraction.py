"""ExtractionAgent — thin wrapper delegating to ExtractionPipeline.

Implements AgentRuntime ABC. Receives an extraction task, delegates to the
existing ExtractionPipeline, and reports progress via checkpoint.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bifrost.agents.runtime import (
    AgentRuntime,
    AgentState,
    AgentStatus,
    AgentTask,
    TaskResult,
)

if TYPE_CHECKING:
    from bifrost.extraction.pipeline import ExtractionPipeline, ReadFileFn

logger = logging.getLogger(__name__)


class ExtractionAgent(AgentRuntime):
    """Thin agent wrapper around ExtractionPipeline.

    No extraction logic is duplicated here — this is pure delegation.

    Usage::

        agent = ExtractionAgent(pipeline=pipeline, agent_id="ext-abc123")
        result = await agent.run(AgentTask(
            task_id="ext-1",
            task_type="extraction",
            payload={
                "file_paths": ["src/a.py", "src/b.py"],
                "read_file": some_async_reader,
            },
        ))
    """

    def __init__(
        self,
        pipeline: ExtractionPipeline,
        *,
        agent_id: str = "extraction-agent",
    ) -> None:
        self._pipeline = pipeline
        self._agent_id = agent_id
        self._state = AgentState(agent_id=agent_id)

    async def run(self, task: AgentTask) -> TaskResult:
        """Delegate extraction to the pipeline and return a summary."""
        self._state.status = AgentStatus.RUNNING

        file_paths: list[str] = task.payload.get("file_paths", [])
        read_file: ReadFileFn | None = task.payload.get("read_file")
        file_previews: dict[str, str] | None = task.payload.get("file_previews")

        if not file_paths or read_file is None:
            self._state.status = AgentStatus.FAILED
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=False,
                errors=["file_paths and read_file are required in payload"],
            )

        try:
            output = await self._pipeline.run(
                file_paths=file_paths,
                read_file=read_file,
                file_previews=file_previews,
            )

            self._state.status = AgentStatus.COMPLETED
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=True,
                answer=(
                    f"Extracted {len(output.components)} components, "
                    f"{len(output.flows)} flows, "
                    f"{len(output.dependencies)} dependencies "
                    f"from {output.files_analyzed} files"
                ),
                confidence_level="confirmed",
                data={
                    "components": len(output.components),
                    "flows": len(output.flows),
                    "dependencies": len(output.dependencies),
                    "relationships": len(output.relationships),
                    "knowledge_index_entries": len(output.knowledge_index),
                    "files_analyzed": output.files_analyzed,
                    "errors": output.errors,
                    "partial": output.partial,
                },
            )
        except Exception as exc:
            self._state.status = AgentStatus.FAILED
            logger.exception("ExtractionAgent failed for task %s", task.task_id)
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
        return "extraction-agent"
