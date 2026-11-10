"""Central error taxonomy + the one retry primitive.

Before this module, ~25 exception classes lived in their own corners with no
common base, no machine-readable ``code``, and no agreed answer to the single
question every caller actually asks: *"is this worth retrying?"*. Retry logic
was hand-rolled in two places (Jira 429 loop, CLI max-turns loop) with
different shapes, and uncaught domain errors fell through to FastAPI's generic
500 regardless of whether they were a 404, a 429, or a config bug.

``BifrostError`` fixes that with three pieces of metadata carried on the
exception itself:

  * ``code`` — a stable dotted string for logs/metrics (``adapter.jira.rate_limit``).
  * ``retryable`` — the canonical "should ``with_retry`` try again?" flag.
  * ``http_status`` — deterministic exception→HTTP mapping (see
    ``bifrost.api.error_handlers``); no more accidental 500s.

Plus ``retry_after`` (honored by ``with_retry`` and emitted as a ``Retry-After``
header) and a free-form ``details`` dict for structured context.

This module deliberately has **no FastAPI / SQLAlchemy import** so the adapter
and extraction layers can raise/catch these without dragging the web stack in.
The HTTP mapping lives in ``bifrost.api.error_handlers``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


# ── Base ─────────────────────────────────────────────────────────────


class BifrostError(Exception):
    """Base for every Bifrost domain error.

    Subclasses set the class-level defaults (``code``/``retryable``/
    ``http_status``); any of them can also be overridden per-instance via
    keyword args, which is handy for one-off raises that don't deserve a
    dedicated subclass.
    """

    # Class-level defaults — overridable per instance.
    code: str = "bifrost.error"
    retryable: bool = False
    http_status: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        if http_status is not None:
            self.http_status = http_status
        # Per-instance only (a hint, e.g. from a Retry-After header).
        self.retry_after = retry_after
        self.details: dict[str, object] = details or {}


# ── Canonical subclasses ─────────────────────────────────────────────
# Each fixes a (code, retryable, http_status) triple so call sites raise
# intent, not bookkeeping. Adapter/extraction base errors re-parent onto
# these (see adapters/*.py, extraction/cli_client.py).


class ConfigError(BifrostError):
    """Misconfiguration — fail fast and loud, never retry."""

    code = "config.invalid"
    retryable = False
    http_status = 500


class ValidationError(BifrostError):
    """Caller supplied bad input. 400, not retryable."""

    code = "request.invalid"
    retryable = False
    http_status = 400


class AuthError(BifrostError):
    """Authentication / authorization failure. Not retryable."""

    code = "auth.denied"
    retryable = False
    http_status = 401


class NotFoundError(BifrostError):
    """Requested resource does not exist. Not retryable."""

    code = "resource.not_found"
    retryable = False
    http_status = 404


class ConflictError(BifrostError):
    """Optimistic-concurrency / state conflict. 409.

    Not auto-retryable: a blind retry just re-conflicts. The caller must
    re-read and re-decide (read-modify-write), so ``with_retry`` leaves it
    alone by default.
    """

    code = "state.conflict"
    retryable = False
    http_status = 409


class RateLimitError(BifrostError):
    """Upstream rate limit (429). Retryable, usually with a ``retry_after``."""

    code = "upstream.rate_limited"
    retryable = True
    http_status = 429


class TransientError(BifrostError):
    """Transient upstream failure (timeout, connection reset, 5xx). Retryable."""

    code = "upstream.transient"
    retryable = True
    http_status = 503


class UpstreamError(BifrostError):
    """Non-retryable upstream failure (bad gateway, unexpected response)."""

    code = "upstream.error"
    retryable = False
    http_status = 502


# ── The one retry primitive ──────────────────────────────────────────


def _default_retryable(exc: BaseException) -> bool:
    """An exception is retryable iff it's a ``BifrostError`` that says so.

    Non-Bifrost exceptions are *not* retried by default — callers that want
    to absorb a raw ``httpx``/``asyncio`` error must opt in via ``retry_on``.
    """
    return isinstance(exc, BifrostError) and exc.retryable


async def with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.1,
    retry_on: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``fn`` and retry it while the raised error is retryable.

    This is the single retry primitive for the codebase — adapters and agents
    should funnel through it rather than hand-rolling loops, so backoff,
    jitter, attempt caps, and ``Retry-After`` honoring all behave identically.

    Args:
        fn: A zero-arg callable returning an awaitable. Re-invoked per attempt
            (so it must be a factory, not a single coroutine — coroutines are
            one-shot).
        max_attempts: Total tries, including the first. ``1`` disables retry.
        base_delay: Backoff base; delay is ``base_delay * 2**(attempt-1)``.
        max_delay: Cap on the computed backoff (before jitter).
        jitter: Fractional jitter added to each delay (``0.1`` → up to +10%),
            to avoid thundering-herd alignment across callers.
        retry_on: Predicate deciding retryability. Defaults to
            :func:`_default_retryable` (``BifrostError.retryable``). Pass a
            custom predicate to absorb raw upstream errors (e.g. ``httpx``).
        sleep: Injectable async sleep — tests pass a no-op to avoid real waits.

    Returns:
        Whatever ``fn`` returns on the first successful attempt.

    Raises:
        The last exception once attempts are exhausted or the error is judged
        non-retryable.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    predicate = retry_on or _default_retryable
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            last_exc = exc
            is_last = attempt >= max_attempts
            if is_last or not predicate(exc):
                raise

            # Prefer an explicit Retry-After hint when the error carries one;
            # otherwise exponential backoff capped at max_delay.
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay += delay * jitter * random.random()

            logger.warning(
                "with_retry: attempt %d/%d failed (%s), retrying in %.2fs",
                attempt,
                max_attempts,
                getattr(exc, "code", type(exc).__name__),
                delay,
            )
            await sleep(delay)

    # Unreachable: the loop either returns or raises. Guard for type-checkers.
    assert last_exc is not None  # noqa: S101
    raise last_exc
