"""Extraction agent — raw Anthropic SDK with tool_use for structured output."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from extraction_probe.prompts import (
    COMPONENT_TOOL,
    FLOW_TOOL,
    build_component_prompt,
    build_flow_prompt,
)
from extraction_probe.validate import (
    ComponentExtractionResult,
    FlowExtractionResult,
    validate_component_result,
    validate_flow_result,
)

EXTRACTION_MODEL = "claude-sonnet-4-6"
MAX_FILE_SIZE = 50_000  # ~50K characters, roughly 50K tokens content limit
MAX_FILES = 50  # Probe cap


@dataclass
class CostTracker:
    """Tracks LLM call costs."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    call_count: int = 0

    # Sonnet 4.6 pricing (per 1M tokens)
    INPUT_COST_PER_M: float = 3.0
    OUTPUT_COST_PER_M: float = 15.0
    CACHE_READ_COST_PER_M: float = 0.30

    def record(self, usage: anthropic.types.Usage) -> None:
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        self.total_cache_read_tokens += cache_read
        self.call_count += 1

    @property
    def total_cost_usd(self) -> float:
        return (
            self.total_input_tokens * self.INPUT_COST_PER_M / 1_000_000
            + self.total_output_tokens * self.OUTPUT_COST_PER_M / 1_000_000
            + self.total_cache_read_tokens * self.CACHE_READ_COST_PER_M / 1_000_000
        )


@dataclass
class ExtractionResult:
    """Aggregated probe results."""

    components: list[ComponentExtractionResult] = field(default_factory=list)
    flows: list[FlowExtractionResult] = field(default_factory=list)
    validation_failures: int = 0
    total_attempts: int = 0
    canary_mismatches: int = 0
    cost: CostTracker = field(default_factory=CostTracker)
    duration_seconds: float = 0.0
    files_processed: int = 0
    files_skipped: int = 0


def collect_python_files(target_dir: str | Path, max_files: int = MAX_FILES) -> list[Path]:
    """Walk target directory and collect .py files, sorted by path."""
    target = Path(target_dir)
    if not target.is_dir():
        raise FileNotFoundError(f"Target directory not found: {target}")

    py_files = sorted(target.rglob("*.py"))
    # Filter out test files for extraction (but collect them for stats)
    extraction_files = [
        f
        for f in py_files
        if not any(
            part in f.name for part in ("test_", "_test.py", "conftest.py")
        )
        and "__pycache__" not in str(f)
    ]
    return extraction_files[:max_files]


def _read_file_safe(path: Path, max_size: int = MAX_FILE_SIZE) -> str | None:
    """Read a file, returning None if too large or unreadable."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_size:
            return None
        if not content.strip():
            return None
        return content
    except OSError:
        return None


def _extract_tool_input(response: anthropic.types.Message) -> dict | None:
    """Extract the tool_use input from an Anthropic response."""
    for block in response.content:
        if block.type == "tool_use":
            return block.input  # type: ignore[return-value]
    return None


def run_extraction(
    target_dir: str | Path,
    repo_name: str | None = None,
    api_key: str | None = None,
) -> ExtractionResult:
    """Run the full extraction probe against a target directory.

    Args:
        target_dir: Path to the Python project to analyze.
        repo_name: Human-readable repo name (defaults to directory name).
        api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var).

    Returns:
        ExtractionResult with all extracted entities and metadata.
    """
    target = Path(target_dir)
    if repo_name is None:
        repo_name = target.name

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    result = ExtractionResult()
    start_time = time.monotonic()

    # Phase 1: Collect files
    py_files = collect_python_files(target)
    if not py_files:
        raise ValueError(f"No Python files found in {target}")

    # Phase 2: Component extraction — batch related files by directory
    batches = _batch_by_directory(py_files, target)

    for batch_files in batches:
        content_parts: list[str] = []
        for f in batch_files:
            text = _read_file_safe(f)
            if text is None:
                result.files_skipped += 1
                continue
            rel_path = f.relative_to(target)
            content_parts.append(f"# File: {rel_path}\n{text}")
            result.files_processed += 1

        if not content_parts:
            continue

        file_content = "\n\n".join(content_parts)
        # Enforce content size limit per call
        if len(file_content) > MAX_FILE_SIZE:
            file_content = file_content[:MAX_FILE_SIZE]

        system_prompt, user_msg, canary = build_component_prompt(repo_name, file_content)
        result.total_attempts += 1

        try:
            response = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                tools=[COMPONENT_TOOL],
                tool_choice={"type": "tool", "name": "extract_components"},
            )
            result.cost.record(response.usage)

            tool_input = _extract_tool_input(response)
            if tool_input is None:
                result.validation_failures += 1
                continue

            # Canary check
            if tool_input.get("canary_echo") != canary:
                result.canary_mismatches += 1

            # Validate components
            components = tool_input.get("components", [])
            for comp_data in components:
                validated = validate_component_result(comp_data)
                if validated is not None:
                    result.components.append(validated)
                else:
                    result.validation_failures += 1

        except anthropic.APIError as e:
            print(f"  API error during component extraction: {e}")
            result.validation_failures += 1

    # Phase 3: Flow extraction — use component summaries as context
    if result.components:
        component_summaries = json.dumps(
            [
                {"name": c.name, "type": c.type, "purpose": c.purpose}
                for c in result.components
            ],
            indent=2,
        )

        # Collect entry-point files (routers, handlers, controllers, main)
        entry_point_files = _find_entry_points(py_files, target)
        if not entry_point_files:
            # Fall back to all files if no obvious entry points
            entry_point_files = py_files[:5]

        for ep_file in entry_point_files:
            text = _read_file_safe(ep_file)
            if text is None:
                continue

            system_prompt, user_msg, canary = build_flow_prompt(
                repo_name, component_summaries, text
            )
            result.total_attempts += 1

            try:
                response = client.messages.create(
                    model=EXTRACTION_MODEL,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_msg}],
                    tools=[FLOW_TOOL],
                    tool_choice={"type": "tool", "name": "extract_flows"},
                )
                result.cost.record(response.usage)

                tool_input = _extract_tool_input(response)
                if tool_input is None:
                    result.validation_failures += 1
                    continue

                if tool_input.get("canary_echo") != canary:
                    result.canary_mismatches += 1

                flows = tool_input.get("flows", [])
                for flow_data in flows:
                    validated = validate_flow_result(flow_data)
                    if validated is not None:
                        result.flows.append(validated)
                    else:
                        result.validation_failures += 1

            except anthropic.APIError as e:
                print(f"  API error during flow extraction: {e}")
                result.validation_failures += 1

    result.duration_seconds = time.monotonic() - start_time
    return result


def _batch_by_directory(files: list[Path], root: Path) -> list[list[Path]]:
    """Group files by their parent directory for batched extraction."""
    batches: dict[Path, list[Path]] = {}
    for f in files:
        parent = f.parent.relative_to(root)
        batches.setdefault(parent, []).append(f)

    return list(batches.values())


def _find_entry_points(files: list[Path], root: Path) -> list[Path]:
    """Identify likely entry-point files for flow extraction."""
    entry_keywords = (
        "main", "app", "server", "router", "handler", "controller",
        "api", "views", "endpoints", "urls", "routes",
    )
    entry_files = []
    for f in files:
        stem = f.stem.lower()
        if any(kw in stem for kw in entry_keywords):
            entry_files.append(f)
    return entry_files
