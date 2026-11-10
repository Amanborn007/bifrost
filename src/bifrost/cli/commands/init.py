"""`bifrost init` — first-run setup helper.

Specified in docs/archive/specs/e2e-user-story.md Step 1 as the initial CLI command
the operator runs after install. Writes a starter ~/.bifrost/config.yaml,
verifies the `claude` CLI is reachable (since all LLM calls flow through
the Claude Code subprocess), and pings the API server if one is running.

V1 deliberately does NOT run database migrations from this command:
migrations require DB connectivity and live behind alembic / make migrate.
Use `make up && make migrate` for that.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bifrost.cli.config import get_settings
from bifrost.core.http import http_timeout

console = Console()
err_console = Console(stderr=True)

DEFAULT_CONFIG_TEMPLATE = """\
# Bifrost local config — written by `bifrost init` on first run.
#
# Bifrost settings are read from environment variables (BIFROST_* prefix)
# or from a .env file in the working directory. This file is informational
# only: it documents the most common knobs without committing values.
#
# To override any of these, set the corresponding env var in your shell or
# in a project-local .env (see RUNNING.md).

# Track A (TypeScript + Postgres) is V1. Track B (Managed Agents) is gated
# behind a Research Preview waitlist — leave on A unless you have access.
# BIFROST_TRACK=A

# Postgres connection (matches docker-compose default).
# BIFROST_DATABASE_URL=postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost

# API server URL (the CLI talks to this).
# BIFROST_API_BASE_URL=http://localhost:8000

# Demo mode: when on, requests with no operator identity resolve to demo@example.com.
# Set to false on anything reachable beyond your laptop.
# BIFROST_DEMO_MODE=false
"""


def init_command(
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing ~/.bifrost/config.yaml"
    ),
    skip_api_check: bool = typer.Option(
        False, "--skip-api-check", help="Don't try to reach the API server"
    ),
) -> None:
    """Initialize Bifrost on this machine.

    Creates ~/.bifrost/ if missing, writes a starter config.yaml (commented
    defaults only — env vars take precedence), verifies the `claude` CLI is
    installed, and pings the API server.
    """
    settings = get_settings()
    rows: list[tuple[str, str, str]] = []

    # 1. ~/.bifrost/ + config.yaml
    bifrost_dir = Path.home() / ".bifrost"
    bifrost_dir.mkdir(mode=0o700, exist_ok=True)
    rows.append(("config dir", str(bifrost_dir), "ok"))

    config_path = bifrost_dir / "config.yaml"
    if config_path.exists() and not force:
        rows.append(("config.yaml", str(config_path), "exists (use --force to overwrite)"))
    else:
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)
        config_path.chmod(0o600)
        rows.append(("config.yaml", str(config_path), "written"))

    # 2. claude CLI presence
    claude_path = shutil.which(settings.claude_cli_path)
    if claude_path is None:
        rows.append(
            (
                "claude CLI",
                settings.claude_cli_path,
                "NOT FOUND — install Claude Code: https://claude.com/claude-code",
            )
        )
    else:
        version = _safe_version(claude_path)
        rows.append(("claude CLI", claude_path, version))

    # 3. API server reachability (optional)
    if skip_api_check:
        rows.append(("api server", settings.api_base_url, "skipped"))
    else:
        rows.append(("api server", settings.api_base_url, _ping_api(settings.api_base_url)))

    table = Table(title="bifrost init", show_lines=False)
    table.add_column("check")
    table.add_column("target")
    table.add_column("status")
    healthy_markers = {"ok", "written", "reachable"}
    warn_markers = ("exists", "skipped", "reachable (status")
    for name, target, status in rows:
        if status in healthy_markers or "Claude Code" in status or status.endswith("ok"):
            style = "green"
        elif any(m in status for m in warn_markers):
            style = "yellow"
        else:
            style = "red"
        table.add_row(name, target, f"[{style}]{status}[/{style}]")
    console.print(table)

    console.print(
        Panel(
            "Next steps:\n"
            "  - Bring up Postgres + API:   make up\n"
            "  - Run migrations:            make migrate\n"
            "  - Register your first repo:  bifrost repo add <git-url>\n"
            "  - Or open the UI:            http://localhost:5173",
            title="Next",
            border_style="cyan",
        )
    )


def _safe_version(claude_path: str) -> str:
    try:
        result = subprocess.run(
            [claude_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = (result.stdout or result.stderr).strip().splitlines()
        return out[0] if out else "ok"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"could not run --version: {exc}"


def _ping_api(base_url: str) -> str:
    try:
        with httpx.Client(timeout=http_timeout(cap=2.0)) as client:
            resp = client.get(f"{base_url}/api/v1/health")
        if resp.status_code == 200:
            return "reachable"
        return f"reachable (status {resp.status_code})"
    except httpx.HTTPError:
        return "not reachable (run `make up` to start it)"
