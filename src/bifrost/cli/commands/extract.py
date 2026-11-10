"""Extraction CLI command — triggers extraction via REST API and streams SSE progress."""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from bifrost.cli.commands._http import get_client, unwrap
from bifrost.cli.config import get_settings
from bifrost.core.http import http_timeout

extract_app = typer.Typer(help="Trigger extraction pipelines.")
console = Console()
err_console = Console(stderr=True)


@extract_app.command("run")
def extract_run(
    repo_id: str = typer.Argument(..., help="Repository ID to extract from"),
    force: bool = typer.Option(False, "--force", help="Ignore change detection, re-extract all"),
) -> None:
    """Trigger extraction for a repository and stream progress."""
    # 1. Trigger extraction via API
    payload: dict[str, object] = {"force_full": force}
    with get_client() as client:
        resp = client.post(f"/repos/{repo_id}/extract", json=payload)
    data = unwrap(resp)
    task_id: str = data["task_id"]
    console.print(f"Extraction queued — task [bold]{task_id}[/bold]")

    # 2. Stream SSE progress
    _stream_progress(task_id)


@extract_app.command("status")
def extract_status(
    task_id: str = typer.Argument(..., help="Task ID to check"),
) -> None:
    """Stream live progress for an extraction task."""
    _stream_progress(task_id)


def _stream_progress(task_id: str) -> None:
    """Connect to SSE stream and display live progress."""
    settings = get_settings()
    url = f"{settings.api_base_url}/tasks/{task_id}/stream"

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    )
    task = progress.add_task("Extracting...", total=100)
    failed = False

    try:
        with (
            httpx.Client(timeout=http_timeout(settings)) as client,
            client.stream("GET", url, headers={"Accept": "text/event-stream"}) as stream,
            Live(progress, console=console, refresh_per_second=4),
        ):
            for event_type, event_data in _parse_sse(stream):
                _handle_event(progress, task, event_type, event_data)
                failed = failed or event_type == "task_failed"
    except httpx.ConnectError:
        err_console.print(
            "[red]Connection refused[/red] — is the Bifrost server running? "
            f"(tried {settings.api_base_url})"
        )
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        console.print("\n[yellow]Stream interrupted.[/yellow]")
    if failed:
        raise typer.Exit(code=1)


def _parse_sse(
    stream: httpx.Response,
) -> Generator[tuple[str, dict[str, Any]], None, None]:
    """Parse SSE event stream, yielding (event_type, data) pairs."""
    current_event = ""
    current_data = ""

    for line in stream.iter_lines():
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            current_data = line[6:]
        elif line == "":
            # End of event frame
            if current_event and current_data:
                try:
                    parsed = json.loads(current_data)
                except json.JSONDecodeError:
                    parsed = {"raw": current_data}
                yield current_event, parsed
            current_event = ""
            current_data = ""


def _handle_event(
    progress: Progress,
    task_id: TaskID,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Update progress display based on SSE event emitted by extraction worker."""
    if event_type == "extraction_progress":
        pct = int(data.get("progress", 0))
        phase = data.get("phase", "")
        progress.update(task_id, completed=pct, description=f"{phase}")

    elif event_type == "phase_complete":
        phase = data.get("phase", "?")
        progress.update(task_id, description=f"Phase {phase} complete")

    elif event_type == "consultation.started":
        agent = data.get("agent_id", "agent")
        repo = data.get("repo_id", "")
        progress.update(task_id, description=f"Consulting {agent} on {repo}...")

    elif event_type == "cost_update":
        cost = data.get("cost_usd") or data.get("estimated_cost_usd") or data.get("total_cost_usd")
        if cost is not None:
            progress.update(task_id, description=f"Cost so far: ${float(cost):.2f}")

    elif event_type == "heartbeat":
        return

    elif event_type == "task_complete":
        progress.update(
            task_id,
            description="[green]Complete[/green]",
            completed=100,
        )
        partial = data.get("partial", False)
        errors = data.get("errors", 0)
        border = "yellow" if partial or errors else "green"
        title = "Extraction Result (partial)" if partial else "Extraction Result"
        lines = [
            f"Components:   {data.get('components', '-')}",
            f"Flows:        {data.get('flows', '-')}",
            f"Dependencies: {data.get('dependencies', '-')}",
        ]
        if errors:
            lines.append(f"Errors:       {errors}")
        console.print(Panel("\n".join(lines), title=title, border_style=border))

    elif event_type == "task_failed":
        err = data.get("error", "unknown error")
        progress.update(task_id, description=f"[red]Failed[/red]: {err}")
        console.print(Panel(str(err), title="Extraction Failed", border_style="red"))
