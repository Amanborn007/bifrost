"""In-process extraction worker with lifecycle controls.

Manages background extraction tasks with start/pause/resume/cancel support.
Publishes progress via the SSE event bus for UI consumption.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.api.sse import SSEEvent, event_bus
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.checkpoint import ExtractionCheckpoint
from bifrost.extraction.cli_auth import (
    ClaudeCliNotAuthenticated,
    verify_claude_cli_auth,
)
from bifrost.extraction.cli_client import ClaudeLlmClient
from bifrost.extraction.cost import CostTracker
from bifrost.extraction.pipeline import ExtractionOutput, ExtractionPipeline

logger = logging.getLogger(__name__)


# ── Cancellation token ───────────────────────────────────────────────


class TokenState(StrEnum):
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    CANCEL_REQUESTED = "cancel_requested"


class CancellationToken:
    """Cooperative cancellation/pause token for extraction tasks.

    Insert ``await token.check()`` at safe points in the pipeline.
    Cancel raises ``asyncio.CancelledError``; pause blocks until resumed.
    """

    def __init__(self) -> None:
        self._state = TokenState.RUNNING
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # starts unblocked
        self.on_paused: Any | None = None  # callback: async () -> None

    @property
    def state(self) -> TokenState:
        return self._state

    def request_cancel(self) -> None:
        self._state = TokenState.CANCEL_REQUESTED
        # Unblock if paused so the cancel can propagate
        self._resume_event.set()

    def request_pause(self) -> None:
        if self._state == TokenState.RUNNING:
            self._state = TokenState.PAUSE_REQUESTED
            self._resume_event.clear()

    def resume(self) -> None:
        self._state = TokenState.RUNNING
        self._resume_event.set()

    async def check(self) -> None:
        """Call between pipeline steps. Raises CancelledError or blocks on pause."""
        if self._state == TokenState.CANCEL_REQUESTED:
            raise asyncio.CancelledError("Extraction cancelled by user")

        if self._state == TokenState.PAUSE_REQUESTED:
            if self.on_paused:
                await self.on_paused()
            await self._resume_event.wait()
            # After resume, check for cancel again
            if self._state == TokenState.CANCEL_REQUESTED:
                raise asyncio.CancelledError("Extraction cancelled while paused")


# ── Mock pipeline for dry-run testing ────────────────────────────────


async def _mock_extraction(
    file_paths: list[str],
    repo_name: str,
    repo_id: str,
    on_phase_complete: Any | None = None,
    cancellation_token: CancellationToken | None = None,
) -> ExtractionOutput:
    """Generate mock extraction output for dry-run testing."""
    from bifrost.extraction.pipeline import (
        ConfidenceScore,
        ExtractedComponent,
        ExtractedDependency,
        ExtractedFlow,
        ExtractedFlowSegment,
        ExtractedRelationship,
        KnowledgeIndexEntry,
    )

    token = cancellation_token
    output = ExtractionOutput(files_analyzed=len(file_paths))

    # Phase A: Triage (mock)
    await asyncio.sleep(0.3)
    if on_phase_complete:
        await on_phase_complete("A")
    if token:
        await token.check()

    # Phase D: Components (mock)
    await asyncio.sleep(0.5)
    code_exts = (
        ".ts",
        ".py",
        ".js",
        ".tsx",
        ".java",
        ".kt",
        ".go",
        ".rs",
        ".rb",
        ".cs",
        ".cpp",
        ".c",
        ".h",
    )
    source_files = [f for f in file_paths if f.endswith(code_exts)][:5]
    if not source_files:
        # Fallback: pick any non-binary files
        source_files = [f for f in file_paths if "." in f.rsplit("/", 1)[-1]][:5]
    for i, sf in enumerate(source_files):
        name = sf.rsplit("/", 1)[-1].replace(".ts", "").replace(".py", "").replace(".js", "")
        output.components.append(
            ExtractedComponent(
                component_name=name,
                component_type="service" if i % 2 == 0 else "utility",
                purpose=f"Mock component from {sf}",
                source_file=sf,
                exported_interfaces=[f"{name}Interface"],
                internal_dependencies=[],
            )
        )
    if on_phase_complete:
        await on_phase_complete("D")
    if token:
        await token.check()

    # Phase E: Flows (mock)
    await asyncio.sleep(0.3)
    if len(output.components) >= 2:
        output.flows.append(
            ExtractedFlow(
                flow_name="main_request_flow",
                flow_type="request",
                segments=[
                    ExtractedFlowSegment(
                        component_name=output.components[0].component_name,
                        segment_type="entry",
                        description="Request entry point",
                        order=0,
                    ),
                    ExtractedFlowSegment(
                        component_name=output.components[1].component_name,
                        segment_type="processing",
                        description="Core processing",
                        order=1,
                    ),
                ],
            )
        )
    if on_phase_complete:
        await on_phase_complete("E")
    if token:
        await token.check()

    # Phase F: Dependencies (mock)
    await asyncio.sleep(0.3)
    for i in range(min(3, len(output.components) - 1)):
        output.dependencies.append(
            ExtractedDependency(
                source_name=output.components[i].component_name,
                target_name=output.components[i + 1].component_name,
                dep_type="import",
                discovery_method="mock",
                description="Mock dependency",
            )
        )
    if on_phase_complete:
        await on_phase_complete("F")
    if token:
        await token.check()

    # Phase G: Relationships (mock)
    await asyncio.sleep(0.2)
    for dep in output.dependencies:
        output.relationships.append(
            ExtractedRelationship(
                source_urn=f"urn:bifrost:repo/{repo_name}/component/{dep.source_name}",
                target_urn=f"urn:bifrost:repo/{repo_name}/component/{dep.target_name}",
                relationship_type="uses",
                description=dep.description,
                evidence="mock",
            )
        )
    if on_phase_complete:
        await on_phase_complete("G")
    if token:
        await token.check()

    # Phase H: Knowledge index (mock)
    await asyncio.sleep(0.2)
    for comp in output.components:
        output.knowledge_index.append(
            KnowledgeIndexEntry(
                entity_urn=f"urn:bifrost:repo/{repo_name}/component/{comp.component_name}",
                summary=f"{comp.component_name}: {comp.purpose}",
                key_facts=[comp.purpose],
                search_keywords=[comp.component_name, comp.component_type],
            )
        )
    if on_phase_complete:
        await on_phase_complete("H")
    if token:
        await token.check()

    # Phase I: Confidence (mock)
    await asyncio.sleep(0.2)
    for comp in output.components:
        output.confidence_scores.append(
            ConfidenceScore(
                entity_urn=f"urn:bifrost:repo/{repo_name}/component/{comp.component_name}",
                confidence_level="inferred",
                confidence_score=0.75,
                reason="Mock extraction — no real analysis performed",
            )
        )
    if on_phase_complete:
        await on_phase_complete("I")

    return output


# ── Progress helpers ─────────────────────────────────────────────────

PHASE_PROGRESS = {"A": 10, "D": 30, "E": 50, "F": 60, "G": 70, "H": 80, "I": 90}


async def _publish_progress(task_id: str, progress: int, phase: str) -> None:
    await event_bus.publish(
        task_id,
        SSEEvent(event_type="extraction_progress", data={"progress": progress, "phase": phase}),
    )


async def _publish_phase_complete(task_id: str, phase: str) -> None:
    await event_bus.publish(
        task_id,
        SSEEvent(event_type="phase_complete", data={"phase": phase}),
    )
    progress = PHASE_PROGRESS.get(phase, 0)
    await _publish_progress(task_id, progress, f"Phase {phase} complete")


# ── Running task record ──────────────────────────────────────────────


@dataclass
class _RunningTask:
    """Tracks state for an in-flight extraction."""

    task_id: str
    asyncio_task: asyncio.Task[None]
    token: CancellationToken
    checkpoint: ExtractionCheckpoint


# ── Extraction worker ────────────────────────────────────────────────


class ExtractionWorker:
    """Manages background extraction tasks with lifecycle controls.

    Created once in the FastAPI lifespan, accessed via ``get_worker()``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: BifrostSettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._semaphore = asyncio.Semaphore(self._settings.max_concurrent_extractions)
        self._running: dict[str, _RunningTask] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        # Completion-event registry. ``_completion[task_id]`` is set when
        # the task reaches a terminal state; ``_final_state[task_id]``
        # records the outcome string. ``wait_for_task()`` reads both so
        # the chat coordinator can block on first-touch
        # auto-extraction before falling back to manifest-only answers.
        self._completion: dict[str, asyncio.Event] = {}
        self._final_state: dict[str, str] = {}
        self._completion_max = 256

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the worker — recover interrupted tasks and pick up queued ones.

        On startup we do two things:
          1. Recover tasks stuck in ``running`` (crash recovery — revert to
             ``received`` and re-submit).
          2. Auto-dispatch every task currently sitting in ``received`` so
             users don't have to poke the API after a restart. The semaphore
             enforces concurrency; extras queue naturally.
        """
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        async with self._session_factory() as session:
            from sqlalchemy import select, update

            from bifrost.storage.models import ExtractionTask as TaskModel

            # Step 1: flip crashed "running" rows back to "received"
            stuck = await session.execute(select(TaskModel.id).where(TaskModel.status == "running"))
            stuck_ids = [str(row[0]) for row in stuck.all()]
            if stuck_ids:
                logger.info("Recovering %d interrupted tasks", len(stuck_ids))
                await session.execute(
                    update(TaskModel).where(TaskModel.status == "running").values(status="received")
                )
                await session.commit()

            # Step 2: submit every received task (order by created_at so FIFO)
            pending = await session.execute(
                select(TaskModel.id)
                .where(TaskModel.status == "received")
                .order_by(TaskModel.created_at)
            )
            pending_ids = [str(row[0]) for row in pending.all()]

        if pending_ids:
            logger.info(
                "Auto-dispatching %d pending task(s) (semaphore=%d)",
                len(pending_ids),
                self._settings.max_concurrent_extractions,
            )
            for tid in pending_ids:
                self.submit(tid)

    async def stop(self) -> None:
        """Cancel all in-flight tasks and wait for completion."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task

        for rt in list(self._running.values()):
            rt.token.request_cancel()

        if self._running:
            tasks = [rt.asyncio_task for rt in self._running.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

        self._running.clear()

    # ── Task submission ──────────────────────────────────────────────

    def submit(self, task_id: str) -> None:
        """Submit a task for background extraction."""
        if task_id in self._running:
            logger.warning("Task %s already running, ignoring submit", task_id)
            return

        # Reset / install completion-event entry BEFORE scheduling the
        # asyncio task so any concurrent ``wait_for_task`` sees the
        # fresh event rather than a stale set-and-forgotten one.
        self._reset_completion_record(task_id)

        token = CancellationToken()
        checkpoint = ExtractionCheckpoint(
            checkpoint_dir=self._settings.checkpoint_dir,
            session_id=task_id,
        )
        asyncio_task = asyncio.create_task(self._run_guarded(task_id, token, checkpoint))
        self._running[task_id] = _RunningTask(
            task_id=task_id,
            asyncio_task=asyncio_task,
            token=token,
            checkpoint=checkpoint,
        )

    def _reset_completion_record(self, task_id: str) -> None:
        """Install a fresh, unset completion event for ``task_id``.

        Called by ``submit`` so a re-submission with the same task id
        doesn't immediately wake up new ``wait_for_task`` callers with a
        stale 'success' from the prior run. Also bounds the registries
        by evicting the oldest entries when the cap is exceeded.
        """
        self._final_state.pop(task_id, None)
        existing = self._completion.get(task_id)
        if existing is None or existing.is_set():
            self._completion[task_id] = asyncio.Event()
        if len(self._completion) > self._completion_max:
            evict = list(self._completion)[: max(1, len(self._completion) // 4)]
            for stale in evict:
                if stale == task_id:
                    continue
                self._completion.pop(stale, None)
                self._final_state.pop(stale, None)

    async def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float,
    ) -> str | None:
        """Block until ``task_id`` reaches a terminal state.

        Returns the final state string (``"success"`` / ``"failed"`` /
        ``"cancelled"``) when the task completes within ``timeout``
        seconds, or ``None`` on timeout. Returns immediately with the
        recorded outcome if the task already finished.

        Used by the chat coordinator to wait on first-touch
        auto-extractions before falling back to manifest-only answers.
        """
        if task_id not in self._running and task_id in self._final_state:
            return self._final_state[task_id]
        ev = self._completion.get(task_id)
        if ev is None:
            return self._final_state.get(task_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._final_state.get(task_id)

    # ── Lifecycle controls ───────────────────────────────────────────

    def pause(self, task_id: str) -> bool:
        """Request pause for a running task. Returns True if signal sent."""
        rt = self._running.get(task_id)
        if rt and rt.token.state == TokenState.RUNNING:
            rt.token.request_pause()
            return True
        return False

    def resume(self, task_id: str) -> bool:
        """Resume a paused task. Returns True if signal sent."""
        rt = self._running.get(task_id)
        if rt and rt.token.state == TokenState.PAUSE_REQUESTED:
            rt.token.resume()
            return True
        return False

    def cancel(self, task_id: str) -> bool:
        """Request cancellation for a running/paused task.

        Sets the cancel flag AND cancels the asyncio task directly so any
        in-flight `claude -p` subprocess (held by proc.communicate()) gets
        killed immediately — not waiting for the next cooperative
        token.check() which only fires at phase boundaries.
        """
        rt = self._running.get(task_id)
        if rt:
            rt.token.request_cancel()
            if not rt.asyncio_task.done():
                rt.asyncio_task.cancel()
            return True
        return False

    def get_state(self, task_id: str) -> str | None:
        """Return the current token state for a task, or None if not tracked."""
        rt = self._running.get(task_id)
        return rt.token.state.value if rt else None

    # ── Internal execution ───────────────────────────────────────────

    async def _run_guarded(
        self, task_id: str, token: CancellationToken, checkpoint: ExtractionCheckpoint
    ) -> None:
        """Wrapper: acquire semaphore, run extraction, handle all outcomes."""
        final_state = "success"
        try:
            await _publish_progress(task_id, 0, "Queued — waiting for slot")
            async with self._semaphore:
                await self._execute_extraction(task_id, token, checkpoint)
        except asyncio.CancelledError:
            final_state = "cancelled"
            await self._update_task_status(task_id, "cancelled")
            await event_bus.publish(
                task_id,
                SSEEvent(event_type="task_failed", data={"error": "Extraction cancelled"}),
            )
        except Exception as exc:
            final_state = "failed"
            logger.exception("Extraction failed for task %s", task_id)
            await self._update_task_status(task_id, "failed", error=str(exc))
            await event_bus.publish(
                task_id,
                SSEEvent(event_type="task_failed", data={"error": str(exc)}),
            )
        finally:
            self._final_state[task_id] = final_state
            ev = self._completion.get(task_id)
            if ev is not None:
                ev.set()
            await event_bus.close(task_id)
            self._running.pop(task_id, None)

    async def _execute_extraction(
        self, task_id: str, token: CancellationToken, checkpoint: ExtractionCheckpoint
    ) -> None:
        """Core extraction logic — clones, runs pipeline, persists results."""

        # 1. Load task + repo info
        task_data, repo_data = await self._load_task_and_repo(task_id)
        repo_id = str(repo_data["id"])
        repo_url = repo_data["url"]
        repo_name = repo_data["name"]

        # 1a. Swap to a repo-scoped checkpoint so progress persists ACROSS
        # tasks on the same repo (the per-task checkpoint created in
        # submit() is only useful inside one run). This is what makes
        # "Resume" meaningful after a failed extraction: the next task
        # picks up the completed phases from the repo's checkpoint.
        repo_checkpoint = ExtractionCheckpoint(
            checkpoint_dir=self._settings.checkpoint_dir,
            session_id=f"repo-{repo_id}",
        )
        checkpoint = repo_checkpoint
        rt = self._running.get(task_id)
        if rt is not None:
            rt.checkpoint = repo_checkpoint

        # 1b. Honor force_full / invalidate_phases from scope.
        # - force_full=true: wipe ALL checkpoint progress → full re-run.
        # - invalidate_phases=[...]: handled below once repo_id is known.
        scope_json = task_data.get("scope_json", {}) or {}
        if scope_json.get("force_full"):
            checkpoint.clear()
            logger.info("force_full=true: cleared checkpoint for task %s", task_id)
        if scope_json.get("invalidate_phases") and not scope_json.get("force_full"):
            try:
                await self._invalidate_phases(
                    repo_id=repo_id,
                    checkpoint=checkpoint,
                    phases=list(scope_json["invalidate_phases"]),
                    task_id=task_id,
                )
            except Exception:
                logger.exception("invalidate_phases failed for task %s", task_id)

        # 2. Update status → running
        await self._update_task_status(task_id, "running")
        await _publish_progress(task_id, 0, "Cloning repository")

        # Wire pause callback to update DB + SSE
        async def on_paused() -> None:
            await self._update_task_status(task_id, "paused")
            progress = (
                max(PHASE_PROGRESS.get(p, 0) for p in checkpoint.get_completed_phases())
                if checkpoint.get_completed_phases()
                else 0
            )
            await _publish_progress(task_id, progress, "Paused")

        token.on_paused = on_paused

        # 3. Clone or update repo. Stable per-repo path so we can reuse it
        # across extractions + keep history for incremental `git diff`.
        from bifrost.adapters.git import GitAdapter

        clone_dir = f"{self._settings.clone_dir}/repo-{repo_id}"
        git = GitAdapter()
        force_full = bool(scope_json.get("force_full"))
        prev_commit: str | None = repo_data.get("last_commit_indexed")

        try:
            if force_full and os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir, ignore_errors=True)
            if not os.path.isdir(clone_dir):
                # Fresh clone. Use shallow for first-time speed; we'll
                # unshallow on the NEXT run if it's incremental.
                await git.clone(repo_url, clone_dir, shallow=True)
            else:
                # Re-use existing clone. Fetch + hard-reset so we pick up
                # new commits while keeping git history intact. Fetch can
                # fail if the old clone is broken (interrupted clone,
                # missing remote, auth drift) — fall back to fresh clone
                # in that case instead of failing the whole extraction.
                try:
                    await git.fetch_all(clone_dir)
                    await git.reset_hard_to_remote(clone_dir)
                except Exception as fetch_exc:
                    logger.warning(
                        "fetch+reset failed for %s (%s); falling back to fresh clone",
                        clone_dir,
                        fetch_exc,
                    )
                    shutil.rmtree(clone_dir, ignore_errors=True)
                    await git.clone(repo_url, clone_dir, shallow=True)
        except Exception as exc:
            raise RuntimeError(f"Failed to clone/fetch {repo_url}: {exc}") from exc

        # Persist the clone path on the repo row so chat knows where to find it
        await self._update_repo_clone_path(repo_id, clone_dir)
        self._schedule_ownership_sync(repo_id, clone_dir)

        await _publish_progress(task_id, 5, "Repository synced")

        # Record HEAD sha now — we'll stamp Repo.last_commit_indexed with it
        # when extraction succeeds. If it fails, the previous sha is preserved.
        try:
            head_meta = await git.get_metadata(clone_dir)
            current_head = head_meta.get("head_commit", "") or ""
        except Exception:
            logger.exception("Could not read HEAD for %s", clone_dir)
            current_head = ""

        # 4. Determine which files to extract.
        #    - force_full: every tracked file
        #    - first run (no prev_commit): every tracked file
        #    - incremental: diff vs prev_commit, fall back to full if diff fails
        all_files = await git.list_files(clone_dir)
        incremental_files: list[str] | None = None
        if not force_full and prev_commit and prev_commit != current_head:
            try:
                # Need full history for diff. Unshallow on-demand.
                await git.unshallow_if_needed(clone_dir)
                if await git.commit_exists(clone_dir, prev_commit):
                    changes = await git.get_changes_since(clone_dir, prev_commit)
                    # Extract added/modified only — deleted files have nothing
                    # to re-extract (entity cleanup handled separately).
                    incremental_files = [
                        c["path"]
                        for c in changes
                        if c["status"] in ("A", "M") and c["path"] in set(all_files)
                    ]
                    logger.info(
                        "Incremental extraction: %d changed files since %s (of %d total)",
                        len(incremental_files),
                        prev_commit[:8],
                        len(all_files),
                    )
                    # Heuristic: if >50% of the repo changed, just do a full run
                    if len(incremental_files) > len(all_files) / 2:
                        logger.info(
                            "Incremental skipped — %d changed files >50%% of repo; full re-extraction",
                            len(incremental_files),
                        )
                        incremental_files = None
                else:
                    logger.info(
                        "prev_commit %s not reachable after unshallow; full re-extraction",
                        prev_commit[:8],
                    )
            except Exception:
                logger.exception("Incremental diff failed; falling back to full extraction")
                incremental_files = None

        file_paths = incremental_files if incremental_files is not None else all_files

        mode = (
            "incremental"
            if incremental_files is not None
            else ("first-run" if not prev_commit else "full")
        )
        await _publish_progress(task_id, 7, f"{mode}: {len(file_paths)} files to process")

        previews: dict[str, str] = {}
        for fp in file_paths:
            try:
                content = await git.read_file(clone_dir, fp)
                previews[fp] = content[:200]
            except Exception:
                pass

        # 5. Create cost tracker with SSE broadcast
        budget = task_data.get("budget_json", {}).get("max_cost_usd")

        # Flush-every-N knob. 5 records × ~30s/call ≈ 2.5 min cadence,
        # so /spend updates every ~couple of minutes during a long run.
        FLUSH_EVERY_N_RECORDS = 5

        async def _flush_cost_incrementally(tracker) -> None:  # type: ignore[no-untyped-def]
            try:
                async with self._session_factory() as s:
                    await tracker.flush_new(s)
                    await s.commit()
            except Exception:
                # Never fail the extraction on a DB write hiccup — we'll
                # retry on the next flush or at task completion.
                logger.exception("Incremental cost flush failed (non-fatal)")

        def _broadcast_cost(rec, tracker) -> None:  # type: ignore[no-untyped-def]
            asyncio.create_task(
                event_bus.publish(
                    task_id,
                    SSEEvent(
                        event_type="cost_update",
                        data={
                            "total_cost_usd": round(tracker.total_cost, 4),
                            "input_tokens": tracker.total_input_tokens,
                            "output_tokens": tracker.total_output_tokens,
                            "cache_read_tokens": tracker.total_cache_read_tokens,
                            "calls": len(tracker.records),
                            "per_phase_cost": {
                                k: round(v, 4) for k, v in tracker.per_phase_cost().items()
                            },
                            "per_phase_calls": tracker.per_phase_calls(),
                            "last_phase": rec.phase,
                            "last_model": rec.model,
                            "last_cost": round(rec.cost_usd, 4),
                        },
                    ),
                )
            )
            # Flush to llm_cost_log every N records so /spend shows live burn
            unpersisted = len(tracker.records) - tracker._persisted_count
            if unpersisted >= FLUSH_EVERY_N_RECORDS:
                asyncio.create_task(_flush_cost_incrementally(tracker))

        cost_tracker = CostTracker(
            task_id=task_id,
            repo_id=repo_id,
            budget=float(budget) if budget else None,
            on_record=_broadcast_cost,
        )

        # 6. Build read_file function
        async def read_file(path: str) -> str:
            from bifrost.extraction.secrets import SecretScanner

            content = await git.read_file(clone_dir, path)
            scanner = SecretScanner()
            return scanner.scan_and_redact(content).redacted_content

        # 7. Phase-complete callback
        async def on_phase_complete(phase: str) -> None:
            checkpoint.mark_phase_complete(phase)
            await _publish_phase_complete(task_id, phase)

        # 8. Run pipeline (real or mock)
        if self._settings.extraction_dry_run:
            output = await _mock_extraction(
                file_paths=file_paths,
                repo_name=repo_name,
                repo_id=repo_id,
                on_phase_complete=on_phase_complete,
                cancellation_token=token,
            )
        else:
            # Verify Claude CLI auth before running — fail fast with clear message
            try:
                auth_status = await verify_claude_cli_auth(self._settings)
                logger.info(
                    "Claude CLI auth OK: %s (%s, %s)",
                    auth_status.email,
                    auth_status.org_name,
                    auth_status.subscription_type,
                )
            except ClaudeCliNotAuthenticated as exc:
                await self._update_task_status(
                    task_id,
                    "failed",
                    error=f"Claude CLI not authenticated: {exc}",
                )
                await event_bus.publish(
                    task_id,
                    SSEEvent(
                        event_type="task_failed",
                        data={"error": "Claude CLI not authenticated. Run `claude auth login`."},
                    ),
                )
                return

            client = ClaudeLlmClient(self._settings)

            async def on_progress(pct: int, phase: str) -> None:
                await _publish_progress(task_id, pct, phase)

            # Incremental persistence — after each phase's entities are
            # extracted, upsert them so the Dashboard's entity counts
            # update live. Non-fatal: we swallow exceptions so the pipeline
            # isn't derailed by a DB hiccup.
            async def on_partial_output(phase: str, output) -> None:  # type: ignore[no-untyped-def]
                try:
                    await self._persist_results(output, repo_id, repo_name)
                except Exception:
                    logger.exception(
                        "Incremental persist after phase %s failed (non-fatal)",
                        phase,
                    )

            # CRITICAL: pass repo_NAME, not the UUID, as ``repo_id``.
            # The pipeline uses this value to construct URNs in
            # Phase G (relationships), Phase H (knowledge index),
            # and Phase I (confidence). Entity rows are stored with
            # ``urn:bifrost:repo/{repo_name}/component/{name}`` (see
            # the persist loops below at line 965+); if Phase G
            # generates URNs with the UUID instead, every
            # ``get_entity_by_urn`` lookup at line 1035 returns
            # None and the relationship is silently dropped at the
            # ``if source and target`` check on line 1037. A URN-segment
            # mismatch here leaves deep-indexed repos with zero edges.
            pipeline = ExtractionPipeline(
                client=client,
                repo_id=repo_name,
                triage_model=self._settings.triage_model,
                extraction_model=self._settings.extraction_model,
                on_phase_complete=on_phase_complete,
                on_progress=on_progress,
                cost_tracker=cost_tracker,
                on_partial_output=on_partial_output,
            )
            # Honour per-task max_files override from scope_json
            max_files = task_data.get("scope_json", {}).get("max_files")
            output = await pipeline.run(
                file_paths=file_paths,
                read_file=read_file,
                file_previews=previews,
                cancellation_token=token,
                resume_checkpoint=checkpoint,
                max_extraction_files=int(max_files) if max_files else None,
            )

        # 9. Persist results
        await _publish_progress(task_id, 95, "Persisting results")
        counts = await self._persist_results(output, repo_id, repo_name)

        # 10. Persist cost records
        if cost_tracker.records:
            async with self._session_factory() as session:
                await cost_tracker.persist(session)
                await session.commit()

        # 11. Update repo freshness + stamp the HEAD sha we extracted.
        # Next incremental run diffs against this sha.
        await self._update_repo_freshness(
            repo_id=repo_id,
            files_analyzed=output.files_analyzed,
            counts=counts,
            checkpoint_phase="I",
        )
        if current_head:
            await self._update_repo_last_commit(repo_id, current_head)

        # 12. Mark task complete (include error count if any)
        if output.errors:
            counts["errors"] = len(output.errors)
            counts["partial"] = True
        await self._update_task_status(task_id, "delivered", result=counts)
        await _publish_progress(task_id, 100, "Complete")
        await event_bus.publish(
            task_id,
            SSEEvent(event_type="task_complete", data=counts),
        )

        # 13. Auto-flag any repo-scoped handover docs as stale.
        # The underlying entity graph just moved, so any cached handover
        # narrative is no longer guaranteed accurate. Best-effort only —
        # staleness flagging must NEVER break extraction. On failure: leave
        # the previous (now slightly stale) handover alone and log; the UI's
        # Refetch + manual Regenerate buttons remain a working fallback.
        try:
            await self._mark_handover_docs_stale(repo_name)
        except Exception:
            logger.exception(
                "Failed to flag handover docs stale for repo %s (non-fatal)",
                repo_name,
            )

        # 14. Keep the clone on disk so chat + re-extractions can use it.
        #     Future re-extractions will `rm -rf` this path and re-clone above.

    # ── DB helpers (short-lived sessions) ────────────────────────────

    async def _load_task_and_repo(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Load task row + first repo from scope_json.repo_ids."""
        async with self._session_factory() as session:
            from bifrost.storage.models import ExtractionTask as TaskModel
            from bifrost.storage.models import Repo

            task = await session.get(TaskModel, uuid.UUID(task_id))
            if not task:
                raise ValueError(f"Task {task_id} not found")

            task_data = {
                "id": task.id,
                "scope_json": task.scope_json,
                "budget_json": task.budget_json,
            }

            repo_ids = task.scope_json.get("repo_ids", [])
            if not repo_ids:
                raise ValueError(f"Task {task_id} has no repo_ids in scope_json")

            repo = await session.get(Repo, uuid.UUID(repo_ids[0]))
            if not repo:
                raise ValueError(f"Repo {repo_ids[0]} not found")

            # last_commit_indexed lives on repo_freshness, not on the repo
            # row. Load it via an explicit fetch so incremental mode works.
            from sqlalchemy import select as _select

            from bifrost.storage.models import RepoFreshness

            freshness_row = (
                await session.execute(
                    _select(RepoFreshness.last_commit_indexed).where(
                        RepoFreshness.repo_id == repo.id
                    )
                )
            ).scalar_one_or_none()

            repo_data = {
                "id": repo.id,
                "url": repo.url,
                "name": repo.name,
                "urn": repo.urn,
                "last_commit_indexed": freshness_row,
            }

        return task_data, repo_data

    async def _update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Update task status in a short-lived session."""
        async with self._session_factory() as session:
            from bifrost.storage.models import ExtractionTask as TaskModel

            task = await session.get(TaskModel, uuid.UUID(task_id))
            if task is None:
                raise LookupError(f"Task {task_id} not found; cannot set status {status!r}")
            task.status = status
            if error:
                task.error_message = error
            if result:
                task.result_json = result
            await session.commit()

        logger.info("Task %s → %s", task_id, status)

    async def _persist_results(
        self,
        output: ExtractionOutput,
        repo_id: str,
        repo_name: str,
    ) -> dict[str, int]:
        """Map extraction output → Entity/Relationship rows. Returns counts."""
        from bifrost.storage.repository import EntityRepository

        counts = {
            "components": 0,
            "flows": 0,
            "dependencies": 0,
            "relationships": 0,
        }

        async with self._session_factory() as session:
            repo = EntityRepository(session)
            repo_uuid = uuid.UUID(repo_id)

            # Components → entity_type="component"
            for comp in output.components:
                urn = f"urn:bifrost:repo/{repo_name}/component/{comp.component_name}"
                await repo.upsert_entity(
                    entity_type="component",
                    urn=urn,
                    name=comp.component_name,
                    qualified_name=f"{comp.source_file}::{comp.component_name}",
                    scope_repo_id=repo_uuid,
                    entity_metadata={
                        "component_type": comp.component_type,
                        "purpose": comp.purpose,
                        "source_file": comp.source_file,
                        "exported_interfaces": comp.exported_interfaces,
                        "internal_dependencies": comp.internal_dependencies,
                        "known_gaps": comp.known_gaps,
                    },
                    freshness_state="fresh",
                )
                counts["components"] += 1

            # Flows → entity_type="flow"
            for flow in output.flows:
                urn = f"urn:bifrost:repo/{repo_name}/flow/{flow.flow_name}"
                await repo.upsert_entity(
                    entity_type="flow",
                    urn=urn,
                    name=flow.flow_name,
                    qualified_name=flow.flow_name,
                    scope_repo_id=repo_uuid,
                    entity_metadata={
                        "flow_type": flow.flow_type,
                        "segments": [
                            {
                                "component_name": s.component_name,
                                "segment_type": s.segment_type,
                                "description": s.description,
                                "order": s.order,
                            }
                            for s in flow.segments
                        ],
                        "known_gaps": flow.known_gaps,
                    },
                    freshness_state="fresh",
                )
                counts["flows"] += 1

            # Dependencies → entity_type="dependency"
            for dep in output.dependencies:
                urn = (
                    f"urn:bifrost:repo/{repo_name}/dependency/{dep.source_name}->{dep.target_name}"
                )
                await repo.upsert_entity(
                    entity_type="dependency",
                    urn=urn,
                    name=f"{dep.source_name} → {dep.target_name}",
                    qualified_name=f"{dep.source_name}::{dep.target_name}",
                    scope_repo_id=repo_uuid,
                    entity_metadata={
                        "dep_type": dep.dep_type,
                        "discovery_method": dep.discovery_method,
                        "description": dep.description,
                    },
                    freshness_state="fresh",
                )
                counts["dependencies"] += 1

            await session.commit()

        # Relationships — need entity IDs, so separate session after entities committed
        async with self._session_factory() as session:
            repo = EntityRepository(session)

            unresolved_relationships: list[tuple[str, str, str]] = []
            for rel in output.relationships:
                source = await repo.get_entity_by_urn(rel.source_urn)
                target = await repo.get_entity_by_urn(rel.target_urn)
                if source and target:
                    await repo.upsert_relationship(
                        source_id=source.id,
                        target_id=target.id,
                        rel_type=rel.relationship_type,
                        rel_metadata={
                            "description": rel.description,
                            "evidence": rel.evidence,
                        },
                    )
                    counts["relationships"] += 1
                else:
                    # Capture for diagnostics — a silent drop here was
                    # the root cause of an earlier missing-edges
                    # bug (URN segment mismatch between Phase G's
                    # prompt and the stored entity URNs). Logging this
                    # as a WARNING means any future regression
                    # surfaces immediately in the worker logs instead
                    # of producing a hollow knowledge graph that only
                    # shows up downstream as "graph retrieval depth=0".
                    unresolved_relationships.append(
                        (
                            rel.source_urn,
                            rel.target_urn,
                            "source_missing" if not source else "target_missing",
                        )
                    )
            if unresolved_relationships:
                # Log a sample so the URN format is visible without
                # blowing up the log on a 1000-edge repo.
                sample = unresolved_relationships[:3]
                logger.warning(
                    "Dropped %d/%d relationships during persist for "
                    "repo %s — entity URN lookup returned None. "
                    "Sample: %s",
                    len(unresolved_relationships),
                    len(output.relationships),
                    repo_name,
                    sample,
                )

            # Confidence scores → update existing entities
            for score in output.confidence_scores:
                entity = await repo.get_entity_by_urn(score.entity_urn)
                if entity:
                    entity.confidence = score.confidence_score
                    entity.confidence_level = score.confidence_level

            # Phase J: persist repo summary (description) if generated
            if output.repo_summary:
                repo_obj = await repo.get_repo(repo_uuid)
                if repo_obj:
                    repo_obj.description = output.repo_summary

            await session.commit()

        logger.info(
            "Persisted: %d components, %d flows, %d deps, %d rels",
            counts["components"],
            counts["flows"],
            counts["dependencies"],
            counts["relationships"],
        )

        # Kick off embedding backfill for the entities we just persisted.
        # Cheap + runs in the background — don't block task completion.
        try:
            import asyncio as _asyncio

            from bifrost.research.embed_backfill import backfill_entity_embeddings

            async def _embed_new() -> None:
                try:
                    r = await backfill_entity_embeddings(
                        session_factory=self._session_factory,
                        force=False,
                    )
                    logger.info(
                        "Post-extract embeddings: %d/%d entities (errors=%d)",
                        r.embedded,
                        r.total_candidates,
                        r.errors,
                    )
                except Exception:
                    logger.exception("post-extract embedding failed (non-fatal)")

            _asyncio.create_task(_embed_new())
        except Exception:
            logger.exception("scheduling post-extract embedding failed (non-fatal)")

        return counts

    async def _mark_handover_docs_stale(self, repo_name: str) -> None:
        """Flag any fresh handover docs scoped to this repo as stale.

        a successful re-extraction means the entity graph moved,
        so any handover narrative built from the previous snapshot is no
        longer guaranteed accurate. Best-effort — caller wraps in try/except.
        """
        from bifrost.storage.repository import EntityRepository

        async with self._session_factory() as session:
            repo = EntityRepository(session)
            flagged = await repo.mark_handover_docs_stale_by_repo_name(repo_name)
            await session.commit()
            if flagged:
                logger.info(
                    "Flagged %d handover doc(s) stale for repo %s after re-extraction",
                    flagged,
                    repo_name,
                )

    async def _update_repo_clone_path(self, repo_id: str, clone_dir: str) -> None:
        """Persist the local clone path on the Repo row so chat can use it."""
        from sqlalchemy import update

        from bifrost.storage.models import Repo

        async with self._session_factory() as session:
            await session.execute(
                update(Repo)
                .where(Repo.id == uuid.UUID(repo_id))
                .values(local_clone_path=clone_dir),
            )
            await session.commit()

    def _schedule_ownership_sync(self, repo_id: str, clone_dir: str) -> None:
        """Read the manifest from the fresh clone, then refresh the service graph."""
        from bifrost.manifest.graph_apply import get_graph_refresher, spawn_background
        from bifrost.research.ownership_sync import sync_repo_ownership

        refresher = get_graph_refresher(self._session_factory, self._settings)

        async def _run() -> None:
            await sync_repo_ownership(
                session_factory=self._session_factory,
                repo_id=repo_id,
                clone_path=clone_dir,
                settings=self._settings,
            )
            await refresher.request()

        spawn_background(_run(), what=f"post-clone ownership sync for {repo_id}")

    # Downstream dependency map for phase invalidation.
    # If you invalidate phase X, you must also invalidate everything
    # downstream whose output depends on X's output.
    _PHASE_DEPENDENTS: dict[str, tuple[str, ...]] = {
        # Phase A = triage (file classification); D depends on it, so do
        # E, F, G, H, I transitively.
        "A": ("D", "E", "F", "G", "H", "I"),
        "D": ("E", "F", "G", "H", "I"),
        "E": ("G", "H", "I"),
        "F": ("G", "H", "I"),
        "G": ("H", "I"),
        "H": ("I",),
        "I": (),
    }

    # Which entity types to delete when a phase is invalidated.
    _PHASE_ENTITY_TYPES: dict[str, tuple[str, ...]] = {
        "D": ("component",),
        "E": ("flow",),
        "F": ("dependency",),
        "G": (),  # relationships, handled separately
        "H": (),  # knowledge_index — stored on entities, auto-regenerated
        "I": (),  # confidence — stored on entities, auto-regenerated
    }

    async def _invalidate_phases(
        self,
        *,
        repo_id: str,
        checkpoint: ExtractionCheckpoint,
        phases: list[str],
        task_id: str,
    ) -> None:
        """Clear the named phases (+ downstream dependents) from checkpoint
        and delete their extracted entities from the DB.

        Called when a request passes ``invalidate_phases`` — typical use
        case is after fixing a phase's prompt and wanting to re-run only
        that phase (+ dependents) without redoing expensive earlier phases.
        """
        # Expand to include downstream dependents so we don't leave the
        # knowledge graph in an inconsistent state.
        expanded: set[str] = set(phases)
        for p in phases:
            expanded.update(self._PHASE_DEPENDENTS.get(p, ()))

        logger.info(
            "invalidate_phases: task=%s requested=%s expanded=%s",
            task_id,
            sorted(phases),
            sorted(expanded),
        )

        # 1. Clear checkpoint state for each phase.
        for p in expanded:
            checkpoint.clear_phase(p)

        # 2. Delete the entities those phases produced so they get
        #    regenerated cleanly. Also drop relationships if G is invalidated.
        entity_types_to_delete: set[str] = set()
        delete_relationships = "G" in expanded
        for p in expanded:
            entity_types_to_delete.update(self._PHASE_ENTITY_TYPES.get(p, ()))

        if entity_types_to_delete or delete_relationships:
            from sqlalchemy import delete, select

            from bifrost.storage.models import Entity, EntityRelationship

            repo_uuid = uuid.UUID(repo_id)
            async with self._session_factory() as session:
                if delete_relationships:
                    # Relationships whose source OR target entity lives
                    # in this repo. The subquery collects those entity
                    # ids; we OR across source_id + target_id.
                    repo_entity_ids = (
                        select(Entity.id).where(Entity.scope_repo_id == repo_uuid).scalar_subquery()
                    )
                    await session.execute(
                        delete(EntityRelationship).where(
                            (EntityRelationship.source_id.in_(repo_entity_ids))
                            | (EntityRelationship.target_id.in_(repo_entity_ids))
                        )
                    )
                if entity_types_to_delete:
                    await session.execute(
                        delete(Entity).where(
                            Entity.scope_repo_id == repo_uuid,
                            Entity.entity_type.in_(list(entity_types_to_delete)),
                        )
                    )
                await session.commit()

        logger.info(
            "invalidate_phases: cleared checkpoint for phases %s; deleted entity_types=%s rels=%s",
            sorted(expanded),
            sorted(entity_types_to_delete),
            delete_relationships,
        )

    async def _update_repo_last_commit(self, repo_id: str, head_sha: str) -> None:
        """Record the HEAD sha of a successful extraction on repo_freshness.
        Next incremental run diffs against this to decide which files changed."""
        from sqlalchemy import update

        from bifrost.storage.models import RepoFreshness

        async with self._session_factory() as session:
            await session.execute(
                update(RepoFreshness)
                .where(RepoFreshness.repo_id == uuid.UUID(repo_id))
                .values(last_commit_indexed=head_sha),
            )
            await session.commit()

    async def _update_repo_freshness(
        self,
        repo_id: str,
        files_analyzed: int,
        counts: dict[str, int],
        checkpoint_phase: str,
    ) -> None:
        """Update the RepoFreshness row after extraction completes."""
        from bifrost.storage.repository import EntityRepository

        async with self._session_factory() as session:
            repo = EntityRepository(session)
            await repo.upsert_repo_freshness(
                repo_id=uuid.UUID(repo_id),
                index_status="complete",
                freshness_state="fresh",
                checkpoint_phase=checkpoint_phase,
                files_analyzed=files_analyzed,
                component_count=counts.get("components", 0),
                flow_count=counts.get("flows", 0),
                dependency_count=counts.get("dependencies", 0),
            )
            await session.commit()

    # ── Heartbeat ────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat events to all running tasks."""
        from datetime import datetime

        while True:
            try:
                await asyncio.sleep(15)
                for task_id in list(self._running.keys()):
                    await event_bus.publish(
                        task_id,
                        SSEEvent(
                            event_type="heartbeat",
                            data={"timestamp": datetime.now(UTC).isoformat()},
                        ),
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug("Heartbeat error", exc_info=True)


# ── Module-level singleton ───────────────────────────────────────────

_worker: ExtractionWorker | None = None


def get_worker() -> ExtractionWorker:
    """Return the active extraction worker. Raises if not initialized."""
    if _worker is None:
        raise RuntimeError("ExtractionWorker not initialized — is the API server running?")
    return _worker


def set_worker(worker: ExtractionWorker) -> None:
    """Set the module-level worker singleton (called from lifespan)."""
    global _worker  # noqa: PLW0603
    _worker = worker
