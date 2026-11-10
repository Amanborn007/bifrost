"""Task dispatcher — routes tasks to appropriate RepoAgent instances.

Manages an agent registry (repo URN -> RepoAgent), lazily initializes agents
on first use, and queues tasks when all agents are busy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from bifrost.agents.repo import RepoAgent
from bifrost.agents.runtime import AgentTask, TaskResult
from bifrost.extraction.cli_client import ClaudeLlmClient
from bifrost.orchestration.task import OrchestrationQuery, QueryRunState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class AgentBusyError(Exception):
    """Raised when trying to dispatch but agent is already running."""


class Dispatcher:
    """Routes tasks to RepoAgent instances, creating them lazily.

    The dispatcher does NOT pre-create agents for all known repos.
    Agents are created on first dispatch and cached for reuse.
    """

    def __init__(
        self,
        client: ClaudeLlmClient,
        session: AsyncSession,
        *,
        model: str = "claude-sonnet-4-6",
        max_context_tokens: int = 5000,
    ) -> None:
        self._client = client
        self._session = session
        self._model = model
        self._max_context_tokens = max_context_tokens
        # repo URN -> RepoAgent (lazy init)
        self._agents: dict[str, RepoAgent] = {}
        # repo URN -> running task lock (prevents double-dispatch)
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_or_create_agent(self, repo_urn: str, repo_id: uuid.UUID) -> RepoAgent:
        """Lazy-initialize a RepoAgent for the given repo."""
        if repo_urn not in self._agents:
            self._agents[repo_urn] = RepoAgent(
                client=self._client,
                session=self._session,
                repo_id=repo_id,
                model=self._model,
                max_context_tokens=self._max_context_tokens,
            )
            logger.info("Created RepoAgent for %s", repo_urn)
        return self._agents[repo_urn]

    def _get_lock(self, repo_urn: str) -> asyncio.Lock:
        if repo_urn not in self._locks:
            self._locks[repo_urn] = asyncio.Lock()
        return self._locks[repo_urn]

    async def dispatch_one(
        self,
        repo_urn: str,
        repo_id: uuid.UUID,
        task: OrchestrationQuery,
    ) -> TaskResult:
        """Dispatch a task to a single repo's agent. Blocks if agent is locked."""
        agent = self._get_or_create_agent(repo_urn, repo_id)
        lock = self._get_lock(repo_urn)

        async with lock:
            agent_task = AgentTask(
                task_id=task.id,
                task_type=task.task_type.value,
                payload={"question": task.query},
            )
            return await agent.run(agent_task)

    async def dispatch_parallel(
        self,
        repo_map: dict[str, uuid.UUID],
        task: OrchestrationQuery,
        *,
        timeout: float | None = None,
    ) -> dict[str, TaskResult]:
        """Dispatch a task to multiple repos in parallel.

        Args:
            repo_map: {repo_urn: repo_id} for each repo to query
            task: The orchestrated task
            timeout: Optional per-agent timeout in seconds

        Returns:
            {repo_urn: TaskResult} for each repo, including failures
        """
        task.transition(QueryRunState.DISPATCHED)

        async def _run_one(urn: str, rid: uuid.UUID) -> tuple[str, TaskResult]:
            try:
                if timeout:
                    result = await asyncio.wait_for(
                        self.dispatch_one(urn, rid, task),
                        timeout=timeout,
                    )
                else:
                    result = await self.dispatch_one(urn, rid, task)
            except TimeoutError:
                result = TaskResult(
                    agent_id=f"repo-agent-{rid.hex[:8]}",
                    task_id=task.id,
                    success=False,
                    errors=[f"Agent timed out after {timeout}s"],
                )
            except Exception as exc:
                result = TaskResult(
                    agent_id=f"repo-agent-{rid.hex[:8]}",
                    task_id=task.id,
                    success=False,
                    errors=[str(exc)],
                )
            return urn, result

        task.transition(QueryRunState.RUNNING)
        coros = [_run_one(urn, rid) for urn, rid in repo_map.items()]
        pairs = await asyncio.gather(*coros)
        return dict(pairs)

    @property
    def active_agents(self) -> dict[str, RepoAgent]:
        """Return the current agent registry (for inspection/testing)."""
        return dict(self._agents)
