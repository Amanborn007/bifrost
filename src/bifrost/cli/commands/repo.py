"""Repo management CLI commands — thin API consumers."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from bifrost.cli.commands._http import get_client, unwrap

repo_app = typer.Typer(help="Repository management.")
console = Console()


@repo_app.command("add")
def repo_add(
    url: str = typer.Argument(..., help="Repository URL to register"),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Display name (defaults to URL basename)"
    ),
    domain_id: str | None = typer.Option(
        None, "--domain", "-d", help="Domain ID to associate with"
    ),
) -> None:
    """Register a new repository."""
    payload: dict[str, object] = {"url": url}
    if name:
        payload["name"] = name
    else:
        # Derive name from URL
        payload["name"] = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    if domain_id:
        payload["domain_id"] = domain_id

    with get_client() as client:
        resp = client.post("/repos", json=payload)
    data = unwrap(resp)
    console.print(f"[green]Registered[/green] {data['urn']} (id: {data['id']})")


@repo_app.command("list")
def repo_list() -> None:
    """List registered repositories."""
    with get_client() as client:
        resp = client.get("/repos")
    repos = unwrap(resp)

    if not repos:
        console.print("[dim]No repositories registered.[/dim]")
        return

    table = Table(title="Repositories")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("URL")
    table.add_column("Freshness")
    table.add_column("Index Status")

    for r in repos:
        table.add_row(
            r["id"][:8],
            r["name"],
            r["url"],
            r.get("freshness_state", "—"),
            r.get("index_status", "—"),
        )
    console.print(table)


@repo_app.command("remove")
def repo_remove(
    repo_id: str = typer.Argument(..., help="Repository ID to deregister"),
) -> None:
    """Deregister a repository."""
    with get_client() as client:
        resp = client.delete(f"/repos/{repo_id}")
    data = unwrap(resp)
    console.print(f"[yellow]Deregistered[/yellow] {data['id']}")


@repo_app.command("status")
def repo_status(
    repo_id: str = typer.Argument(..., help="Repository ID"),
) -> None:
    """Show repository detail and freshness."""
    with get_client() as client:
        resp = client.get(f"/repos/{repo_id}")
    data = unwrap(resp)

    console.print(f"[bold]{data['name']}[/bold]  ({data['urn']})")
    console.print(f"  URL: {data['url']}")
    if data.get("domain_id"):
        console.print(f"  Domain: {data['domain_id']}")

    freshness = data.get("freshness", {})
    console.print(f"  Freshness: {freshness.get('state', '—')}")
    if freshness.get("last_indexed_at"):
        console.print(f"  Last indexed: {freshness['last_indexed_at']}")
    if freshness.get("pending_commits"):
        console.print(f"  Pending commits: {freshness['pending_commits']}")

    stats = data.get("stats", {})
    console.print(
        f"  Entities: {stats.get('component_count', 0)} components, "
        f"{stats.get('flow_count', 0)} flows, "
        f"{stats.get('dependency_count', 0)} dependencies"
    )
