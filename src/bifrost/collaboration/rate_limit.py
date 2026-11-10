"""In-memory per-requester sliding-window rate limiter.

Goal: prevent a runaway client from issuing N concurrent
``handover-docs/.../generate`` calls and burning through the per-call
cost ceiling N times in parallel. We don't need a distributed limiter
here — a single-process Bifrost server is the deployment shape —
so this module keeps timestamps in a process-local dict.

Algorithm: pure sliding window. For each requester we keep a
:class:`collections.deque` of monotonic timestamps of allowed calls
inside the current window. On each check we evict timestamps older
than ``window_seconds`` from the head, compare ``len(deque)`` to the
limit, and either append the current timestamp (allow) or compute
``retry_after`` from the oldest remaining timestamp (deny).

Limitations
-----------
- **Single-process only.** When Bifrost is deployed behind multiple
  uvicorn workers or replicas, each process maintains its own
  counters and the effective limit becomes ``N * configured_limit``.
  This ships single-process; revisit with Redis/cache when we
  horizontally scale.
- **No persistence.** Restart resets all counters. Acceptable: a
  restart is rare and runaway clients reattacking immediately are
  out-of-scope here.
- **No memory bound.** ``dict[str, deque]`` grows with the count of
  distinct requesters. With cookie-session ids this is bounded by
  unique browsers; in practice handover generation is invoked by a
  small set of authenticated humans. If this ever leaks, add a
  periodic sweep keyed by ``len(deque) == 0``.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# Module-level state — singleton per process. The lock guards both
# the dict-of-deques shape and individual deque mutations; contention
# is negligible at handover-generate volumes (~ones per minute).
_buckets: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def check_and_consume(
    requester: str,
    limit: int,
    window_seconds: int = 60,
) -> tuple[bool, int]:
    """Check the requester's bucket and consume one slot if allowed.

    Args:
        requester: Stable identity for the caller. For handover
            generation this is the authenticated principal's email.
        limit: Maximum allowed calls per ``window_seconds``.
        window_seconds: Sliding-window length in seconds. Defaults to
            60 (per-minute limit).

    Returns:
        ``(allowed, retry_after_seconds)``. When ``allowed`` is True
        the call was recorded and ``retry_after_seconds`` is 0. When
        False the call was *not* recorded and ``retry_after_seconds``
        is the number of whole seconds the caller should wait before
        retrying (always >= 1 so clients don't busy-loop).
    """
    if limit <= 0:
        # Disabled limiter: fail open. Keeps tests and dev configs
        # simple — set the env var to 0 to drop the gate entirely.
        return True, 0

    now = time.monotonic()
    cutoff = now - window_seconds

    with _lock:
        bucket = _buckets[requester]
        # Evict expired timestamps from the head.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) < limit:
            bucket.append(now)
            return True, 0

        # Denied. Earliest timestamp determines when the next slot
        # frees up; round up so retry_after >= 1.
        earliest = bucket[0]
        retry_after = max(1, int(window_seconds - (now - earliest)) + 1)
        return False, retry_after


def reset() -> None:
    """Clear all buckets — test-only helper."""
    with _lock:
        _buckets.clear()
