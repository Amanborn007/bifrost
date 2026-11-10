"""Post an escalated doubt's draft to Slack through the Slack Web API.

No LLM, no subprocess, no MCP: the body goes verbatim to the one configured
channel, so nothing inside the body can redirect the post. Opt-in: needs both
``slack_escalation_channel`` and ``slack_bot_token`` (``BIFROST_SLACK_BOT_TOKEN``).
Each Slack call is tried twice, then ``SlackEscalationError`` is raised.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.core.http import http_timeout
from bifrost.errors import ConfigError, UpstreamError

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"
_BODY_CHAR_CAP = 10_000
_RETRY_DELAY_S = 0.5


class SlackEscalationError(UpstreamError):
    """Slack rejected or never answered the escalation post (or its permalink lookup)."""

    code = "slack.escalation.post_failed"
    retryable = False
    http_status = 502


class _AttemptFailed(Exception):
    pass


def slack_escalation_enabled(settings: BifrostSettings | None = None) -> bool:
    """True only when both the channel and the bot token are configured."""
    settings = settings or get_settings()
    channel = (settings.slack_escalation_channel or "").strip()
    token = settings.slack_bot_token.get_secret_value().strip() if settings.slack_bot_token else ""
    return bool(channel and token)


async def _slack_call(
    client: httpx.AsyncClient, token: str, verb: str, method: str, **kwargs: Any
) -> dict[str, Any]:
    try:
        resp = await client.request(
            verb, f"{_SLACK_API}/{method}", headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _AttemptFailed(f"{method}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        error = data.get("error", "unknown") if isinstance(data, dict) else "non-object response"
        raise _AttemptFailed(f"{method}: slack error {error!r}")
    return data


async def _twice[T](label: str, op: Callable[[], Awaitable[T]]) -> T:
    try:
        return await op()
    except _AttemptFailed as first:
        logger.warning("Slack %s failed (%s); retrying once", label, first)
    await asyncio.sleep(_RETRY_DELAY_S)
    return await op()


async def post_escalation_to_slack(
    *,
    channel: str,
    body_md: str,
    settings: BifrostSettings | None = None,
) -> str:
    """Post ``body_md`` to ``channel`` and return the message permalink.

    Raises ``ConfigError`` when the channel or token is unset and
    ``SlackEscalationError`` when Slack fails twice in a row.
    """
    settings = settings or get_settings()
    channel = (channel or "").strip()
    if not channel:
        raise ConfigError("Slack escalation needs a channel (slack_escalation_channel)")
    if settings.slack_bot_token is None or not settings.slack_bot_token.get_secret_value().strip():
        raise ConfigError("Slack escalation needs BIFROST_SLACK_BOT_TOKEN")
    token = settings.slack_bot_token.get_secret_value().strip()

    text = body_md or ""
    if len(text) > _BODY_CHAR_CAP:
        logger.warning(
            "Slack escalation body truncated from %d to %d chars", len(text), _BODY_CHAR_CAP
        )
        text = text[:_BODY_CHAR_CAP]

    async with httpx.AsyncClient(timeout=http_timeout(settings)) as client:
        try:
            posted = await _twice(
                "chat.postMessage",
                lambda: _slack_call(
                    client,
                    token,
                    "POST",
                    "chat.postMessage",
                    json={"channel": channel, "text": text},
                ),
            )
        except _AttemptFailed as exc:
            raise SlackEscalationError(f"Slack post to {channel} failed: {exc}") from exc

        try:
            link = await _twice(
                "chat.getPermalink",
                lambda: _slack_call(
                    client,
                    token,
                    "GET",
                    "chat.getPermalink",
                    params={"channel": posted["channel"], "message_ts": posted["ts"]},
                ),
            )
            return str(link["permalink"])
        except (_AttemptFailed, KeyError) as exc:
            raise SlackEscalationError(
                f"Slack message was posted to {channel} but its permalink lookup failed: {exc!r}",
                code="slack.escalation.permalink_failed",
                details={"posted": True},
            ) from exc
