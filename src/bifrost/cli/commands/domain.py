"""Domain management CLI commands — thin API consumers."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from bifrost.cli.commands._http import get_client, unwrap

domain_app = typer.Typer(help="Domain management.")
console = Console()


@domain_app.command("create")
def domain_create(
    name: str = typer.Argument(..., help="Domain name"),
    description: str | None = typer.Option(None, "--description", "-d", help="Domain description"),
) -> None:
    """Create a new domain."""
    payload: dict[str, object] = {"name": name}
    if description:
        payload["description"] = description

    with get_client() as client:
        resp = client.post("/domains", json=payload)
    data = unwrap(resp)
    console.print(f"[green]Created[/green] domain {data['urn']} (id: {data['id']})")


@domain_app.command("list")
def domain_list() -> None:
    """List all domains."""
    with get_client() as client:
        resp = client.get("/domains")
    domains = unwrap(resp)

    if not domains:
        console.print("[dim]No domains created.[/dim]")
        return

    table = Table(title="Domains")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Repos", justify="right")

    for d in domains:
        table.add_row(d["id"][:8], d["name"], str(d.get("repo_count", 0)))
    console.print(table)


@domain_app.command("info")
def domain_info(
    domain_id: str = typer.Argument(..., help="Domain ID"),
) -> None:
    """Show domain details."""
    with get_client() as client:
        resp = client.get(f"/domains/{domain_id}")
    data = unwrap(resp)

    console.print(f"[bold]{data['name']}[/bold]  (id: {data['id']})")
    if data.get("description"):
        console.print(f"  {data['description']}")
    console.print(f"  Cross-repo flows: {data.get('cross_repo_flow_count', 0)}")

    repos = data.get("repos", [])
    if repos:
        table = Table(title="Repos in Domain")
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name")
        table.add_column("Freshness")
        for r in repos:
            table.add_row(r["id"][:8], r["name"], r.get("freshness_state", "—"))
        console.print(table)
    else:
        console.print("  [dim]No repos in this domain.[/dim]")
