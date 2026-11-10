"""Checkpoint/resume support for extraction pipelines.

Persists extraction progress after each phase so long-running extractions
survive interruptions. Checkpoint files are written atomically (temp + rename)
to prevent corruption on crash.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical phase ordering — phases are completed in this sequence
PHASE_ORDER = ("A", "B", "C", "D", "E", "F", "G", "H", "I")


class ExtractionCheckpoint:
    """Manages checkpoint state for an extraction session.

    Usage::

        cp = ExtractionCheckpoint(checkpoint_dir="/tmp/bifrost", session_id="abc-123")

        # Check if we can resume
        resume_phase = cp.get_resume_phase()
        if resume_phase:
            print(f"Resuming from phase {resume_phase}")

        # After each phase completes
        cp.mark_phase_complete("A")
        cp.mark_phase_complete("D")

        # Query state
        assert cp.is_phase_complete("A")
        assert not cp.is_phase_complete("E")
    """

    def __init__(self, checkpoint_dir: str, session_id: str) -> None:
        self._dir = Path(checkpoint_dir)
        self._session_id = session_id
        self._file = self._dir / f"{session_id}.json"
        self._state = self._load()

    def _load(self) -> dict:
        """Load existing checkpoint or create empty state."""
        if self._file.exists():
            try:
                with open(self._file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt checkpoint file %s — starting fresh", self._file)
        return {
            "session_id": self._session_id,
            "completed_phases": [],
            "entity_ids": [],
            "started_at": datetime.now(UTC).isoformat(),
            "last_updated": None,
        }

    def _save(self) -> None:
        """Write checkpoint atomically: temp file + rename."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._state["last_updated"] = datetime.now(UTC).isoformat()
        fd, tmp_path = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp", prefix=".checkpoint_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp_path, str(self._file))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def mark_phase_complete(self, phase: str, entity_ids: list[str] | None = None) -> None:
        """Mark a phase as complete and persist checkpoint."""
        if phase not in self._state["completed_phases"]:
            self._state["completed_phases"].append(phase)
        if entity_ids:
            self._state["entity_ids"].extend(entity_ids)
        self._save()
        logger.info("Checkpoint: phase %s complete (session %s)", phase, self._session_id)

    def is_phase_complete(self, phase: str) -> bool:
        """Check if a specific phase has been completed."""
        return phase in self._state["completed_phases"]

    def get_resume_phase(self) -> str | None:
        """Return the next phase to run, or None if all phases are complete."""
        completed = set(self._state["completed_phases"])
        for phase in PHASE_ORDER:
            if phase not in completed:
                return phase
        return None

    def get_completed_phases(self) -> list[str]:
        """Return list of completed phase names in order."""
        return list(self._state["completed_phases"])

    def clear(self) -> None:
        """Wipe all progress. Called when ``force_full=true`` on a re-run.

        Removes the persisted file AND resets the in-memory state so the
        next query returns the fresh defaults. Silent if the file doesn't
        exist — a fresh session has no file yet.
        """
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(self._file))
        self._state = {
            "session_id": self._session_id,
            "completed_phases": [],
            "entity_ids": [],
            "phase_units": {},
            "started_at": datetime.now(UTC).isoformat(),
            "last_updated": None,
        }

    def clear_phase(self, phase: str) -> None:
        """Invalidate a single phase's progress (used by invalidate_phases).

        Removes the phase from ``completed_phases`` AND clears its
        per-unit tracking so it re-runs from scratch on next execution.
        """
        if phase in self._state.get("completed_phases", []):
            self._state["completed_phases"].remove(phase)
        self._state.setdefault("phase_units", {}).pop(phase, None)
        self._save()

    def get_entity_ids(self) -> list[str]:
        """Return all entity IDs stored across checkpoints."""
        return list(self._state["entity_ids"])

    # ── Per-unit (batch / file) tracking ─────────────────────────────
    # For fine-grained resume: Phase D checkpoints after each batch of
    # files, Phase E checkpoints after each file. On resume, skip already-
    # processed units instead of redoing the whole phase.

    def mark_unit_done(self, phase: str, unit_key: str) -> None:
        """Mark a single unit within a phase as done (a batch, a file path)."""
        units = self._state.setdefault("phase_units", {}).setdefault(phase, [])
        if unit_key not in units:
            units.append(unit_key)
            self._save()

    def unit_done(self, phase: str, unit_key: str) -> bool:
        """Check if a unit within a phase was already processed."""
        units = self._state.get("phase_units", {}).get(phase, [])
        return unit_key in units

    def get_done_units(self, phase: str) -> list[str]:
        """Return the list of unit keys completed for a phase."""
        return list(self._state.get("phase_units", {}).get(phase, []))

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def checkpoint_file(self) -> Path:
        return self._file
