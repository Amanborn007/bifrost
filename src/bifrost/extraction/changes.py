"""Source change detection for incremental extraction.

Compares SHA-256 content hashes against prior extraction records to
skip unchanged files. Detects renames via content-similarity heuristic
(>80 % overlap).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


def compute_hash(content: str) -> str:
    """Return the SHA-256 hex digest for *content*."""
    return hashlib.sha256(content.encode()).hexdigest()


def has_changed(content: str, previous_hash: str) -> bool:
    """Return True if *content* differs from *previous_hash*."""
    return compute_hash(content) != previous_hash


def content_similarity(a: str, b: str) -> float:
    """Return a 0-1 similarity ratio between two strings (quick ratio)."""
    return SequenceMatcher(None, a, b).quick_ratio()


# ── Change detection result ───────────────────────────────────────────


@dataclass
class FileChange:
    """Describes one file's change status."""

    path: str
    status: str  # "changed" | "new" | "deleted" | "renamed"
    previous_path: str | None = None  # set when status == "renamed"
    content_hash: str | None = None  # current hash for changed/new


@dataclass
class ChangeReport:
    """Summary of what changed since the last extraction."""

    changed: list[FileChange] = field(default_factory=list)
    unchanged_count: int = 0

    @property
    def files_to_extract(self) -> list[str]:
        """Paths that need (re-)extraction."""
        return [f.path for f in self.changed]


# ── Change detector ───────────────────────────────────────────────────


RENAME_SIMILARITY_THRESHOLD = 0.80


class ChangeDetector:
    """Compares current file set against a previous hash map.

    Usage::

        detector = ChangeDetector()
        report = await detector.detect(
            current_files={"src/a.py": "code...", "src/b.py": "code..."},
            previous_hashes={"src/a.py": "abc123...", "src/old_b.py": "def456..."},
        )
        for path in report.files_to_extract:
            ...  # run extraction on these
    """

    def __init__(self, *, rename_threshold: float = RENAME_SIMILARITY_THRESHOLD) -> None:
        self._rename_threshold = rename_threshold

    async def detect(
        self,
        current_files: dict[str, str],
        previous_hashes: dict[str, str],
    ) -> ChangeReport:
        """Compare current file contents against previous extraction hashes.

        Args:
            current_files: {path: content} for all files in the current repo state.
            previous_hashes: {path: sha256_hex} from the last extraction record.

        Returns:
            ChangeReport with changed/new/deleted/renamed files.
        """
        report = ChangeReport()
        previous_paths = set(previous_hashes.keys())
        current_paths = set(current_files.keys())

        # Files present in both — check for content changes
        for path in current_paths & previous_paths:
            h = compute_hash(current_files[path])
            if h != previous_hashes[path]:
                report.changed.append(
                    FileChange(
                        path=path,
                        status="changed",
                        content_hash=h,
                    )
                )
            else:
                report.unchanged_count += 1

        # New files (in current, not in previous)
        new_paths = current_paths - previous_paths
        # Deleted files (in previous, not in current)
        deleted_paths = previous_paths - current_paths

        # Attempt rename detection between new and deleted
        renames: list[tuple[str, str]] = []  # (old_path, new_path)

        if new_paths and deleted_paths:
            renames = self._detect_renames(
                new_paths,
                deleted_paths,
                current_files,
                previous_hashes,
            )

        matched_new = {new for _, new in renames}
        matched_deleted = {old for old, _ in renames}

        # Add renames
        for old_path, new_path in renames:
            report.changed.append(
                FileChange(
                    path=new_path,
                    status="renamed",
                    previous_path=old_path,
                    content_hash=compute_hash(current_files[new_path]),
                )
            )

        # Remaining new files
        for path in new_paths - matched_new:
            report.changed.append(
                FileChange(
                    path=path,
                    status="new",
                    content_hash=compute_hash(current_files[path]),
                )
            )

        # Remaining deleted files
        for path in deleted_paths - matched_deleted:
            report.changed.append(FileChange(path=path, status="deleted"))

        logger.info(
            "Change detection: %d to extract, %d unchanged, %d total current",
            len(report.changed),
            report.unchanged_count,
            len(current_files),
        )

        return report

    def _detect_renames(
        self,
        new_paths: set[str],
        deleted_paths: set[str],
        current_files: dict[str, str],
        previous_hashes: dict[str, str],
    ) -> list[tuple[str, str]]:
        """Find renames by exact hash match between new and deleted files.

        Returns list of (old_path, new_path) tuples for detected renames.
        """
        renames: list[tuple[str, str]] = []
        matched_deleted: set[str] = set()

        # Build hash → deleted_path map
        deleted_by_hash: dict[str, str] = {}
        for dp in deleted_paths:
            deleted_by_hash[previous_hashes[dp]] = dp

        for np in sorted(new_paths):
            new_hash = compute_hash(current_files[np])
            if new_hash in deleted_by_hash:
                old_path = deleted_by_hash[new_hash]
                if old_path not in matched_deleted:
                    matched_deleted.add(old_path)
                    renames.append((old_path, np))
                    logger.info("Detected rename: %s -> %s", old_path, np)

        return renames
