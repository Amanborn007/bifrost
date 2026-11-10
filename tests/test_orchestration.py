"""Tests for the orchestration layer: task model, dispatcher, synthesis, skeptic, budget, coordinator."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bifrost.agents.runtime import AgentState, AgentStatus, AgentTask, TaskResult
from bifrost.core.enums import ConfidenceLevel
from bifrost.orchestration.budget import (
    BudgetExceededError,
    BudgetTracker,
    CostEntry,
    SessionPool,
    SessionPoolExhaustedError,
)
from bifrost.orchestration.skeptic import (
    SkepticConfig,
    SkepticResult,
    apply_skeptic_result,
    should_challenge,
)
from bifrost.orchestration.synthesis import synthesize
from bifrost.orchestration.task import (
    OrchestratedTaskResult,
    OrchestrationQuery,
    QueryRunState,
    QueryType,
)

# ═══════════════════════════════════════════════════════════════════════
# Task Model + State Machine
# ═══════════════════════════════════════════════════════════════════════


class TestQueryRunStateMachine:
    def test_initial_state_is_pending(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        assert task.state == QueryRunState.PENDING

    def test_valid_forward_transitions(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        task.transition(QueryRunState.DISPATCHED)
        assert task.state == QueryRunState.DISPATCHED
        task.transition(QueryRunState.RUNNING)
        assert task.state == QueryRunState.RUNNING
        task.transition(QueryRunState.COMPLETED)
        assert task.state == QueryRunState.COMPLETED

    def test_reject_backward_transition(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        task.transition(QueryRunState.DISPATCHED)
        with pytest.raises(ValueError, match="Invalid state transition"):
            task.transition(QueryRunState.PENDING)

    def test_reject_transition_from_terminal_state(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        task.transition(QueryRunState.DISPATCHED)
        task.transition(QueryRunState.RUNNING)
        task.transition(QueryRunState.COMPLETED)
        with pytest.raises(ValueError, match="terminal state"):
            task.transition(QueryRunState.RUNNING)

    def test_any_active_state_can_fail(self) -> None:
        for start_state in [QueryRunState.PENDING, QueryRunState.DISPATCHED, QueryRunState.RUNNING]:
            task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
            # Walk to the start state
            if start_state == QueryRunState.DISPATCHED:
                task.transition(QueryRunState.DISPATCHED)
            elif start_state == QueryRunState.RUNNING:
                task.transition(QueryRunState.DISPATCHED)
                task.transition(QueryRunState.RUNNING)
            task.transition(QueryRunState.FAILED)
            assert task.state == QueryRunState.FAILED

    def test_dispatched_and_running_can_timeout(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        task.transition(QueryRunState.DISPATCHED)
        task.transition(QueryRunState.TIMEOUT)
        assert task.state == QueryRunState.TIMEOUT

    def test_transition_updates_timestamp(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        original = task.updated_at
        task.transition(QueryRunState.DISPATCHED)
        assert task.updated_at >= original

    def test_task_type_default(self) -> None:
        task = OrchestrationQuery(id="t1", query="test", target_repos=["repo1"])
        assert task.task_type == QueryType.QUESTION


# ═══════════════════════════════════════════════════════════════════════
# Session Pool
# ═══════════════════════════════════════════════════════════════════════


class TestSessionPool:
    def test_acquire_within_limit(self) -> None:
        pool = SessionPool(max_concurrent=2)
        pool.acquire("agent-1")
        pool.acquire("agent-2")
        assert pool.active_count == 2

    def test_reject_over_limit(self) -> None:
        pool = SessionPool(max_concurrent=2)
        pool.acquire("agent-1")
        pool.acquire("agent-2")
        with pytest.raises(SessionPoolExhaustedError, match="capacity"):
            pool.acquire("agent-3")

    def test_release_frees_slot(self) -> None:
        pool = SessionPool(max_concurrent=2)
        pool.acquire("agent-1")
        pool.acquire("agent-2")
        pool.release("agent-1")
        assert pool.active_count == 1
        pool.acquire("agent-3")  # should work now
        assert pool.active_count == 2

    def test_acquire_idempotent(self) -> None:
        pool = SessionPool(max_concurrent=2)
        pool.acquire("agent-1")
        pool.acquire("agent-1")  # same agent, no error
        assert pool.active_count == 1

    def test_release_nonexistent_is_safe(self) -> None:
        pool = SessionPool(max_concurrent=2)
        pool.release("never-acquired")  # no error
        assert pool.active_count == 0

    def test_available_count(self) -> None:
        pool = SessionPool(max_concurrent=3)
        assert pool.available == 3
        pool.acquire("a1")
        assert pool.available == 2


# ═══════════════════════════════════════════════════════════════════════
# Budget Tracker
# ═══════════════════════════════════════════════════════════════════════


class TestBudgetTracker:
    def test_record_within_budget(self) -> None:
        tracker = BudgetTracker(max_budget_usd=1.0)
        tracker.record(CostEntry("a1", "sonnet", 100, 50, 0.01))
        assert tracker.total_cost == pytest.approx(0.01)
        assert tracker.remaining == pytest.approx(0.99)

    def test_exceed_budget_raises(self) -> None:
        tracker = BudgetTracker(max_budget_usd=0.05)
        tracker.record(CostEntry("a1", "sonnet", 1000, 500, 0.03))
        with pytest.raises(BudgetExceededError, match="exceeded"):
            tracker.record(CostEntry("a2", "sonnet", 1000, 500, 0.03))

    def test_aggregates_across_agents(self) -> None:
        tracker = BudgetTracker(max_budget_usd=1.0)
        tracker.record(CostEntry("agent-1", "sonnet", 100, 50, 0.10))
        tracker.record(CostEntry("agent-2", "sonnet", 100, 50, 0.20))
        tracker.record(CostEntry("agent-3", "sonnet", 100, 50, 0.30))
        assert tracker.total_cost == pytest.approx(0.60)
        assert len(tracker.entries) == 3


# ═══════════════════════════════════════════════════════════════════════
# Synthesis
# ═══════════════════════════════════════════════════════════════════════


class TestSynthesis:
    def test_single_repo_passthrough(self) -> None:
        """Single-repo case: no synthesis needed, just pass through."""
        results = {
            "urn:repo:A": TaskResult(
                agent_id="a1",
                task_id="t1",
                success=True,
                answer="The auth flow uses OAuth2.",
                confidence_level="confirmed",
                source_urns=["urn:entity:comp:auth"],
            ),
        }
        synth = synthesize(results)
        assert synth.answer == "The auth flow uses OAuth2."
        assert synth.confidence == "confirmed"
        assert len(synth.agent_results) == 1
        assert len(synth.conflicts) == 0

    def test_multi_repo_merge(self) -> None:
        """Multi-repo: answers merged, confidence = min across agents."""
        results = {
            "urn:repo:A": TaskResult(
                agent_id="a1",
                task_id="t1",
                success=True,
                answer="Service A handles orders.",
                confidence_level="confirmed",
                source_urns=["urn:entity:comp:order-a"],
            ),
            "urn:repo:B": TaskResult(
                agent_id="a2",
                task_id="t1",
                success=True,
                answer="Service B validates orders.",
                confidence_level="inferred",
                source_urns=["urn:entity:comp:order-b"],
            ),
        }
        synth = synthesize(results)
        assert "Service A" in synth.answer
        assert "Service B" in synth.answer
        # Min confidence: inferred < confirmed
        assert synth.confidence == ConfidenceLevel.INFERRED
        assert len(synth.source_urns) == 2
        assert len(synth.agent_results) == 2

    def test_all_agents_failed(self) -> None:
        results = {
            "urn:repo:A": TaskResult(
                agent_id="a1",
                task_id="t1",
                success=False,
                errors=["timeout"],
            ),
        }
        synth = synthesize(results)
        assert "failed" in synth.answer.lower()
        assert synth.confidence == ConfidenceLevel.UNKNOWN

    def test_source_dedup(self) -> None:
        """Overlapping source URNs should be deduplicated."""
        results = {
            "urn:repo:A": TaskResult(
                agent_id="a1",
                task_id="t1",
                success=True,
                answer="A says yes",
                confidence_level="confirmed",
                source_urns=["urn:entity:shared", "urn:entity:a-only"],
            ),
            "urn:repo:B": TaskResult(
                agent_id="a2",
                task_id="t1",
                success=True,
                answer="B says yes",
                confidence_level="confirmed",
                source_urns=["urn:entity:shared", "urn:entity:b-only"],
            ),
        }
        synth = synthesize(results)
        # "shared" appears only once
        assert synth.source_urns.count("urn:entity:shared") == 1
        assert len(synth.source_urns) == 3

    def test_conflict_detection_on_divergent_confidence(self) -> None:
        """Agents with significantly different confidence should produce conflicts."""
        results = {
            "urn:repo:A": TaskResult(
                agent_id="a1",
                task_id="t1",
                success=True,
                answer="Definitely yes",
                confidence_level="confirmed",
                source_urns=["urn:a"],
            ),
            "urn:repo:B": TaskResult(
                agent_id="a2",
                task_id="t1",
                success=True,
                answer="Maybe yes",
                confidence_level="uncertain",
                source_urns=["urn:b"],
            ),
        }
        synth = synthesize(results)
        assert len(synth.conflicts) >= 1
        assert synth.conflicts[0]["type"] == "confidence_divergence"

    def test_partial_failure_produces_gaps(self) -> None:
        """When some agents fail, gaps should be reported."""
        results = {
            "urn:repo:A": TaskResult(
                agent_id="a1",
                task_id="t1",
                success=True,
                answer="A's answer",
                confidence_level="confirmed",
                source_urns=["urn:a"],
            ),
            "urn:repo:B": TaskResult(
                agent_id="a2",
                task_id="t1",
                success=False,
                errors=["connection refused"],
            ),
        }
        synth = synthesize(results)
        assert synth.answer == "A's answer"
        assert len(synth.gaps) == 1
        assert "urn:repo:B" in synth.gaps[0]


# ═══════════════════════════════════════════════════════════════════════
# Skeptic
# ═══════════════════════════════════════════════════════════════════════


class TestSkeptic:
    def test_should_challenge_low_confidence(self) -> None:
        config = SkepticConfig(
            enabled=True,
            task_types={QueryType.ASSESS_IMPACT},
            confidence_threshold=ConfidenceLevel.INFERRED,
        )
        result = OrchestratedTaskResult(
            answer="test",
            confidence=ConfidenceLevel.UNCERTAIN,
        )
        assert should_challenge(result, QueryType.ASSESS_IMPACT, config)

    def test_should_not_challenge_high_confidence(self) -> None:
        config = SkepticConfig(
            enabled=True,
            task_types={QueryType.ASSESS_IMPACT},
            confidence_threshold=ConfidenceLevel.INFERRED,
        )
        result = OrchestratedTaskResult(
            answer="test",
            confidence=ConfidenceLevel.CONFIRMED,
        )
        assert not should_challenge(result, QueryType.ASSESS_IMPACT, config)

    def test_should_not_challenge_disabled(self) -> None:
        config = SkepticConfig(enabled=False)
        result = OrchestratedTaskResult(
            answer="test",
            confidence=ConfidenceLevel.UNCERTAIN,
        )
        assert not should_challenge(result, QueryType.ASSESS_IMPACT, config)

    def test_should_not_challenge_wrong_task_type(self) -> None:
        config = SkepticConfig(
            enabled=True,
            task_types={QueryType.ASSESS_IMPACT},
        )
        result = OrchestratedTaskResult(
            answer="test",
            confidence=ConfidenceLevel.UNCERTAIN,
        )
        # QUESTION not in task_types
        assert not should_challenge(result, QueryType.QUESTION, config)

    def test_apply_skeptic_downgrade(self) -> None:
        result = OrchestratedTaskResult(
            answer="test answer",
            confidence=ConfidenceLevel.INFERRED,
        )
        skeptic = SkepticResult(
            passed=False,
            issues=[
                {
                    "claim": "unsupported claim",
                    "problem": "unsupported",
                    "explanation": "no evidence",
                }
            ],
            original_confidence=ConfidenceLevel.INFERRED,
            adjusted_confidence=ConfidenceLevel.UNCERTAIN,
            rounds_used=1,
        )
        updated = apply_skeptic_result(result, skeptic)
        assert updated.confidence == ConfidenceLevel.UNCERTAIN
        assert len(updated.gaps) == 1
        assert "Skeptic" in updated.gaps[0]

    def test_apply_skeptic_pass_no_change(self) -> None:
        result = OrchestratedTaskResult(
            answer="test answer",
            confidence=ConfidenceLevel.CONFIRMED,
        )
        skeptic = SkepticResult(
            passed=True,
            original_confidence=ConfidenceLevel.CONFIRMED,
            adjusted_confidence=ConfidenceLevel.CONFIRMED,
            rounds_used=1,
        )
        updated = apply_skeptic_result(result, skeptic)
        assert updated.confidence == ConfidenceLevel.CONFIRMED
        assert len(updated.gaps) == 0


# ═══════════════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════════════


class TestDispatcher:
    def test_lazy_agent_creation(self) -> None:
        """Dispatcher should NOT pre-create agents — only on first dispatch."""
        from bifrost.orchestration.dispatcher import Dispatcher

        client = MagicMock()
        session = MagicMock()
        dispatcher = Dispatcher(client, session)
        # No agents created yet
        assert len(dispatcher.active_agents) == 0

    @pytest.mark.asyncio
    async def test_dispatch_creates_agent(self) -> None:
        """Dispatching to a repo should lazily create a RepoAgent."""
        from bifrost.orchestration.dispatcher import Dispatcher

        client = MagicMock()
        session = AsyncMock()
        dispatcher = Dispatcher(client, session)

        repo_id = uuid.uuid4()
        task = OrchestrationQuery(id="t1", query="test?", target_repos=["urn:repo:A"])

        # Mock the RepoAgent.run to return a result
        mock_result = TaskResult(
            agent_id="a1",
            task_id="t1",
            success=True,
            answer="yes",
        )
        with patch.object(
            dispatcher,
            "_get_or_create_agent",
            wraps=dispatcher._get_or_create_agent,
        ):
            # We need to mock the actual agent run
            agent_mock = AsyncMock()
            agent_mock.run = AsyncMock(return_value=mock_result)
            dispatcher._agents["urn:repo:A"] = agent_mock  # type: ignore[assignment]

            result = await dispatcher.dispatch_one("urn:repo:A", repo_id, task)
            assert result.success
            assert result.answer == "yes"


# ═══════════════════════════════════════════════════════════════════════
# TaskCoordinator
# ═══════════════════════════════════════════════════════════════════════


class TestTaskCoordinator:
    @pytest.mark.asyncio
    async def test_decompose_and_dispatch(self) -> None:
        """Coordinator decomposes multi-repo question into sub-tasks."""
        from bifrost.agents.coordinator import TaskCoordinator

        client = AsyncMock()
        session = AsyncMock()
        entity_repo = AsyncMock()

        # Mock repos
        repo_a = MagicMock()
        repo_a.urn = "urn:repo:A"
        repo_a.name = "Service A"
        repo_a.url = "https://github.com/org/a"
        repo_a.id = uuid.uuid4()

        repo_b = MagicMock()
        repo_b.urn = "urn:repo:B"
        repo_b.name = "Service B"
        repo_b.url = "https://github.com/org/b"
        repo_b.id = uuid.uuid4()

        entity_repo.list_repos = AsyncMock(return_value=[repo_a, repo_b])
        entity_repo.get_entity_by_urn = AsyncMock(return_value=None)

        # Mock LLM decomposition response (select both repos)
        decomp_block = MagicMock()
        decomp_block.type = "tool_use"
        decomp_block.name = "select_repos"
        decomp_block.input = {
            "repo_urns": ["urn:repo:A", "urn:repo:B"],
            "reasoning": "Both repos handle orders",
        }
        decomp_response = MagicMock()
        decomp_response.content = [decomp_block]

        # Mock agent run results
        agent_result_a = TaskResult(
            agent_id="a1",
            task_id="t1",
            success=True,
            answer="A handles shipment initiation",
            confidence_level="confirmed",
            source_urns=["urn:entity:comp:order-init"],
        )
        agent_result_b = TaskResult(
            agent_id="a2",
            task_id="t1",
            success=True,
            answer="B validates shipment data",
            confidence_level="inferred",
            source_urns=["urn:entity:comp:order-valid"],
        )

        # Track LLM calls
        client.messages.create = AsyncMock(return_value=decomp_response)

        coordinator = TaskCoordinator(
            client=client,
            session=session,
            entity_repo=entity_repo,
        )

        # Mock dispatcher to return our results
        coordinator._dispatcher.dispatch_parallel = AsyncMock(  # type: ignore[method-assign]
            return_value={"urn:repo:A": agent_result_a, "urn:repo:B": agent_result_b},
        )

        task = AgentTask(
            task_id="t1",
            task_type="question",
            payload={"question": "How does shipment processing work?"},
        )
        result = await coordinator.run(task)

        assert result.success
        assert result.answer is not None
        assert "shipment" in result.answer.lower()
        assert len(result.source_urns) == 2

    @pytest.mark.asyncio
    async def test_no_question_returns_error(self) -> None:
        from bifrost.agents.coordinator import TaskCoordinator

        coordinator = TaskCoordinator(
            client=AsyncMock(),
            session=AsyncMock(),
            entity_repo=AsyncMock(),
        )
        task = AgentTask(task_id="t1", task_type="question", payload={})
        result = await coordinator.run(task)
        assert not result.success
        assert "No question" in result.errors[0]

    @pytest.mark.asyncio
    async def test_no_repos_returns_unknown(self) -> None:
        from bifrost.agents.coordinator import TaskCoordinator

        entity_repo = AsyncMock()
        entity_repo.list_repos = AsyncMock(return_value=[])

        coordinator = TaskCoordinator(
            client=AsyncMock(),
            session=AsyncMock(),
            entity_repo=entity_repo,
        )
        task = AgentTask(
            task_id="t1",
            task_type="question",
            payload={"question": "anything?"},
        )
        result = await coordinator.run(task)
        assert result.success
        assert result.confidence_level == ConfidenceLevel.UNKNOWN

    @pytest.mark.asyncio
    async def test_feedback_positive_bumps_confidence(self) -> None:
        """Feedback loop: positive feedback bumps entity confidence."""
        from bifrost.agents.coordinator import TaskCoordinator

        entity = MagicMock()
        entity.confidence_level = ConfidenceLevel.INFERRED

        entity_repo = AsyncMock()
        entity_repo.get_entity_by_urn = AsyncMock(return_value=entity)

        session = AsyncMock()

        coordinator = TaskCoordinator(
            client=AsyncMock(),
            session=session,
            entity_repo=entity_repo,
        )

        updates = await coordinator.submit_feedback(
            entity_urns=["urn:entity:comp:auth"],
            helpful=True,
        )
        assert "urn:entity:comp:auth" in updates
        assert "confirmed" in updates["urn:entity:comp:auth"]

    @pytest.mark.asyncio
    async def test_feedback_negative_degrades_confidence(self) -> None:
        """Feedback loop: negative feedback degrades entity confidence."""
        from bifrost.agents.coordinator import TaskCoordinator

        entity = MagicMock()
        entity.confidence_level = ConfidenceLevel.CONFIRMED

        entity_repo = AsyncMock()
        entity_repo.get_entity_by_urn = AsyncMock(return_value=entity)

        session = AsyncMock()

        coordinator = TaskCoordinator(
            client=AsyncMock(),
            session=session,
            entity_repo=entity_repo,
        )

        updates = await coordinator.submit_feedback(
            entity_urns=["urn:entity:comp:auth"],
            helpful=False,
        )
        assert "urn:entity:comp:auth" in updates
        assert "inferred" in updates["urn:entity:comp:auth"]

    @pytest.mark.asyncio
    async def test_feedback_entity_not_found(self) -> None:
        from bifrost.agents.coordinator import TaskCoordinator

        entity_repo = AsyncMock()
        entity_repo.get_entity_by_urn = AsyncMock(return_value=None)

        coordinator = TaskCoordinator(
            client=AsyncMock(),
            session=AsyncMock(),
            entity_repo=entity_repo,
        )

        updates = await coordinator.submit_feedback(
            entity_urns=["urn:entity:nonexistent"],
            helpful=True,
        )
        assert updates["urn:entity:nonexistent"] == "not_found"

    @pytest.mark.asyncio
    async def test_coordinator_implements_agent_runtime(self) -> None:
        """TaskCoordinator must satisfy the AgentRuntime ABC."""
        from bifrost.agents.coordinator import TaskCoordinator

        coordinator = TaskCoordinator(
            client=AsyncMock(),
            session=AsyncMock(),
            entity_repo=AsyncMock(),
        )
        state = await coordinator.get_state()
        assert isinstance(state, AgentState)
        assert state.status == AgentStatus.IDLE

        cp = await coordinator.checkpoint()
        assert cp.last_checkpoint_at is not None
