"""ExtractionPipeline — orchestrates phases A-I for a single repository.

Phase A: File triage (Haiku) — classify files for extraction priority
Phase B: N/A in pipeline (clone/fetch handled by GitAdapter externally)
Phase C: N/A in pipeline (file listing handled by GitAdapter externally)
Phase D: Component extraction (Sonnet) — extract Component entities
Phase E: Flow extraction (Sonnet) — extract Flow entities from entry points
Phase F: Dependency extraction (Sonnet) — extract Dependency entities
Phase G: Relationship synthesis (Sonnet) — cross-reference entities from B-F
Phase H: Knowledge index compression (Sonnet) — generate <=5K token summaries
Phase I: Confidence scoring (Sonnet) — assign ConfidenceLevel to all entities

Phases B and C (clone/fetch and file inventory) are handled by the caller
using GitAdapter before invoking the pipeline. The pipeline receives file
paths and a read function, not a raw repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bifrost.extraction.checkpoint import ExtractionCheckpoint
from bifrost.extraction.cli_client import (
    ClaudeCliParseError,
    ClaudeCliTransientError,
    ClaudeLlmClient,
    CliCompletion,
)
from bifrost.extraction.prompts import (
    COMPONENT_SYSTEM_PROMPT,
    COMPONENT_TOOL_SCHEMA,
    CONFIDENCE_SYSTEM_PROMPT,
    CONFIDENCE_TOOL_SCHEMA,
    DEPENDENCY_SYSTEM_PROMPT,
    DEPENDENCY_TOOL_SCHEMA,
    FLOW_SYSTEM_PROMPT,
    FLOW_TOOL_SCHEMA,
    KNOWLEDGE_INDEX_SYSTEM_PROMPT,
    KNOWLEDGE_INDEX_TOOL_SCHEMA,
    MAX_CHARS_PER_CALL,
    RELATIONSHIP_SYSTEM_PROMPT,
    RELATIONSHIP_TOOL_SCHEMA,
    build_canary,
    build_component_batch_messages,
    build_component_messages,
    build_confidence_messages,
    build_dependency_messages,
    build_flow_messages,
    build_knowledge_index_messages,
    build_relationship_messages,
    validate_canary,
)
from bifrost.extraction.triage import TriageResult, triage_files

if TYPE_CHECKING:
    from bifrost.extraction.cost import CostTracker

logger = logging.getLogger(__name__)


# ── Content size validation ─────────────────────────────────────────


class ContentTooLargeError(ValueError):
    """Raised when file content exceeds the 50K token limit."""


def _validate_content_size(content: str) -> None:
    """Reject content that exceeds 50K tokens (~200K chars).

    Called before every LLM invocation to protect cost and latency.
    """
    if len(content) > MAX_CHARS_PER_CALL:
        estimated_tokens = len(content) // 4
        msg = (
            f"Content too large for extraction: ~{estimated_tokens:,} tokens "
            f"(limit: 50K tokens / {MAX_CHARS_PER_CALL:,} chars)"
        )
        raise ContentTooLargeError(msg)


def _has_flows_worth_extracting(content: str) -> bool:
    """Heuristic: does this file contain code that could plausibly describe a flow?

    Phase E hangs the LLM when fed pure data files (enums, records, DTOs, value
    objects with no behavior). They have no methods, no branches, nothing to
    trace — but the schema demands a flow, so the model thinks in circles.

    Returns False for files that are clearly enum-only / POJO-only / interface-only.
    """
    # Strip block and line comments before pattern-matching
    import re as _re

    stripped = _re.sub(r"/\*[\s\S]*?\*/", "", content)
    stripped = _re.sub(r"//[^\n]*", "", stripped)

    # Any branching / loop / invocation suggests flow-worthy logic
    behavior_markers = (
        "if ",
        "if(",
        "for ",
        "for(",
        "while ",
        "while(",
        "switch ",
        "switch(",
        "case ",
        "try ",
        "try{",
        "catch ",
        "return ",
        "throw ",
        "await ",
        "async ",
        ".then(",
        ".catch(",
    )
    if any(m in stripped for m in behavior_markers):
        return True

    # Enum-only files — `enum Foo { A, B, C; }` with no methods
    if _re.search(r"\benum\s+\w+\s*\{[^}]*\}", stripped) and "void " not in stripped:
        # If the only method tokens are the auto-generated values()/valueOf(),
        # there's nothing to extract.
        method_like = _re.findall(r"\b(?:public|private|protected)\s+\w+\s+\w+\s*\(", stripped)
        if len(method_like) <= 1:
            return False

    # Pure interface with only method signatures — Phase D handled it; no flow.
    if _re.search(r"\binterface\s+\w+\s*\{", stripped) and "{" in stripped:
        if "return " not in stripped and "for " not in stripped:
            return False

    # Record/data-class with no methods
    return not _re.match(r"\s*(public\s+)?record\s+\w+", stripped)


# ── Retry absorption ───────────────────────────────────────────────

MAX_RETRIES = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)  # Exponential: 1s, 2s, 4s

# Phase D parallelism: each unit spawns a `claude -p` subprocess.
# 5 concurrent calls is comfortable under the CLI's session limits.
PHASE_D_CONCURRENCY = 5

# Batching: files smaller than this threshold are packed together; larger
# files get their own call. Packing amortises the ~1-2s subprocess spawn cost.
PHASE_D_SMALL_FILE_CHARS = 3_500
PHASE_D_BATCH_SIZE = 5

# Phase E parallelism: previously serial per-repo, causing a 30+ min tail on
# repos with many entry points. 3 concurrent is enough to cut wall-clock
# without stressing the shared CLI session.
PHASE_E_CONCURRENCY = 3

# Phase E per-call timeout (seconds). With the "≤3 flows, ≤6 segments"
# prompt a legit call finishes in 30-60s. 90s bounds the worst case
# without wasting too much on misses.
PHASE_E_TIMEOUT_SECONDS = 90


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is worth retrying (rate limit, timeout, transient)."""
    if isinstance(exc, (ClaudeCliTransientError, ClaudeCliParseError)):
        return True
    exc_str = str(exc).lower()
    retryable_patterns = ("rate_limit", "rate limit", "timeout", "overloaded", "529", "503")
    return any(p in exc_str for p in retryable_patterns)


