"""Health check route handler per TRD §4.7."""

from __future__ import annotations

import time

from fastapi import APIRouter

from bifrost.api.deps import Settings
from bifrost.api.schemas.health import CheckStatus, HealthChecks, HealthResponse
from bifrost.extraction.cli_auth import (
    ClaudeCliNotAuthenticated,
    verify_claude_cli_auth,
)

router = APIRouter(tags=["health"])

_start_time = time.monotonic()


@router.get("/health/detailed", response_model=HealthResponse)
async def health_check(settings: Settings) -> HealthResponse:
    """Detailed health check with component status.

    The simple /health endpoint is defined in app.py.
    This provides the full TRD §4.7 response with checks.
    """
    uptime = time.monotonic() - _start_time

    # V1: Return healthy status with placeholder latencies.
    # Real health checks (DB ping, Redis ping) will be wired in integration.
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        track=settings.track,
        checks=HealthChecks(
            database=CheckStatus(status="healthy", latency_ms=0.0),
            redis=CheckStatus(status="healthy", latency_ms=0.0),
            adapters={},
        ),
        uptime_seconds=round(uptime, 2),
    )


@router.get("/health/claude-cli")
async def claude_cli_status(settings: Settings) -> dict:
    """Report whether the Claude Code CLI is logged in and usable.

    Returns `{"ok": true, "email": ..., "org_name": ..., "subscription_type": ...}`
    when authenticated, `{"ok": false, "error": ...}` otherwise.
    Never 500s — the error path is a 200 with ok=false so the UI can show a banner.
    """
    try:
        status = await verify_claude_cli_auth(settings)
    except ClaudeCliNotAuthenticated as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Run `claude auth login` in a terminal and reload.",
        }
    return {
        "ok": True,
        "email": status.email,
        "org_name": status.org_name,
        "subscription_type": status.subscription_type,
        "auth_method": status.auth_method,
        "api_provider": status.api_provider,
        "triage_model": settings.triage_model,
        "extraction_model": settings.extraction_model,
        "agent_model": settings.agent_model,
        "reasoning_model": settings.reasoning_model,
    }
