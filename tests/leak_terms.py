"""Banned-term list shared by the leak gate and the tests that assert a name is absent.

LEAK_DENYLIST points at a local file kept outside the repo, one term per line.
Blank lines and lines starting with # are ignored. A term matches as a
case-insensitive substring; the prefix "w:" matches only when the term is not
embedded in a longer word. A test that needs a banned name calls matches() or
terms(), so no term is spelled in a tracked file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_SKIP_REASON = "LEAK_DENYLIST is unset; set it to a local term file to run the term gate"


def load_terms(raw: str) -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for line in raw.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if term.startswith("w:"):
            literal = term[2:].strip()
            out.append(
                (literal, re.compile(rf"(?<![A-Za-z]){re.escape(literal)}(?![A-Za-z])", re.I))
            )
        else:
            out.append((term, re.compile(re.escape(term), re.IGNORECASE)))
    return out


def load_patterns(raw: str) -> list[re.Pattern[str]]:
    return [pat for _, pat in load_terms(raw)]


def _configured() -> list[tuple[str, re.Pattern[str]]]:
    denylist = os.environ.get("LEAK_DENYLIST")
    if not denylist:
        pytest.skip(_SKIP_REASON)
    path = Path(denylist)
    assert path.is_file(), f"LEAK_DENYLIST points at a missing file: {denylist}"
    loaded = load_terms(path.read_text(encoding="utf-8"))
    assert loaded, "LEAK_DENYLIST contains no terms"
    return loaded


def patterns() -> list[re.Pattern[str]]:
    """Compiled patterns from LEAK_DENYLIST; skips the calling test when it is unset."""
    return [pat for _, pat in _configured()]


def terms() -> list[str]:
    """Literal terms from LEAK_DENYLIST, without any w: prefix; skips when it is unset."""
    return [literal for literal, _ in _configured()]


def matches(text: str) -> bool:
    """True when text carries any banned term; skips the calling test when it is unset."""
    return any(pat.search(text) for pat in patterns())
