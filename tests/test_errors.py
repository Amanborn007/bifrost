"""Tests for the central error taxonomy + with_retry.

Locks the three invariants every caller relies on:
  * the (code, retryable, http_status) triple per error class,
  * per-instance override + retry_after carry-through,
  * with_retry's retry/give-up/backoff behavior — including that it honors
    a Retry-After hint and never sleeps in tests (injected sleep).
"""

from __future__ import annotations

import pytest

from bifrost.errors import (
    AuthError,
    BifrostError,
    ConfigError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    TransientError,
    UpstreamError,
    ValidationError,
    with_retry,
)

# ── Taxonomy invariants ──────────────────────────────────────────────


def test_base_defaults() -> None:
    err = BifrostError("boom")
    assert err.code == "bifrost.error"
    assert err.retryable is False
    assert err.http_status == 500
    assert err.message == "boom"
    assert err.retry_after is None
    assert err.details == {}


@pytest.mark.parametrize(
    ("cls", "code", "retryable", "status"),
    [
        (ConfigError, "config.invalid", False, 500),
        (ValidationError, "request.invalid", False, 400),
        (AuthError, "auth.denied", False, 401),
        (NotFoundError, "resource.not_found", False, 404),
        (ConflictError, "state.conflict", False, 409),
        (RateLimitError, "upstream.rate_limited", True, 429),
        (TransientError, "upstream.transient", True, 503),
        (UpstreamError, "upstream.error", False, 502),
    ],
)
def test_subclass_triples(cls, code, retryable, status) -> None:
    err = cls("x")
    assert err.code == code
    assert err.retryable is retryable
    assert err.http_status == status
    assert isinstance(err, BifrostError)


def test_per_instance_override() -> None:
    """A one-off raise can override the class defaults without a subclass."""
    err = BifrostError(
        "custom",
        code="my.code",
        retryable=True,
        http_status=418,
        retry_after=2.5,
        details={"k": "v"},
    )
    assert err.code == "my.code"
    assert err.retryable is True
    assert err.http_status == 418
    assert err.retry_after == 2.5
    assert err.details == {"k": "v"}
    # Class default is untouched by the instance override.
    assert BifrostError.retryable is False


def test_only_retryable_classes_are_retryable() -> None:
    assert RateLimitError("x").retryable is True
    assert TransientError("x").retryable is True
    assert NotFoundError("x").retryable is False


# ── with_retry ───────────────────────────────────────────────────────


async def _noop_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_returns_on_first_success() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    out = await with_retry(fn, sleep=_noop_sleep)
    assert out == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_retries_retryable_then_succeeds() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientError("flaky")
        return "recovered"

    out = await with_retry(fn, max_attempts=3, sleep=_noop_sleep)
    assert out == "recovered"
    assert calls == 3


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise RateLimitError("always limited")

    with pytest.raises(RateLimitError):
        await with_retry(fn, max_attempts=3, sleep=_noop_sleep)
    assert calls == 3  # tried exactly max_attempts


@pytest.mark.asyncio
async def test_non_retryable_raises_immediately() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise NotFoundError("nope")

    with pytest.raises(NotFoundError):
        await with_retry(fn, max_attempts=5, sleep=_noop_sleep)
    assert calls == 1  # not retried


@pytest.mark.asyncio
async def test_raw_exception_not_retried_by_default() -> None:
    """Non-Bifrost errors are not retryable unless retry_on opts in."""
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("raw")

    with pytest.raises(ValueError):
        await with_retry(fn, max_attempts=3, sleep=_noop_sleep)
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_on_predicate_opts_in_raw_errors() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ValueError("raw transient")
        return "ok"

    out = await with_retry(
        fn,
        max_attempts=3,
        retry_on=lambda exc: isinstance(exc, ValueError),
        sleep=_noop_sleep,
    )
    assert out == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_honors_retry_after_hint() -> None:
    """When the error carries retry_after, with_retry sleeps exactly that —
    not the exponential backoff."""
    slept: list[float] = []

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RateLimitError("slow down", retry_after=7.0)
        return "ok"

    out = await with_retry(fn, max_attempts=3, base_delay=1.0, jitter=0.0, sleep=record_sleep)
    assert out == "ok"
    assert slept == [7.0]  # retry_after wins over base_delay backoff


@pytest.mark.asyncio
async def test_exponential_backoff_without_hint() -> None:
    slept: list[float] = []

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    async def fn() -> str:
        raise TransientError("flaky")  # no retry_after

    with pytest.raises(TransientError):
        await with_retry(fn, max_attempts=3, base_delay=1.0, jitter=0.0, sleep=record_sleep)
    # attempt 1 -> 1.0, attempt 2 -> 2.0 (no sleep after the final attempt)
    assert slept == [1.0, 2.0]


@pytest.mark.asyncio
async def test_max_attempts_one_disables_retry() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise TransientError("x")

    with pytest.raises(TransientError):
        await with_retry(fn, max_attempts=1, sleep=_noop_sleep)
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_max_attempts_rejected() -> None:
    async def fn() -> str:
        return "x"

    with pytest.raises(ValueError):
        await with_retry(fn, max_attempts=0, sleep=_noop_sleep)


# ── HTTP mapping (deterministic exception → response) ────────────────


def _handler_app():
    """Minimal FastAPI app with the Bifrost error handler and routes that
    raise each error kind. No auth middleware — pure handler test."""
    from fastapi import FastAPI

    from bifrost.api.error_handlers import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/not-found")
    async def _nf() -> dict:
        raise NotFoundError("missing thing")

    @app.get("/rate-limited")
    async def _rl() -> dict:
        raise RateLimitError("slow down", retry_after=12.0)

    @app.get("/conflict")
    async def _cf() -> dict:
        raise ConflictError("clobbered")

    return app


def test_handler_maps_status_and_body() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_handler_app())

    r = client.get("/not-found")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "resource.not_found"
    assert body["error"]["message"] == "missing thing"
    assert body["error"]["retryable"] is False


def test_handler_emits_retry_after_header() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_handler_app())

    r = client.get("/rate-limited")
    assert r.status_code == 429
    assert r.json()["error"]["retryable"] is True
    assert r.headers["Retry-After"] == "12"  # integer seconds


def test_handler_maps_conflict_to_409() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_handler_app())

    r = client.get("/conflict")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "state.conflict"
