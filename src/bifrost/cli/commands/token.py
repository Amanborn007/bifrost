"""``bifrost token`` — mint API tokens for non-browser clients."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer

from bifrost.api.auth.tokens import generate_token

token_app = typer.Typer(help="API tokens for CLI and CI clients.")


class TokenRole(StrEnum):
    member = "member"
    admin = "admin"


@token_app.command("new")
def token_new(
    name: Annotated[
        str, typer.Option("--name", "-n", help="Name recorded on the token's principal")
    ] = "cli",
    role: Annotated[TokenRole, typer.Option("--role", help="Role the token carries")] = (
        TokenRole.member
    ),
) -> None:
    """Print a new token once, with the config entry that authorises it."""
    token, digest = generate_token()
    typer.echo(f"Token (shown once, store it now): {token}")
    typer.echo(f"sha256: {digest}")
    typer.echo("")
    typer.echo("Add this to bifrost.yaml on the server, then restart it:")
    typer.echo("api_tokens:")
    typer.echo(f"  - name: {name}")
    typer.echo(f"    sha256: {digest}")
    typer.echo(f"    roles: [{role.value}]")
    typer.echo("")
    typer.echo("Clients send it as BIFROST_TOKEN (Authorization: Bearer).")
