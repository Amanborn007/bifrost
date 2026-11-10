"""Shared httpx client for CLI commands."""

from __future__ import annotations

from typing import Any

import httpx
import typer

from bifrost.cli.config import get_settings
from bifrost.core.http import http_timeout


def get_client() -> httpx.Client:
    """Create an httpx client pointing at the configured API server."""
    settings = get_settings()
    base = settings.api_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.token}"} if settings.token else {}
    return httpx.Client(base_url=f"{base}/api/v1", timeout=http_timeout(settings), headers=headers)


def unwrap(response: httpx.Response) -> Any:
    """Unwrap a BifrostEnvelope response, handling errors.

    Returns the `data` field from the envelope (may be dict, list, or other).
    Raises typer.Exit on HTTP errors with user-friendly messages.
    """
    try:
        response.raise_for_status()
    except httpx.ConnectError:
        typer.echo(
            "Connection refused — is the Bifrost server running? "
            f"(tried {get_settings().api_base_url})",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPStatusError as exc:
        _handle_http_error(exc)

    body = response.json()
    return body.get("data", body)


def _handle_http_error(exc: httpx.HTTPStatusError) -> None:
    """Convert HTTP errors to user-friendly CLI messages."""
    status = exc.response.status_code
    try:
        body = exc.response.json()
        error = body.get("error", {})
        msg = error.get("message", exc.response.text)
    except Exception:
        msg = exc.response.text

    if status == 404:
        typer.echo(f"Not found: {msg}", err=True)
    elif status == 422:
        typer.echo(f"Invalid request: {msg}", err=True)
    elif status >= 500:
        typer.echo(f"Server error ({status}): {msg}", err=True)
    else:
        typer.echo(f"HTTP {status}: {msg}", err=True)
    raise typer.Exit(code=1) from None
