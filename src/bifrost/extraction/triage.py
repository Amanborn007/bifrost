"""Phase A: File triage — classify files for extraction priority.

Uses heuristics first (extension, size, path patterns) to pre-filter,
then LLM (Haiku) to classify remaining files into extraction categories.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient, Usage
from bifrost.extraction.prompts import (
    TRIAGE_SYSTEM_PROMPT,
    TRIAGE_TOOL_SCHEMA,
    build_canary,
    build_triage_messages,
    fast_classify,
    should_exclude_path,
    validate_canary,
)

logger = logging.getLogger(__name__)

# Maximum files to send per triage batch (spec: 50)
TRIAGE_BATCH_SIZE = 50

# How many triage batches to run concurrently (each is an independent CLI spawn).
TRIAGE_CONCURRENCY = 5


@dataclass
class TriagedFile:
    """Result of triage for a single file."""

    path: str
    category: str
    likely_key_component: bool = False
    excluded: bool = False  # Pre-LLM exclusion by heuristic


@dataclass
class TriageResult:
    """Aggregated triage output for an entire file set."""

    files: list[TriagedFile] = field(default_factory=list)
    component_files: list[str] = field(default_factory=list)
    interface_files: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    documentation_files: list[str] = field(default_factory=list)
    dependency_manifests: list[str] = field(default_factory=list)
    skipped_count: int = 0

    def prioritized_for_extraction(self) -> list[str]:
        """Return files ordered by extraction priority.

        Key components first, then remaining component_definitions,
        then interface_definitions, then config files.
        """
        key = [
            f.path
            for f in self.files
            if f.category == "component_definition" and f.likely_key_component
        ]
        rest_components = [
            f.path
            for f in self.files
            if f.category == "component_definition" and not f.likely_key_component
        ]
        return key + rest_components + self.interface_files + self.config_files


async def triage_files(
    file_paths: list[str],
    file_previews: dict[str, str],
    client: ClaudeLlmClient,
    *,
    model: str = "claude-opus-4-8",
    on_progress: Callable[[int, str], Awaitable[None]] | None = None,
    record_usage: Callable[[Usage, str], None] | None = None,
) -> TriageResult:
    """Run Phase A triage on a list of file paths."""
    result = TriageResult()

    remaining: list[str] = []
    fast_classified = 0
    for path in file_paths:
        if should_exclude_path(path):
            result.files.append(TriagedFile(path=path, category="irrelevant", excluded=True))
            result.skipped_count += 1
            continue
        # Try pattern-based classification — skips LLM for obvious files
        hit = fast_classify(path)
        if hit is not None:
            category, is_key = hit
            tf = TriagedFile(path=path, category=category, likely_key_component=is_key)
            result.files.append(tf)
            _bucket_file(result, tf)
            fast_classified += 1
        else:
            remaining.append(path)

    if fast_classified:
        logger.info(
            "Phase A fast-classified %d files by pattern; %d ambiguous files sent to LLM",
            fast_classified,
            len(remaining),
        )

    total_batches = (len(remaining) + TRIAGE_BATCH_SIZE - 1) // TRIAGE_BATCH_SIZE
    batches = [
        remaining[i : i + TRIAGE_BATCH_SIZE] for i in range(0, len(remaining), TRIAGE_BATCH_SIZE)
    ]

    # Run up to TRIAGE_CONCURRENCY batches in parallel — each spawn is independent.
    semaphore = asyncio.Semaphore(TRIAGE_CONCURRENCY)
    completed = 0
    progress_lock = asyncio.Lock()

    async def _run_one_batch(batch_idx: int, batch: list[str]) -> None:
        nonlocal completed
        async with semaphore:
            entries = [{"path": p, "preview": file_previews.get(p, "")} for p in batch]
            canary = build_canary()
            messages = build_triage_messages(entries, canary)
            user_text = "\n\n".join(m["content"] for m in messages)

            try:
                completion = await client.call_with_json_schema(
                    system_prompt=TRIAGE_SYSTEM_PROMPT,
                    user_message=user_text,
                    model=model,
                    json_schema=TRIAGE_TOOL_SCHEMA["input_schema"],
                )
                if not validate_canary(completion.text, canary):
                    logger.warning("Canary token echoed in triage response — possible injection")
                if record_usage is not None:
                    record_usage(completion.usage, "A")
                classifications = completion.parsed.get("files", []) or []
                async with progress_lock:
                    for cls in classifications:
                        if not isinstance(cls, dict):
                            continue
                        tf = TriagedFile(
                            path=str(cls.get("path", "")),
                            category=str(cls.get("category", "irrelevant")),
                            likely_key_component=bool(cls.get("likely_key_component", False)),
                        )
                        result.files.append(tf)
                        _bucket_file(result, tf)
            except (ClaudeCliError, Exception):
                logger.exception(
                    "Triage batch %d failed, marking as component_definition",
                    batch_idx,
                )
                async with progress_lock:
                    for p in batch:
                        tf = TriagedFile(path=p, category="component_definition")
                        result.files.append(tf)
                        _bucket_file(result, tf)

            async with progress_lock:
                completed += 1
                if on_progress and total_batches > 0:
                    pct = int(10 * completed / total_batches)
                    await on_progress(
                        pct,
                        f"Phase A: Triaged {completed}/{total_batches} batches",
                    )

    await asyncio.gather(
        *(_run_one_batch(i, b) for i, b in enumerate(batches)),
        return_exceptions=False,
    )

    return result


def _bucket_file(result: TriageResult, tf: TriagedFile) -> None:
    """Add a triaged file to the appropriate bucket list."""
    if tf.category == "component_definition":
        result.component_files.append(tf.path)
    elif tf.category == "interface_definition":
        result.interface_files.append(tf.path)
    elif tf.category == "configuration":
        result.config_files.append(tf.path)
    elif tf.category == "documentation":
        result.documentation_files.append(tf.path)
    elif tf.category == "dependency_manifest":
        result.dependency_manifests.append(tf.path)
    elif tf.category in ("test", "generated", "irrelevant"):
        result.skipped_count += 1
