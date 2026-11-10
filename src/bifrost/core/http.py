"""One timeout policy for every outbound HTTP client."""

from __future__ import annotations

import httpx

from bifrost.cli.config import BifrostSettings, get_settings


def http_timeout(
    settings: BifrostSettings | None = None, *, cap: float | None = None
) -> httpx.Timeout:
    """Timeout from ``http_timeout_seconds``; ``cap`` can only lower it (quick probes)."""
    seconds = (settings or get_settings()).http_timeout_seconds
    if cap is not None:
        seconds = min(seconds, cap)
    return httpx.Timeout(seconds)