def _is_transient(exc: Exception) -> bool:
    """Check if an exception is a transient network/API error worth retrying."""
    if isinstance(exc, (ClaudeCliTransientError, ClaudeCliParseError)):
        return True
    cls_name = type(exc).__name__.lower()
    if "connection" in cls_name or "timeout" in cls_name:
        return True
    return _is_retryable(exc)


async def _retry_llm_call(
    coro_factory: Callable[[], Awaitable[Any]],
    phase: str,
    context: str = "",
) -> Any:
    """Execute an LLM call with retry absorption.

    Args:
        coro_factory: Zero-arg callable that returns a fresh awaitable for the LLM call.
        phase: Phase name for logging (e.g., "D", "G").
        context: Additional context for log messages (e.g., file path).

    Returns:
        The LLM response on success.

    Raises:
        The last exception after all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1 and _is_retryable(exc):
                delay = BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Phase %s retry %d/%d for %s (waiting %.1fs): %s",
                    phase,
                    attempt + 1,
                    MAX_RETRIES,
                    context,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                if attempt > 0:
                    logger.warning(
                        "Phase %s failed after %d attempts for %s: %s",
                        phase,
                        attempt + 1,
                        context,
                        exc,
                    )
                raise
    raise last_exc  # type: ignore[misc]


# ── Extracted entity containers ─────────────────────────────────────


@dataclass
class ExtractedComponent:
    """Raw component data extracted by the LLM."""

    component_name: str
    component_type: str
    purpose: str
    source_file: str
    exported_interfaces: list[str] = field(default_factory=list)
    internal_dependencies: list[str] = field(default_factory=list)
    known_gaps: list[str] = field(default_factory=list)


@dataclass
class ExtractedFlowSegment:
    """One step in an extracted flow."""

    component_name: str
    segment_type: str
    description: str
    order: int = 0


@dataclass
class ExtractedFlow:
    """Raw flow data extracted by the LLM."""

    flow_name: str
    flow_type: str
    segments: list[ExtractedFlowSegment] = field(default_factory=list)
    known_gaps: list[str] = field(default_factory=list)


@dataclass
class ExtractedDependency:
    """Raw dependency data extracted by the LLM."""

    source_name: str
    target_name: str
    dep_type: str
    discovery_method: str
    description: str = ""


@dataclass
class ExtractedRelationship:
    """Raw relationship data synthesized in Phase G."""

    source_urn: str
    target_urn: str
    relationship_type: str
    description: str
    evidence: str = ""


@dataclass
class KnowledgeIndexEntry:
    """Compressed knowledge index entry from Phase H."""

    entity_urn: str
    summary: str
    key_facts: list[str] = field(default_factory=list)
    search_keywords: list[str] = field(default_factory=list)


@dataclass
class ConfidenceScore:
    """Confidence assessment from Phase I."""

    entity_urn: str
    confidence_level: str
    confidence_score: float
    reason: str


@dataclass
class ExtractionOutput:
    """Aggregated output from the full A-I pipeline."""

    components: list[ExtractedComponent] = field(default_factory=list)
    flows: list[ExtractedFlow] = field(default_factory=list)
    dependencies: list[ExtractedDependency] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)
    knowledge_index: list[KnowledgeIndexEntry] = field(default_factory=list)
    confidence_scores: list[ConfidenceScore] = field(default_factory=list)
    triage: TriageResult | None = None
    files_analyzed: int = 0
    errors: list[str] = field(default_factory=list)
    partial: bool = False
    repo_summary: str | None = None


# ── Read function type ──────────────────────────────────────────────

ReadFileFn = Callable[[str], Awaitable[str]]


# ── ExtractionPipeline ──────────────────────────────────────────────


class ExtractionPipeline:
    """Orchestrates extraction phases A through F.

    Usage::

        pipeline = ExtractionPipeline(client=anthropic_client)
        output = await pipeline.run(
            file_paths=["src/main.py", "src/utils.py", ...],
            read_file=adapter.read_file,      # async (path) -> str
            file_previews={"src/main.py": "def main...", ...},
        )
    """

    def __init__(
        self,
        client: ClaudeLlmClient,
        *,
        repo_id: str = "unknown",
        triage_model: str = "claude-opus-4-8",
        extraction_model: str = "claude-sonnet-4-6",
        on_phase_complete: Callable[[str], Awaitable[None]] | None = None,
        on_progress: Callable[[int, str], Awaitable[None]] | None = None,
        cost_tracker: CostTracker | None = None,
        on_partial_output: Callable[[str, ExtractionOutput], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._repo_id = repo_id
        self._triage_model = triage_model
        self._extraction_model = extraction_model
        self._on_phase_complete = on_phase_complete
        self._on_progress = on_progress
        self._cost_tracker = cost_tracker
        # Called after each phase (D, E, F, G) with the phase letter and the
        # output-so-far. Lets the worker persist entities incrementally so
        # the UI sees them live instead of at the very end.
        self._on_partial_output = on_partial_output

    def _record_cost(self, completion: CliCompletion, phase: str, model: str) -> None:
        """Record LLM token usage if a cost tracker is attached.

        Uses Claude CLI's ``total_cost_usd`` (the billed number, computed by
        the CLI using live Anthropic pricing) as source of truth. Falls back
        to our price-table estimate only if the CLI didn't report a cost.
        """
        if self._cost_tracker is None:
            return
        u = completion.usage
        self._cost_tracker.record(
            model=model,
            phase=phase,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens,
            reported_cost_usd=u.total_cost_usd,
        )

    async def _run_phase_call(
        self,
        *,
        phase: str,
        context: str,
        system_prompt: str,
        user_message: str,
        json_schema: dict,
        canary: str,
        model: str | None = None,
        timeout_override: float | None = None,
    ) -> CliCompletion:
        """Invoke the CLI client for one phase call with retry + canary check."""
        chosen_model = model or self._extraction_model
        # Stable cache key: strip the per-call random canary so identical file
        # content hits the response cache across runs / re-registrations (the
        # canary would otherwise make every extraction prompt unique → 0 hits).
        # On a hit, the cached clean output won't contain the new canary, so
        # the post-call validation below still passes.
        from bifrost.extraction.llm_cache import make_cache_key

        stable_msg = user_message.replace(canary, "<CANARY>") if canary else user_message
        cache_key = make_cache_key(
            system_prompt=system_prompt,
            user_message=stable_msg,
            model=chosen_model,
            json_schema=json_schema,
        )
        completion = await _retry_llm_call(
            lambda: self._client.call_with_json_schema(
                system_prompt=system_prompt,
                user_message=user_message,
                model=chosen_model,
                json_schema=json_schema,
                timeout_override=timeout_override,
                cache_key_override=cache_key,
            ),
            phase=phase,
            context=context,
        )
        self._record_cost(completion, phase, chosen_model)
        # Canary check: the only real injection vector is the structured
        # output itself (what the pipeline consumes). Checking completion.text
        # gave 95% false positives because the model's assistant-text blocks
        # often restate the user instruction verbatim. Check the serialized
        # parsed payload instead — that's what actually gets persisted.
        parsed_text = json.dumps(completion.parsed)
        if not validate_canary(parsed_text, canary):
            logger.warning(
                "Canary echoed in phase %s structured output (%s) — possible prompt injection",
                phase,
                context,
            )
        return completion

    # ── Public entry point ──────────────────────────────────────────

    async def run(
        self,
        file_paths: list[str],
        read_file: ReadFileFn,
        file_previews: dict[str, str] | None = None,
        *,
        cancellation_token: Any | None = None,
        resume_checkpoint: ExtractionCheckpoint | None = None,
        max_extraction_files: int | None = None,
    ) -> ExtractionOutput:
        """Run the full A-I extraction pipeline.

        Args:
            file_paths: All tracked file paths in the repo.
            read_file: Async function to read file content by path.
            file_previews: Optional {path: first_200_bytes} for triage.
            cancellation_token: Optional cooperative cancel/pause signal.
                Must have an async ``check()`` method.
            resume_checkpoint: Optional checkpoint for skipping completed phases.
        """
        # Store checkpoint on self so helpers can record per-unit progress
        self._checkpoint = resume_checkpoint
        skip_phases: set[str] = set()
        if resume_checkpoint:
            skip_phases = set(resume_checkpoint.get_completed_phases())
            if skip_phases:
                logger.info("Resuming: skipping phases %s", sorted(skip_phases))

        output = ExtractionOutput()
        token = cancellation_token  # shorthand

        # Phase A: Triage (always run — cheap and needed for file lists)
        logger.info("Phase A: Triaging %d files", len(file_paths))

        def _record_triage_usage(usage: Any, phase: str) -> None:
            if self._cost_tracker is not None:
                self._cost_tracker.record(
                    model=self._triage_model,
                    phase=phase,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_input_tokens,
                    reported_cost_usd=getattr(usage, "total_cost_usd", None),
                )

        triage = await triage_files(
            file_paths,
            file_previews or {},
            self._client,
            model=self._triage_model,
            on_progress=self._on_progress,
            record_usage=_record_triage_usage,
        )
        output.triage = triage

        if self._on_phase_complete:
            await self._on_phase_complete("A")

        # Get prioritized file list for extraction
        extraction_files = triage.prioritized_for_extraction()
        if not extraction_files:
            logger.warning("No files selected for extraction after triage")
            return output

        # Cap extraction files to avoid multi-hour runs.
        # max_extraction_files param overrides the default 500.
        cap = max_extraction_files if max_extraction_files and max_extraction_files > 0 else 500
        if len(extraction_files) > cap:
            logger.warning(
                "Triage returned %d files; capping to %d (key components first)",
                len(extraction_files),
                cap,
            )
            extraction_files = extraction_files[:cap]

        # Phase D: Component extraction — parallelized with bounded concurrency,
        # small files packed into batches to amortise the subprocess spawn cost.
        if "D" not in skip_phases:
            if token:
                await token.check()
            total_d = len(extraction_files)
            logger.info("Phase D: Extracting components from %d files", total_d)
            semaphore = asyncio.Semaphore(PHASE_D_CONCURRENCY)
            state_lock = asyncio.Lock()
            counters = {"done": 0, "errors": 0}

            # Read content up-front so we can sort by size. (Reads are cheap —
            # just filesystem I/O in the git clone.)
            file_blobs: list[tuple[str, str]] = []  # (path, content)
            for fpath in extraction_files:
                try:
                    content = await read_file(fpath)
                    _validate_content_size(content)
                    file_blobs.append((fpath, content))
                except ContentTooLargeError:
                    counters["errors"] += 1
                    logger.warning("Skipping %s: content too large", fpath)
                    output.errors.append(f"Content too large: {fpath}")
                except Exception as exc:
                    counters["errors"] += 1
                    logger.warning("Read failed for %s: %s", fpath, exc)
                    output.errors.append(f"Read failed: {fpath}")

            # Partition: small files get batched; large files go one-per-call
            small: list[tuple[str, str]] = []
            large: list[tuple[str, str]] = []
            for fpath, content in file_blobs:
                (small if len(content) <= PHASE_D_SMALL_FILE_CHARS else large).append(
                    (fpath, content),
                )

            # Pack small files into batches of PHASE_D_BATCH_SIZE
            batches: list[list[tuple[str, str]]] = []
            for i in range(0, len(small), PHASE_D_BATCH_SIZE):
                batches.append(small[i : i + PHASE_D_BATCH_SIZE])
            # Add large files as 1-file "batches"
            batches.extend([[(p, c)] for p, c in large])

            logger.info(
                "Phase D: packed %d files into %d batches (%d small + %d large)",
                len(file_blobs),
                len(batches),
                len(small),
                len(large),
            )

            async def _process_batch(batch: list[tuple[str, str]]) -> None:
                if token:
                    await token.check()
                paths = [p for p, _ in batch]
                contents = [c for _, c in batch]
                # Per-batch checkpoint key = sorted paths joined. Stable
                # across runs for the same file set, so resume works.
                batch_key = "|".join(sorted(paths))
                if self._checkpoint and self._checkpoint.unit_done("D", batch_key):
                    logger.info("Phase D: skip batch (already done) %d files", len(batch))
                    async with state_lock:
                        counters["done"] += len(batch)
                    return
                try:
                    async with semaphore:
                        for attempt in range(3):
                            try:
                                if len(batch) == 1:
                                    components = await self._extract_components(
                                        paths[0],
                                        contents[0],
                                    )
                                else:
                                    components = await self._extract_components_batch(
                                        paths,
                                        contents,
                                    )
                                break
                            except Exception as retry_exc:
                                if attempt < 2 and _is_transient(retry_exc):
                                    wait = (attempt + 1) * 1.0
                                    logger.warning(
                                        "Phase D retry %d/3 for batch %s (waiting %.1fs): %s",
                                        attempt + 1,
                                        paths,
                                        wait,
                                        retry_exc,
                                    )
                                    await asyncio.sleep(wait)
                                else:
                                    raise
                        else:
                            raise RuntimeError("Phase D retry loop exited without a result")
                    async with state_lock:
                        output.components.extend(components)
                        output.files_analyzed += len(batch)
                    # Mark batch done AFTER successful extraction so crash
                    # mid-batch re-runs it, not skips it.
                    if self._checkpoint:
                        self._checkpoint.mark_unit_done("D", batch_key)
                except Exception as exc:
                    async with state_lock:
                        counters["errors"] += len(batch)
                        for p in paths:
                            output.errors.append(f"Component extraction failed: {p}")
                    logger.warning(
                        "Phase D batch failed for %d files: %s",
                        len(batch),
                        exc,
                    )
                finally:
                    async with state_lock:
                        counters["done"] += len(batch)
                        if self._on_progress and total_d > 0:
                            pct = 10 + int(20 * counters["done"] / total_d)
                            err_suffix = (
                                f" ({counters['errors']} errors)" if counters["errors"] else ""
                            )
                            await self._on_progress(
                                pct,
                                f"Phase D: {counters['done']}/{total_d} files{err_suffix}",
                            )

            await asyncio.gather(
                *(_process_batch(b) for b in batches),
                return_exceptions=False,
            )

            if self._on_phase_complete:
                await self._on_phase_complete("D")
            if self._on_partial_output:
                await self._on_partial_output("D", output)

        # Phase E: Flow extraction (uses component summaries)
        if "E" not in skip_phases and output.components:
            if token:
                await token.check()
            logger.info("Phase E: Extracting flows from entry points")
            component_summaries = [
                {
                    "name": c.component_name,
                    "type": c.component_type,
                    "purpose": c.purpose,
                }
                for c in output.components
            ]
            # Use component_definition files with likely_key_component as entry points
            entry_points = [
                f.path
                for f in triage.files
                if f.category == "component_definition" and f.likely_key_component
            ]
            if not entry_points and extraction_files:
                entry_points = extraction_files[:3]

            # Parallelize Phase E — 3-way semaphore. Phase E on a 30-file
            # repo was taking 30+ min serially; parallel cuts wall-clock to
            # ~10 min without stressing the Claude CLI session.
            phase_e_sem = asyncio.Semaphore(PHASE_E_CONCURRENCY)

            async def _extract_one_flow(fpath: str) -> list[ExtractedFlow]:
                async with phase_e_sem:
                    if token:
                        await token.check()
                    if self._checkpoint and self._checkpoint.unit_done("E", fpath):
                        logger.info("Phase E: skip (already done) %s", fpath)
                        return []
                    try:
                        content = await read_file(fpath)
                        _validate_content_size(content)
                        if not _has_flows_worth_extracting(content):
                            logger.info(
                                "Phase E: skipping %s (no flow-worthy logic detected)",
                                fpath,
                            )
                            # Mark skipped files done too — no need to re-check next run
                            if self._checkpoint:
                                self._checkpoint.mark_unit_done("E", fpath)
                            return []
                        flows = await self._extract_flows(fpath, content, component_summaries)
                        if self._checkpoint:
                            self._checkpoint.mark_unit_done("E", fpath)
                        return flows
                    except ContentTooLargeError:
                        logger.warning("Skipping flow extraction for %s: too large", fpath)
                        return []
                    except Exception:
                        logger.exception("Flow extraction failed for %s", fpath)
                        output.errors.append(f"Flow extraction failed: {fpath}")
                        return []

            flow_lists = await asyncio.gather(
                *(_extract_one_flow(fp) for fp in entry_points),
                return_exceptions=False,
            )
            for flows in flow_lists:
                output.flows.extend(flows)

            if self._on_phase_complete:
                await self._on_phase_complete("E")
            if self._on_partial_output:
                await self._on_partial_output("E", output)

        # Phase F: Dependency extraction
        if "F" not in skip_phases:
            if token:
                await token.check()
            logger.info("Phase F: Extracting dependencies")
            manifest_content = None
            for mpath in triage.dependency_manifests:
                try:
                    manifest_content = await read_file(mpath)
                    break
                except Exception:
                    logger.warning("Could not read manifest %s", mpath)

            sample_files: list[dict[str, str]] = []
            for fpath in extraction_files[:5]:
                try:
                    content = await read_file(fpath)
                    sample_files.append({"path": fpath, "content": content[:10000]})
                except Exception:
                    pass

            if manifest_content or sample_files:
                try:
                    deps = await self._extract_dependencies(manifest_content, sample_files)
                    output.dependencies.extend(deps)
                except Exception:
                    logger.exception("Dependency extraction failed")
                    output.errors.append("Dependency extraction failed")

            if self._on_phase_complete:
                await self._on_phase_complete("F")
            if self._on_partial_output:
                await self._on_partial_output("F", output)

        # Phase G: Relationship synthesis
        if "G" not in skip_phases and (output.components or output.flows or output.dependencies):
            if token:
                await token.check()
            logger.info("Phase G: Synthesizing relationships")
            try:
                rels = await self._extract_relationships(output)
                output.relationships.extend(rels)
            except Exception:
                logger.exception("Relationship synthesis failed")
                output.errors.append("Relationship synthesis failed")
            if self._on_phase_complete:
                await self._on_phase_complete("G")
            if self._on_partial_output:
                await self._on_partial_output("G", output)

        # Phase H: Knowledge index compression
        if "H" not in skip_phases and (output.components or output.flows):
            if token:
                await token.check()
            logger.info("Phase H: Compressing knowledge index")
            try:
                entries = await self._compress_knowledge_index(output)
                output.knowledge_index.extend(entries)
            except Exception:
                logger.exception("Knowledge index compression failed")
                output.errors.append("Knowledge index compression failed")
            if self._on_phase_complete:
                await self._on_phase_complete("H")

        # Phase I: Confidence scoring
        if "I" not in skip_phases and (output.components or output.flows or output.dependencies):
            if token:
                await token.check()
            logger.info("Phase I: Scoring confidence")
            try:
                scores = await self._score_confidence(output)
                output.confidence_scores.extend(scores)
            except Exception:
                logger.exception("Confidence scoring failed")
                output.errors.append("Confidence scoring failed")
            if self._on_phase_complete:
                await self._on_phase_complete("I")

        # Phase J only feeds repos.description, so a failure is recorded, not fatal.
        try:
            output.repo_summary = await self._generate_repo_summary(output)
        except Exception as exc:
            logger.exception("Phase J (repo summary) failed")
            output.errors.append(f"Phase J (repo summary) failed: {exc}")
        if output.repo_summary and self._on_phase_complete:
            await self._on_phase_complete("J")

        if output.errors:
            output.partial = True

        return output

    # ── Phase D: Component extraction ───────────────────────────────

    async def _extract_components(self, file_path: str, content: str) -> list[ExtractedComponent]:
        canary = build_canary()
        messages = build_component_messages(file_path, content, canary)
        user_text = "\n\n".join(m["content"] for m in messages)

        completion = await self._run_phase_call(
            phase="D",
            context=file_path,
            system_prompt=COMPONENT_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=COMPONENT_TOOL_SCHEMA["input_schema"],
            canary=canary,
        )
        raw = completion.parsed.get("components", []) or []

        return [
            ExtractedComponent(
                component_name=c.get("component_name", "unknown"),
                component_type=c.get("component_type", "module"),
                purpose=c.get("purpose", ""),
                source_file=c.get("source_file", file_path),
                exported_interfaces=c.get("exported_interfaces", []),
                internal_dependencies=c.get("internal_dependencies", []),
                known_gaps=c.get("known_gaps", []),
            )
            for c in raw
            if isinstance(c, dict)
        ]

    async def _extract_components_batch(
        self, paths: list[str], contents: list[str]
    ) -> list[ExtractedComponent]:
        """Extract components from multiple files in a single LLM call.

        Packing small files together amortises the ~1-2s subprocess spawn
        cost across 3-5x more files per call.
        """
        canary = build_canary()
        files = [{"path": p, "content": c} for p, c in zip(paths, contents, strict=True)]
        messages = build_component_batch_messages(files, canary)
        user_text = "\n\n".join(m["content"] for m in messages)

        completion = await self._run_phase_call(
            phase="D",
            context=f"batch[{len(paths)}] {paths[0]}…",
            system_prompt=COMPONENT_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=COMPONENT_TOOL_SCHEMA["input_schema"],
            canary=canary,
        )
        raw = completion.parsed.get("components", []) or []

        # Use the FIRST file path as a fallback when a component doesn't
        # identify its source; this only happens when Claude forgets the tag.
        fallback_path = paths[0] if paths else "unknown"
        return [
            ExtractedComponent(
                component_name=c.get("component_name", "unknown"),
                component_type=c.get("component_type", "module"),
                purpose=c.get("purpose", ""),
                source_file=c.get("source_file") or fallback_path,
                exported_interfaces=c.get("exported_interfaces", []),
                internal_dependencies=c.get("internal_dependencies", []),
                known_gaps=c.get("known_gaps", []),
            )
            for c in raw
            if isinstance(c, dict)
        ]

    # ── Phase E: Flow extraction ────────────────────────────────────

    async def _extract_flows(
        self,
        entry_path: str,
        content: str,
        component_summaries: list[dict[str, str]],
    ) -> list[ExtractedFlow]:
        canary = build_canary()
        messages = build_flow_messages(entry_path, content, component_summaries, canary)
        user_text = "\n\n".join(m["content"] for m in messages)

        # Phase E hangs easily on files that shouldn't have been sent here at
        # all (enums, DTOs) OR on complex service classes where exhaustive
        # flow enumeration takes >2 min. The "≤3 flows, ≤6 segments" prompt
        # caps output length so 90s is enough for legit calls; misses fail
        # fast rather than wasting minutes on dead ends.
        completion = await self._run_phase_call(
            phase="E",
            context=entry_path,
            system_prompt=FLOW_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=FLOW_TOOL_SCHEMA["input_schema"],
            canary=canary,
            timeout_override=PHASE_E_TIMEOUT_SECONDS,
        )
        raw = completion.parsed.get("flows", []) or []

        result: list[ExtractedFlow] = []
        for f in raw:
            if not isinstance(f, dict):
                continue
            segments = [
                ExtractedFlowSegment(
                    component_name=s.get("component_name", ""),
                    segment_type=s.get("segment_type", "sync-call"),
                    description=s.get("description", ""),
                    order=s.get("order", i),
                )
                for i, s in enumerate(f.get("segments", []) or [])
                if isinstance(s, dict)
            ]
            result.append(
                ExtractedFlow(
                    flow_name=f.get("flow_name", "unknown"),
                    flow_type=f.get("flow_type", "mixed"),
                    segments=segments,
                    known_gaps=f.get("known_gaps", []),
                )
            )
        return result

    # ── Phase F: Dependency extraction ──────────────────────────────

    async def _extract_dependencies(
        self,
        manifest_content: str | None,
        source_files: list[dict[str, str]],
    ) -> list[ExtractedDependency]:
        # Validate combined content size
        total = len(manifest_content or "")
        for sf in source_files:
            total += len(sf.get("content", ""))
        if total > MAX_CHARS_PER_CALL:
            # Truncate source files to fit
            budget = MAX_CHARS_PER_CALL - len(manifest_content or "")
            truncated: list[dict[str, str]] = []
            for sf in source_files:
                c = sf["content"]
                if len(c) > budget:
                    c = c[:budget]
                truncated.append({"path": sf["path"], "content": c})
                budget -= len(c)
                if budget <= 0:
                    break
            source_files = truncated

        canary = build_canary()
        messages = build_dependency_messages(manifest_content, source_files, canary)
        user_text = "\n\n".join(m["content"] for m in messages)

        completion = await self._run_phase_call(
            phase="F",
            context="manifests",
            system_prompt=DEPENDENCY_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=DEPENDENCY_TOOL_SCHEMA["input_schema"],
            canary=canary,
        )
        raw = completion.parsed.get("dependencies", []) or []

        return [
            ExtractedDependency(
                source_name=d.get("source_name", ""),
                target_name=d.get("target_name", ""),
                dep_type=d.get("dep_type", "code"),
                discovery_method=d.get("discovery_method", "inferred"),
                description=d.get("description", ""),
            )
            for d in raw
            if isinstance(d, dict)
        ]

    # ── Phase G: Relationship synthesis ──────────────────────────────

    async def _extract_relationships(self, output: ExtractionOutput) -> list[ExtractedRelationship]:
        comp_dicts = [
            {"name": c.component_name, "type": c.component_type, "purpose": c.purpose}
            for c in output.components
        ]
        flow_dicts = [
            {
                "name": f.flow_name,
                "type": f.flow_type,
                "segments": ", ".join(s.component_name for s in f.segments),
            }
            for f in output.flows
        ]
        dep_dicts = [
            {"source": d.source_name, "target": d.target_name, "type": d.dep_type}
            for d in output.dependencies
        ]

        canary = build_canary()
        messages = build_relationship_messages(
            comp_dicts, flow_dicts, dep_dicts, self._repo_id, canary
        )
        user_text = "\n\n".join(m["content"] for m in messages)

        completion = await self._run_phase_call(
            phase="G",
            context="relationships",
            system_prompt=RELATIONSHIP_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=RELATIONSHIP_TOOL_SCHEMA["input_schema"],
            canary=canary,
        )
        raw = completion.parsed.get("relationships", []) or []

        return [
            ExtractedRelationship(
                source_urn=r.get("source_urn", ""),
                target_urn=r.get("target_urn", ""),
                relationship_type=r.get("relationship_type", "uses"),
                description=r.get("description", ""),
                evidence=r.get("evidence", ""),
            )
            for r in raw
            if isinstance(r, dict)
        ]

    # ── Phase H: Knowledge index compression ──────────────────────────

    async def _compress_knowledge_index(
        self, output: ExtractionOutput
    ) -> list[KnowledgeIndexEntry]:
        entities: list[dict[str, str]] = []
        for c in output.components:
            entities.append(
                {
                    "urn": f"urn:bifrost:repo/{self._repo_id}/component/{c.component_name}",
                    "type": "component",
                    "name": c.component_name,
                    "detail": (
                        f"Type: {c.component_type}\nPurpose: {c.purpose}\n"
                        f"Interfaces: {', '.join(c.exported_interfaces)}\n"
                        f"Dependencies: {', '.join(c.internal_dependencies)}"
                    ),
                }
            )
        for f in output.flows:
            segments_desc = " -> ".join(s.component_name for s in f.segments)
            entities.append(
                {
                    "urn": f"urn:bifrost:repo/{self._repo_id}/flow/{f.flow_name}",
                    "type": "flow",
                    "name": f.flow_name,
                    "detail": f"Type: {f.flow_type}\nSegments: {segments_desc}",
                }
            )

        if not entities:
            return []

        canary = build_canary()
        messages = build_knowledge_index_messages(entities, self._repo_id, canary)
        user_text = "\n\n".join(m["content"] for m in messages)

        completion = await self._run_phase_call(
            phase="H",
            context="knowledge_index",
            system_prompt=KNOWLEDGE_INDEX_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=KNOWLEDGE_INDEX_TOOL_SCHEMA["input_schema"],
            canary=canary,
        )
        raw = completion.parsed.get("entries", []) or []

        return [
            KnowledgeIndexEntry(
                entity_urn=e.get("entity_urn", ""),
                summary=e.get("summary", "")[:20000],
                key_facts=e.get("key_facts", [])[:10],
                search_keywords=e.get("search_keywords", []),
            )
            for e in raw
            if isinstance(e, dict)
        ]

    # ── Phase I: Confidence scoring ───────────────────────────────────

    async def _score_confidence(self, output: ExtractionOutput) -> list[ConfidenceScore]:
        entities: list[dict[str, str]] = []
        cross_refs: dict[str, int] = {}

        # Build entity list and count cross-references
        for c in output.components:
            urn = f"urn:bifrost:repo/{self._repo_id}/component/{c.component_name}"
            entities.append(
                {
                    "urn": urn,
                    "type": "component",
                    "name": c.component_name,
                    "source_file": c.source_file,
                }
            )
            # Count references from flows and dependencies
            ref_count = sum(
                1 for f in output.flows for s in f.segments if s.component_name == c.component_name
            ) + sum(
                1
                for d in output.dependencies
                if d.source_name == c.component_name or d.target_name == c.component_name
            )
            cross_refs[urn] = ref_count

        for f in output.flows:
            urn = f"urn:bifrost:repo/{self._repo_id}/flow/{f.flow_name}"
            entities.append(
                {
                    "urn": urn,
                    "type": "flow",
                    "name": f.flow_name,
                    "source_file": "multiple",
                }
            )
            cross_refs[urn] = len(f.segments)

        if not entities:
            return []

        canary = build_canary()
        messages = build_confidence_messages(entities, cross_refs, canary)
        user_text = "\n\n".join(m["content"] for m in messages)

        completion = await self._run_phase_call(
            phase="I",
            context="confidence",
            system_prompt=CONFIDENCE_SYSTEM_PROMPT,
            user_message=user_text,
            json_schema=CONFIDENCE_TOOL_SCHEMA["input_schema"],
            canary=canary,
        )
        raw = completion.parsed.get("scores", []) or []

        return [
            ConfidenceScore(
                entity_urn=s.get("entity_urn", ""),
                confidence_level=s.get("confidence_level", "inferred"),
                confidence_score=min(1.0, max(0.0, float(s.get("confidence_score", 0.6)))),
                reason=s.get("reason", ""),
            )
            for s in raw
            if isinstance(s, dict)
        ]

    async def _generate_repo_summary(self, output: ExtractionOutput) -> str | None:
        """Phase J: 2-3 sentence plain-English description of the repo.

        Raises on LLM failure; ``run`` records it and keeps the extraction.
        """
        if not output.components and not output.flows:
            return None

        lines: list[str] = []
        if output.components:
            comp_lines = "\n".join(
                f"  - {c.component_name}: {c.purpose or '(no description)'}"
                for c in sorted(output.components, key=lambda x: x.component_name)[:10]
            )
            lines.append(f"Components:\n{comp_lines}")
        if output.flows:
            flow_lines = "\n".join(f"  - {f.flow_name}" for f in output.flows[:6])
            lines.append(f"Flows:\n{flow_lines}")
        if output.dependencies:
            dep_lines = "\n".join(
                f"  - {d.source_name}→{d.target_name}" for d in output.dependencies[:8]
            )
            lines.append(f"Dependencies:\n{dep_lines}")

        canary = build_canary()
        completion = await self._run_phase_call(
            phase="J",
            context=self._repo_id,
            system_prompt=(
                "You summarise software services in 2-3 precise sentences. "
                "Be specific: name the domain, what data the service processes, "
                "and what it produces or exposes. Avoid vague verbs like 'manages' or 'handles'."
            ),
            user_message=(
                "Write a 2-3 sentence description of this service based on its "
                "extracted code intelligence.\n\n"
                + "\n\n".join(lines)
                + f"\n\n[CANARY:{canary}] Write the description."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "2-3 sentence factual description of the service.",
                    }
                },
                "required": ["summary"],
            },
            canary=canary,
        )
        return completion.parsed.get("summary") or None

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_content_size(content: str) -> None:
        """Public alias for content size validation (used in verification checks)."""
        _validate_content_size(content)
