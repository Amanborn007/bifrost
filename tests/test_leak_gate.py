"""Release gate: no private terms and no real-looking secrets in tracked files.

The term list is never committed. Point LEAK_DENYLIST at a local file with one
term per line (blank lines and lines starting with # are ignored). A term matches
as a case-insensitive substring. Prefix it with "w:" to match only when it is not
embedded in a longer word, so "w:bolt" skips "bolted" but still matches
"bolt.yml" and "raw_bolt". The report names file:line only, so a failing run
does not print the term.

The secret check always runs. It skips the fixtures that feed the secret
scrubber on purpose.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.leak_terms import load_patterns

ROOT = Path(__file__).resolve().parents[1]

SECRET_FIXTURES = {
    "tests/test_extraction_infra.py",
    "tests/test_hardening_regressions.py",
    "tests/test_secret_scrubbing.py",
    "tests/test_subprocess_env.py",
    "tests/test_leak_gate.py",
}

# Files allowed to name a banned term. Keep empty: tests read terms through tests/leak_terms.py.
TERM_GATE_EXEMPT_FILES: set[str] = set()

SECRET_PATTERNS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
]


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    paths = [ROOT / p for p in out.decode().split("\0") if p]
    return [p for p in paths if p.is_file()]


def _lines(path: Path) -> list[str] | None:
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def test_denylist_terms_absent_from_tracked_files() -> None:
    denylist = os.environ.get("LEAK_DENYLIST")
    if not denylist:
        pytest.skip("LEAK_DENYLIST is unset; set it to a local term file to run the term gate")
    path = Path(denylist)
    assert path.is_file(), f"LEAK_DENYLIST points at a missing file: {denylist}"
    patterns = load_patterns(path.read_text(encoding="utf-8"))
    assert patterns, "LEAK_DENYLIST contains no terms"

    hits: list[str] = []
    for f in _tracked_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in TERM_GATE_EXEMPT_FILES:
            continue
        for pat in patterns:
            if pat.search(rel):
                hits.append(f"{rel}:0 (path)")
                break
        lines = _lines(f)
        if lines is None:
            continue
        for n, line in enumerate(lines, 1):
            if any(p.search(line) for p in patterns):
                hits.append(f"{rel}:{n}")
    assert not hits, f"{len(hits)} denylist hit(s):\n" + "\n".join(hits)


def test_no_real_looking_secrets_in_tracked_files() -> None:
    hits: list[str] = []
    for f in _tracked_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in SECRET_FIXTURES:
            continue
        lines = _lines(f)
        if lines is None:
            continue
        for n, line in enumerate(lines, 1):
            if any(p.search(line) for p in SECRET_PATTERNS):
                hits.append(f"{rel}:{n}")
    assert not hits, "secret-shaped strings in tracked files:\n" + "\n".join(hits)


def test_denylist_loader_ignores_comments_and_blanks() -> None:
    pats = load_patterns("# note\n\nAcme-Secret\n")
    assert len(pats) == 1
    assert pats[0].search("see acme-secret here")
    assert not pats[0].search("nothing")


def test_denylist_word_prefix_skips_longer_words() -> None:
    (pat,) = load_patterns("w:bolt")
    assert pat.search("see bolt.yml")
    assert pat.search("raw_bolt column")
    assert not pat.search("a bolted door")
    assert not pat.search("thunderbolts")


def test_term_gate_exemptions_are_tracked_files() -> None:
    tracked = {f.relative_to(ROOT).as_posix() for f in _tracked_files()}
    assert sorted(TERM_GATE_EXEMPT_FILES - tracked) == []
