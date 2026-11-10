"""Verify Claude Code CLI is logged in before running extraction.

Invokes `claude auth status` and parses the JSON. Raises with a clear message
if the machine isn't authenticated — the operator needs to run `claude auth login`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from bifrost.cli.config import BifrostSettings, get_settings

logger = logging.getLogger(__name__)


class ClaudeCliNotAuthenticated(RuntimeError):
    """Raised when `claude auth status` reports the user is not logged in."""


@dataclass(frozen=True)
class ClaudeAuthStatus:
    logged_in: bool
    email: str
    org_name: str
    subscription_type: str  # e.g. "enterprise", "team", "pro"
    auth_method: str  # e.g. "claude.ai", "api_key"
    api_provider: str  # e.g. "firstParty", "bedrock", "vertex"


async def verify_claude_cli_auth(
    settings: BifrostSettings | None = None,
    *,
    timeout_seconds: float = 10.0,
) -> ClaudeAuthStatus:
    """Return auth status. Raises ClaudeCliNotAuthenticated if not logged in."""
    settings = settings or get_settings()
    proc = await asyncio.create_subprocess_exec(
        settings.claude_cli_path,
        "auth",
        "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ClaudeCliNotAuthenticated(
            f"`claude auth status` timed out after {timeout_seconds}s — "
            f"is the CLI installed at {settings.claude_cli_path!r}?"
        ) from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise ClaudeCliNotAuthenticated(
            f"`claude auth status` exited {proc.returncode}. "
            f"Run `claude auth login` to authenticate.\nstderr: {stderr.strip()[:500]}"
        )

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliNotAuthenticated(
            f"`claude auth status` did not produce JSON: {stdout[:300]}"
        ) from exc

    if not data.get("loggedIn", False):
        raise ClaudeCliNotAuthenticated(
            "Claude Code CLI is not logged in. Run `claude auth login` to authenticate "
            "with your Claude account."
        )

    return ClaudeAuthStatus(
        logged_in=True,
        email=str(data.get("email") or ""),
        org_name=str(data.get("orgName") or ""),
        subscription_type=str(data.get("subscriptionType") or ""),
        auth_method=str(data.get("authMethod") or ""),
        api_provider=str(data.get("apiProvider") or ""),
    )
