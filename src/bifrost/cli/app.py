"""Typer CLI application — entry point for `bifrost` command.

Command groups are thin API consumers (httpx + rich). The `extract-local`
command retains the direct extraction pipeline for development use.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid

import typer

from bifrost.cli.commands import domain_app, extract_app, query_app, repo_app, token_app
from bifrost.cli.commands.init import init_command
from bifrost.cli.commands.query import query_ask

app = typer.Typer(
    name="bifrost",
    help="Bifrost engineering knowledge platform CLI.",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)


# ── Global options ────────────────────────────────────────────────────


@app.callback()
def main(
    output: str = typer.Option("human", "-o", "--output", help="Output format: human, json, yaml"),
    server: str | None = typer.Option(None, "-s", "--server", help="API server URL"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
) -> None:
    """Bifrost engineering knowledge platform CLI."""
    # Same structlog/stdlib pipeline as the server. On a TTY the default
    # "auto" format renders human-readable console output, so CLI UX is
    # unchanged; --verbose just drops the level to DEBUG.
    from bifrost.cli.config import validate_startup
    from bifrost.logging_config import configure_logging

    configure_logging(validate_startup())
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


# ── Register command groups from commands package ─────────────────────

app.add_typer(repo_app, name="repo")
app.add_typer(domain_app, name="domain")
app.add_typer(query_app, name="query")
app.add_typer(extract_app, name="extract")
app.add_typer(token_app, name="token")

# Top-level `bifrost init` — first-run setup (spec'd in e2e-user-story.md Step 1).
app.command("init", help="Initialize Bifrost on this machine (writes ~/.bifrost/config.yaml).")(
    init_command
)

# Top-level `bifrost ask "..."` alias for `bifrost query ask` — spec'd in
# docs/archive/specs/e2e-user-story.md and docs/archive/specs/interfaces.md.
app.command("ask", help="Ask a question about the codebase (alias for `query ask`).")(query_ask)


# ── Local extract command (development/direct mode) ─────────────────

local_extract_app = typer.Typer(
    help="Local extraction (direct, no API server needed).", hidden=True
)
app.add_typer(local_extract_app, name="extract-local")

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_BUDGET_EXCEEDED = 2


@local_extract_app.command("run")
def extract_local_run(
    repo_url: str = typer.Argument(..., help="Repository URL to extract from"),
    shallow: bool = typer.Option(True, "--shallow/--no-shallow", help="Use shallow clone"),
    budget: float | None = typer.Option(None, "--budget", help="Max USD cost for extraction"),
    resume: bool = typer.Option(False, "--resume", help="Resume from checkpoint"),
    force: bool = typer.Option(False, "--force", help="Ignore change detection, re-extract all"),
) -> None:
    """Run extraction pipeline locally (no API server required)."""
    try:
        asyncio.run(
            _run_extraction(
                repo_url=repo_url,
                shallow=shallow,
                budget=budget,
                resume=resume,
                force=force,
            )
        )
    except SystemExit:
        raise
    except Exception as exc:
        typer.echo(f"Extraction failed: {exc}", err=True)
        raise typer.Exit(code=EXIT_FAILURE) from None


async def _run_extraction(
    *,
    repo_url: str,
    shallow: bool,
    budget: float | None,
    resume: bool,
    force: bool,
) -> None:
    """Async core of the local extract command."""
    from bifrost.adapters.git import GitAdapter
    from bifrost.cli.config import get_settings
    from bifrost.extraction.cli_client import ClaudeLlmClient
    from bifrost.extraction.cost import BudgetExceededError, CostTracker
    from bifrost.extraction.pipeline import ExtractionPipeline
    from bifrost.extraction.secrets import SecretScanner

    settings = get_settings()
    task_id = f"extract-{uuid.uuid4().hex[:8]}"

    typer.echo(f"Starting extraction: {repo_url}")
    typer.echo(f"Task ID: {task_id}")
    if budget is not None:
        typer.echo(f"Budget: ${budget:.2f}")

    # 1. Clone repository
    adapter = GitAdapter()
    clone_dir = tempfile.mkdtemp(prefix="bifrost-extract-")
    typer.echo(f"Cloning to {clone_dir}...")
    await adapter.clone(repo_url, clone_dir, shallow=shallow)

    # 2. List files
    file_paths = await adapter.list_files(clone_dir)
    typer.echo(f"Found {len(file_paths)} tracked files")

    # 3. Set up cost tracker and secret scanner
    cost_tracker = CostTracker(task_id=task_id, budget=budget)
    scanner = SecretScanner()

    # 4. Build read_file with secret scanning
    async def safe_read_file(path: str) -> str:
        content = await adapter.read_file(clone_dir, path)
        result = scanner.scan_and_redact(content, file_path=path)
        if result.has_redactions:
            typer.echo(f"  Redacted {len(result.redactions)} secret(s) in {path}")
        return result.redacted_content

    # 5. Build file previews for triage
    typer.echo("Building file previews...")
    previews: dict[str, str] = {}
    for fp in file_paths:
        try:
            content = await adapter.read_file(clone_dir, fp)
            previews[fp] = content[:200]
        except Exception:
            pass  # skip files that can't be read

    # 6. Run extraction
    client = ClaudeLlmClient(settings)
    pipeline = ExtractionPipeline(
        client=client,
        repo_id=task_id,
        triage_model=settings.triage_model,
        extraction_model=settings.extraction_model,
    )

    def _phase_msg(phase: str) -> str:
        return f"  Phase {phase} complete"

    async def _on_phase(phase: str) -> None:
        typer.echo(_phase_msg(phase))

    pipeline._on_phase_complete = _on_phase

    typer.echo("Running extraction pipeline...")
    try:
        output = await pipeline.run(
            file_paths=file_paths,
            read_file=safe_read_file,
            file_previews=previews,
        )
    except BudgetExceededError as exc:
        typer.echo(f"Budget exceeded: {exc}", err=True)
        raise typer.Exit(code=EXIT_BUDGET_EXCEEDED) from None

    # 7. Report results
    typer.echo("")
    typer.echo("Extraction complete:")
    typer.echo(f"  Components: {len(output.components)}")
    typer.echo(f"  Flows: {len(output.flows)}")
    typer.echo(f"  Dependencies: {len(output.dependencies)}")
    typer.echo(f"  Files analyzed: {output.files_analyzed}")
    if output.errors:
        typer.echo(f"  Errors: {len(output.errors)}")
        for err in output.errors[:5]:
            typer.echo(f"    - {err}")
    typer.echo(f"  Cost: ${cost_tracker.total_cost:.4f}")
