"""Knowledge query CLI command — submits question via REST API."""

from __future__ import annotations

import json
from typing import Literal

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bifrost.cli.commands._http import get_client, unwrap

query_app = typer.Typer(help="Query the knowledge graph.")
console = Console()

CONFIDENCE_STYLES: dict[str, str] = {
    "confirmed": "green",
    "inferred": "yellow",
    "uncertain": "yellow",
    "stale": "red",
    "conflicting": "red",
    "unknown": "dim",
}


@query_app.command("ask")
def query_ask(
    question: str = typer.Argument(..., help="Natural language question about the codebase"),
    repo_id: str | None = typer.Option(None, "--repo", "-r", help="Scope query to a repo ID"),
    domain_id: str | None = typer.Option(None, "--domain", "-d", help="Scope query to a domain ID"),
    output_format: Literal["text", "json"] = typer.Option(
        "text", "--format", "-f", help="Output format"
    ),
) -> None:
    """Ask a question about the codebase."""
    payload: dict[str, object] = {"question": question}
    if repo_id:
        payload["repo_ids"] = [repo_id]
    if domain_id:
        payload["domain_id"] = domain_id

    with get_client() as client:
        resp = client.post("/query", json=payload)
    data = unwrap(resp)

    if output_format == "json":
        typer.echo(json.dumps(data, indent=2, default=str))
        return

    _render_answer(data)


def _render_answer(data: dict[str, object]) -> None:
    """Render a structured answer with confidence badge and sources."""
    confidence = str(data.get("confidence", "unknown"))
    style = CONFIDENCE_STYLES.get(confidence, "dim")

    # Answer panel with confidence badge
    answer_text = str(data.get("answer", ""))
    console.print(
        Panel(
            answer_text,
            title=f"Answer  [{style}]({confidence})[/{style}]",
            border_style=style,
        )
    )

    # Sources
    sources = data.get("sources", [])
    if sources and isinstance(sources, list):
        table = Table(title="Sources")
        table.add_column("URN", style="cyan")
        table.add_column("Name")
        table.add_column("Type", style="dim")
        table.add_column("Relevance", justify="right")
        for s in sources:
            if isinstance(s, dict):
                table.add_row(
                    str(s.get("urn", "")),
                    str(s.get("name", "")),
                    str(s.get("entity_type", "—")),
                    f"{s['relevance']:.0%}" if s.get("relevance") else "—",
                )
        console.print(table)

    # Gaps
    gaps = data.get("gaps", [])
    if gaps and isinstance(gaps, list):
        console.print("[yellow]Gaps:[/yellow]")
        for g in gaps:
            console.print(f"  - {g}")

    # Conflicts
    conflicts = data.get("conflicts", [])
    if conflicts and isinstance(conflicts, list):
        console.print("[red]Conflicts:[/red]")
        for c in conflicts:
            console.print(f"  - {c}")

    # Task ID for follow-up
    task_id = data.get("task_id")
    if task_id:
        console.print(f"\n[dim]Task: {task_id}[/dim]")
