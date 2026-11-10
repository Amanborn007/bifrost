"""Unit tests for the already-built detector in bifrost.research.fast_path.

Pure unit tests — no database, no network. We test:
1. Intent detection regex (positive + negative cases).
2. Token extraction from questions.
3. check_already_built() with a mocked session_factory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from bifrost.research.fast_path import _BUILD_REQUEST_PATTERNS, check_already_built

# ── Intent detection tests ────────────────────────────────────────────────────


class TestBuildRequestIntentDetection:
    """Verify _BUILD_REQUEST_PATTERNS matches the right questions."""

    # --- Positive cases (should match) ---

    def test_need_to_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("I need to build a retry queue")

    def test_want_to_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("We want to build a new shipment service")

    def test_how_do_i_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("How do I build a distributed lock?")

    def test_how_would_i_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("How would I build a circuit breaker here?")

    def test_how_do_i_implement(self):
        assert _BUILD_REQUEST_PATTERNS.search("How do I implement rate limiting ourselves?")

    def test_how_would_i_implement(self):
        assert _BUILD_REQUEST_PATTERNS.search("How would I implement a saga pattern?")

    def test_build_from_scratch(self):
        assert _BUILD_REQUEST_PATTERNS.search("We want to build a message queue from scratch")

    def test_implement_ourselves(self):
        assert _BUILD_REQUEST_PATTERNS.search("Should we implement the retry logic ourselves?")

    def test_what_would_it_take_to_build(self):
        assert _BUILD_REQUEST_PATTERNS.search(
            "What would it take to build a deduplication service?"
        )

    def test_we_should_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("We should build an outbox pattern")

    def test_planning_to_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("We're planning to build a new dispatcher")

    def test_thinking_of_building(self):
        assert _BUILD_REQUEST_PATTERNS.search("I'm thinking of building a cache layer")

    def test_looking_to_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("We're looking to build a pub-sub system")

    def test_going_to_build(self):
        assert _BUILD_REQUEST_PATTERNS.search("We're going to build a new inventory service")

    # --- Negative cases (should NOT match) ---

    def test_what_is(self):
        assert not _BUILD_REQUEST_PATTERNS.search("What is RetryQueue?")

    def test_who_owns(self):
        assert not _BUILD_REQUEST_PATTERNS.search("Who owns the orders service?")

    def test_how_does(self):
        assert not _BUILD_REQUEST_PATTERNS.search("How does the circuit breaker work?")

    def test_describe(self):
        assert not _BUILD_REQUEST_PATTERNS.search("Describe the outbox pattern implementation")

    def test_blast_radius(self):
        assert not _BUILD_REQUEST_PATTERNS.search("What's the blast radius of changing RetryQueue?")

    def test_generic_question(self):
        assert not _BUILD_REQUEST_PATTERNS.search(
            "What dependencies does FulfillmentExecutor have?"
        )


# ── check_already_built() integration tests (mocked DB) ──────────────────────


def _make_entity_row(
    name: str,
    urn: str,
    entity_type: str,
    repo_id: uuid.UUID | None = None,
    confidence: float = 0.8,
) -> MagicMock:
    row = MagicMock()
    row.name = name
    row.urn = urn
    row.entity_type = entity_type
    row.scope_repo_id = repo_id
    row.confidence = confidence
    return row


def _make_repo_row(repo_id: uuid.UUID, name: str) -> MagicMock:
    row = MagicMock()
    row.id = repo_id
    row.name = name
    return row


def _make_session(entity_rows: list, repo_rows: list) -> MagicMock:
    """Build a mock async session that returns the given rows for each execute()."""
    call_count = 0
    results = [entity_rows, repo_rows]

    async def _execute(stmt):
        nonlocal call_count
        result = MagicMock()
        current = results[call_count] if call_count < len(results) else []
        call_count += 1
        result.all.return_value = current
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_session_factory(entity_rows: list, repo_rows: list):
    session = _make_session(entity_rows, repo_rows)
    factory = MagicMock()
    # async context manager
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_settings() -> MagicMock:
    settings = MagicMock()
    return settings


async def _collect(gen: AsyncIterator[dict]) -> list[dict]:
    events = []
    async for evt in gen:
        events.append(evt)
    return events


class TestCheckAlreadyBuilt:
    """check_already_built() emits events when appropriate."""

    @pytest.mark.asyncio
    async def test_emits_already_built_when_match_found(self):
        repo_id = uuid.uuid4()
        entity_rows = [
            _make_entity_row(
                name="RetryQueue",
                urn="urn:bifrost:orders-service:component:RetryQueue",
                entity_type="component",
                repo_id=repo_id,
                confidence=0.9,
            )
        ]
        repo_rows = [_make_repo_row(repo_id, "orders-service")]
        factory = _make_session_factory(entity_rows, repo_rows)
        settings = _make_settings()

        events = await _collect(
            check_already_built(
                "I need to build a retry queue for our shipment flow",
                factory,
                settings,
            )
        )

        assert len(events) == 1
        evt = events[0]
        assert evt["type"] == "already_built"
        assert len(evt["matches"]) == 1
        assert evt["matches"][0]["name"] == "RetryQueue"
        assert evt["matches"][0]["entity_type"] == "component"
        assert evt["matches"][0]["repo_name"] == "orders-service"
        assert "Found 1 existing implementation" in evt["message"]

    @pytest.mark.asyncio
    async def test_emits_nothing_when_not_build_question(self):
        factory = _make_session_factory([], [])
        settings = _make_settings()

        events = await _collect(check_already_built("What is RetryQueue?", factory, settings))

        assert events == []

    @pytest.mark.asyncio
    async def test_emits_nothing_when_no_db_match(self):
        # Build question but DB returns nothing
        factory = _make_session_factory([], [])
        settings = _make_settings()

        events = await _collect(
            check_already_built("We need to build a retry queue from scratch", factory, settings)
        )

        assert events == []

    @pytest.mark.asyncio
    async def test_multiple_matches_reported(self):
        repo_id = uuid.uuid4()
        entity_rows = [
            _make_entity_row("RetryQueue", "urn:a", "component", repo_id, 0.9),
            _make_entity_row("RetryPolicy", "urn:b", "component", repo_id, 0.75),
        ]
        repo_rows = [_make_repo_row(repo_id, "orders-service")]
        factory = _make_session_factory(entity_rows, repo_rows)
        settings = _make_settings()

        events = await _collect(
            check_already_built("We want to build a retry mechanism", factory, settings)
        )

        assert len(events) == 1
        assert len(events[0]["matches"]) == 2
        assert "Found 2 existing implementations" in events[0]["message"]

    @pytest.mark.asyncio
    async def test_handles_missing_repo_gracefully(self):
        entity_rows = [
            _make_entity_row(
                "CircuitBreaker",
                "urn:c",
                "component",
                repo_id=None,  # no repo
                confidence=0.85,
            )
        ]
        factory = _make_session_factory(entity_rows, [])
        settings = _make_settings()

        events = await _collect(
            check_already_built("How would I build a circuit breaker?", factory, settings)
        )

        assert len(events) == 1
        assert events[0]["matches"][0]["repo_name"] == "(unknown)"

    @pytest.mark.asyncio
    async def test_db_exception_is_silent(self):
        """DB failure must not propagate — check_already_built swallows it."""
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(
            side_effect=RuntimeError("DB connection refused")
        )
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        settings = _make_settings()

        # Must not raise
        events = await _collect(
            check_already_built("We need to build an event bus from scratch", factory, settings)
        )

        assert events == []

    @pytest.mark.asyncio
    async def test_ownership_question_does_not_trigger(self):
        factory = _make_session_factory([], [])
        settings = _make_settings()

        events = await _collect(
            check_already_built("Who owns the orders service?", factory, settings)
        )

        assert events == []

    @pytest.mark.asyncio
    async def test_definition_question_does_not_trigger(self):
        factory = _make_session_factory([], [])
        settings = _make_settings()

        events = await _collect(
            check_already_built("How does the outbox pattern work?", factory, settings)
        )

        assert events == []
