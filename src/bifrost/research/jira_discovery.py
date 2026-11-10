"""Discover a repo's *real* Jira project key from its cloned source.

Why: team-derived keys (the manifest team mapped through the conventions
config) are often wrong. The authoritative signal is how the repo ACTUALLY
references Jira — via `<jira host>/browse/<KEY>-<N>` URLs in README, PR
templates, workflows, and code comments.

We count project-key mentions across the cloned repo and pick the most
referenced one (with a minimum-support threshold so a single stray ref
doesn't override).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path

from bifrost.cli.config import get_settings
from bifrost.cli.config_sections import JiraSourceConfig

logger = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def _browse_re(host_pattern: str) -> re.Pattern[str]:
    return re.compile(rf"{host_pattern}/browse/([A-Z][A-Z0-9]{{1,9}})-\d+")


# Files we scan for references. Everything else is excluded to keep the
# scan fast and avoid false positives from vendored test data.
_SCAN_INCLUDES = (
    "README.md",
    "README.rst",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    ".github/CODEOWNERS",
    "CODEOWNERS",
)
_SCAN_GLOB_INCLUDES = (
    ".github/PULL_REQUEST_TEMPLATE/*.md",
    ".github/ISSUE_TEMPLATE/*.md",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "docs/*.md",
)

# Common "noise" tokens that look like Jira keys but aren't project keys.
# We can expand this over time based on false-positive observation.
_NOISE_KEYS = frozenset(
    {
        "XXX",  # placeholder in PR templates
        "PROJ",  # example in docs
        "TODO",  # TODO-123 comments
        "NOTE",
        "FIXME",
        "HACK",
        "CVE",
        "PR",
    },
)


def discover_jira_project_key(
    clone_path: str, *, jira: JiraSourceConfig | None = None
) -> tuple[str | None, dict[str, int]]:
    """Return (best_guess_key, all_counts) for a cloned repo.

    If nothing meaningful is found (total hits < 2 OR winning key is in
    the noise list), returns (None, counts).
    """
    browse_re = _browse_re((jira or get_settings().sources.jira).host_pattern())
    base = Path(clone_path)
    if not base.is_dir():
        return None, {}

    # Gather candidate text — literal files first, then globs.
    texts: list[str] = []
    for rel in _SCAN_INCLUDES:
        p = base / rel
        if p.is_file():
            texts.append(_safe_read(p))
    for glob in _SCAN_GLOB_INCLUDES:
        for p in base.glob(glob):
            if p.is_file():
                texts.append(_safe_read(p))

    # Also scan tracked source files. The extension list covers the
    # languages, build files, config formats and infrastructure code a
    # monorepo holds, since Jira URLs also show up in notes and CI configs.
    source_extensions = (
        ".java",
        ".kt",
        ".groovy",
        ".scala",
        ".py",
        ".pyi",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rb",
        ".rs",
        ".swift",
        ".md",
        ".rst",
        ".adoc",
        ".txt",
        ".yml",
        ".yaml",
        ".json",
        ".properties",
        ".xml",
        ".gradle",
        ".sbt",
        ".tf",
        ".hcl",
    )
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True,
            cwd=str(base),
            timeout=20,
        )
        tracked = [
            x
            for x in result.stdout.decode("utf-8", "replace").split("\0")
            if x and x.lower().endswith(source_extensions)
        ]
        # Cap at 3000 files — enough for most repos.
        # File size cap raised to 500KB to include release notes / large READMEs.
        for rel in tracked[:3000]:
            p = base / rel
            try:
                if p.is_file() and p.stat().st_size < 500_000:
                    texts.append(_safe_read(p))
            except OSError:
                continue
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git ls-files scan for jira keys skipped (%s)", exc)

    counts: Counter[str] = Counter()
    for text in texts:
        for m in browse_re.finditer(text):
            key = m.group(1)
            if key in _NOISE_KEYS:
                continue
            counts[key] += 1

    all_counts = dict(counts)

    if not counts:
        return None, all_counts

    top_key, top_count = counts.most_common(1)[0]
    # Need at least 2 refs to be confident — a single stray mention isn't enough
    if top_count < 2:
        return None, all_counts
    return top_key, all_counts


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
