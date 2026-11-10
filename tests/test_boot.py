"""Tests for the agent-infrastructure composition root (boot.py).

Track-A↔B seam. The factory is the single place that wires
agent dependencies per track; before it, construction was duplicated
across ask.py / dispatcher.py and BIFROST_TRACK was decorative.
"""

from __future__ import annotations

import uuid

import pytest

from bifrost.agents.coordinator import TaskCoordinator
from bifrost.agents.repo import RepoAgent
from bifrost.boot import (
    AgentInfrastructure,
    TrackBNotAvailableError,
    create_agent_infrastructure,
)
from bifrost.cli.config import BifrostSettings
from bifrost.extraction.cli_client import ClaudeLlmClient


def _settings(track: str = "A") -> BifrostSettings:
    return BifrostSettings(track=track)


def test_track_a_builds_infrastructure() -> None:
    infra = create_agent_infrastructure(_settings("A"))
    assert isinstance(infra, AgentInfrastructure)
    assert infra.track == "A"
    assert isinstance(infra.llm_client, ClaudeLlmClient)
    # Models come from settings, not hard-coded at call sites.
    assert infra.reasoning_model == _settings().reasoning_model
    assert infra.agent_model == _settings().agent_model


def test_track_b_fails_loud() -> None:
    """BIFROST_TRACK=B must raise, not silently fall back to Track A."""
    with pytest.raises(TrackBNotAvailableError):
        create_agent_infrastructure(_settings("B"))


def test_make_coordinator_returns_wired_coordinator() -> None:
    infra = create_agent_infrastructure(_settings("A"))
    coordinator = infra.make_coordinator(session=None, entity_repo=None)  # type: ignore[arg-type]
    assert isinstance(coordinator, TaskCoordinator)
    # Coordinator uses the infrastructure's shared client + reasoning model.
    assert coordinator._client is infra.llm_client  # noqa: SLF001
    assert coordinator._model == infra.reasoning_model  # noqa: SLF001


def test_make_repo_agent_returns_wired_agent() -> None:
    infra = create_agent_infrastructure(_settings("A"))
    rid = uuid.uuid4()
    agent = infra.make_repo_agent(session=None, repo_id=rid)  # type: ignore[arg-type]
    assert isinstance(agent, RepoAgent)
    assert agent._client is infra.llm_client  # noqa: SLF001
    assert agent._model == infra.agent_model  # noqa: SLF001


def test_default_settings_uses_track_a() -> None:
    # No explicit settings → reads get_settings(); defaults to Track A.
    infra = create_agent_infrastructure()
    assert infra.track == "A"


# ── A3: AgentRuntime contract ────────────────────────────────────────


def test_agents_expose_stable_definition_ids() -> None:
    """get_definition_id() is the Track-B mapping seam — stable + distinct
    per agent template."""
    infra = create_agent_infrastructure()
    coordinator = infra.make_coordinator(session=None, entity_repo=None)  # type: ignore[arg-type]
    repo_agent = infra.make_repo_agent(session=None, repo_id=uuid.uuid4())  # type: ignore[arg-type]

    assert coordinator.get_definition_id() == "task-coordinator"
    assert repo_agent.get_definition_id() == "repo-agent"
    # Distinct from the per-instance agent_id.
    assert repo_agent.get_definition_id() != repo_agent._agent_id  # noqa: SLF001


def test_agent_runtime_requires_definition_id() -> None:
    """The ABC enforces get_definition_id — a subclass missing it can't
    instantiate (the seam is a real contract, not a fiction)."""
    from bifrost.agents.runtime import AgentRuntime

    class IncompleteAgent(AgentRuntime):
        async def run(self, task):  # type: ignore[no-untyped-def]
            ...

        async def checkpoint(self):  # type: ignore[no-untyped-def]
            ...

        async def get_state(self):  # type: ignore[no-untyped-def]
            ...

        # get_definition_id intentionally omitted

    with pytest.raises(TypeError):
        IncompleteAgent()  # type: ignore[abstract]
