"""Composition root for agent infrastructure (Track-A↔B seam).

A single factory that builds the agent infrastructure for the active
track. Before this, agents were hand-wired ad-hoc at each call site
(``api/routes/ask.py`` constructed ``ClaudeLlmClient`` + ``TaskCoordinator``
inline; ``orchestration/dispatcher.py`` builds ``RepoAgent``s itself).
That duplication is what made ``BIFROST_TRACK`` decorative — there was no
one place to swap Track A for Track B.

``create_agent_infrastructure()`` is that place. Callers ask the returned
``AgentInfrastructure`` for a coordinator / repo agent rather than wiring
the LLM client, model names, and pool by hand. Today only **Track A**
(Messages-via-``claude -p`` subprocess + Postgres) is implemented;
**Track B** (Managed Agents + Memory Stores) is gated behind Research
Preview access and raises until it is genuinely available.

Design notes:
  * The infrastructure is **request-agnostic**: it holds the stateless
    LLM client + model names + track. Per-request inputs (the DB session,
    the entity repo) are passed to the ``make_*`` methods. This lets the
    app build one infrastructure at startup and reuse it across requests.
  * Track B raises ``TrackBNotAvailableError`` rather than silently
    falling back to Track A, so a misconfigured ``BIFROST_TRACK=B`` fails
    loudly at the seam instead of pretending to be Track B.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.agents.coordinator import TaskCoordinator
from bifrost.agents.repo import RepoAgent
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.errors import ConfigError
from bifrost.extraction.cli_client import ClaudeLlmClient
from bifrost.orchestration.budget import SessionPool
from bifrost.storage.backend import AgentStateBackend
from bifrost.storage.repository import EntityRepository
from bifrost.storage.state_backend import PostgresStateBackend


class TrackBNotAvailableError(ConfigError, RuntimeError):
    """Raised when ``BIFROST_TRACK=B`` but Managed Agents Research Preview
    access is not available (the Track B runtime/backend are not built).

    A :class:`~bifrost.errors.ConfigError` (boot-time misconfiguration) and a
    ``RuntimeError`` for back-compat with existing ``except`` clauses; maps to
    HTTP 500 if it ever surfaces through a route."""

    code = "config.track_b_unavailable"
    http_status = 501


@dataclass
class AgentInfrastructure:
    """Track-aware bundle of agent dependencies + factory methods.

    Built by :func:`create_agent_infrastructure`. Holds the shared LLM
    client, model names, and the active track; exposes ``make_*`` methods
    so call sites never hand-wire agents.
    """

    track: str
    llm_client: ClaudeLlmClient
    reasoning_model: str
    agent_model: str

    def make_coordinator(
        self,
        session: AsyncSession,
        entity_repo: EntityRepository,
        *,
        pool: SessionPool | None = None,
    ) -> TaskCoordinator:
        """Build a TaskCoordinator wired with this infrastructure's client
        and reasoning model. ``pool`` defaults to a generous per-task cap."""
        return TaskCoordinator(
            client=self.llm_client,
            session=session,
            entity_repo=entity_repo,
            model=self.reasoning_model,
            pool=pool or SessionPool(max_concurrent=10),
        )

    def make_repo_agent(
        self,
        session: AsyncSession,
        repo_id: uuid.UUID,
        *,
        max_context_tokens: int = 5000,
    ) -> RepoAgent:
        """Build a RepoAgent wired with this infrastructure's client and
        agent model."""
        return RepoAgent(
            client=self.llm_client,
            session=session,
            repo_id=repo_id,
            model=self.agent_model,
            max_context_tokens=max_context_tokens,
        )

    def make_state_backend(self, session: AsyncSession) -> AgentStateBackend:
        """Return the track's AgentStateBackend for this session.

        Track A → PostgresStateBackend. Track B would return a
        MemoryStoreBackend, but ``create_agent_infrastructure`` already
        refuses Track B, so this only ever builds the Postgres backend
        today. Routing agent state through this seam (rather than raw
        session access) is what keeps the Track A↔B swap a config change."""
        return PostgresStateBackend(session)


def create_agent_infrastructure(
    settings: BifrostSettings | None = None,
) -> AgentInfrastructure:
    """Build the agent infrastructure for the active track.

    Track A (default): Messages-via-``claude -p`` subprocess.
    Track B: raises :class:`TrackBNotAvailableError` until access is real.
    """
    settings = settings or get_settings()
    track = (settings.track or "A").upper()

    if track == "B":
        raise TrackBNotAvailableError(
            "BIFROST_TRACK=B selected, but Managed Agents Research Preview "
            "access is not available. The Track B runtime (ManagedAgentRuntime) "
            "and backend (MemoryStoreBackend) are not implemented. "
            "Set BIFROST_TRACK=A."
        )

    return AgentInfrastructure(
        track=track,
        llm_client=ClaudeLlmClient(settings),
        reasoning_model=settings.reasoning_model,
        agent_model=settings.agent_model,
    )
