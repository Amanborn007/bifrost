"""Unit tests for the per-requester rate limiter."""

from __future__ import annotations

from bifrost.collaboration.rate_limit import check_and_consume, reset


def test_first_call_within_limit_is_allowed() -> None:
    """A single call inside an empty bucket should be allowed and
    not produce a positive ``retry_after``."""
    reset()
    allowed, retry_after = check_and_consume("alice", limit=3, window_seconds=60)
    assert allowed is True
    assert retry_after == 0


def test_burst_past_limit_is_denied_with_retry_after() -> None:
    """The N+1 call inside the same window must be denied with a
    positive ``retry_after`` so clients can back off."""
    reset()
    limit = 3
    for _ in range(limit):
        allowed, _ = check_and_consume("bob", limit=limit, window_seconds=60)
        assert allowed is True
    # 4th call within the same minute — this is the runaway-client
    # scenario the limiter is designed to stop.
    allowed, retry_after = check_and_consume("bob", limit=limit, window_seconds=60)
    assert allowed is False
    assert retry_after >= 1
    # Other requesters share no state with bob's bucket.
    allowed, _ = check_and_consume("carol", limit=limit, window_seconds=60)
    assert allowed is True
