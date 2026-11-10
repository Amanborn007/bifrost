"""Every `make <target>` a doc or CLI message tells users to run must exist."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = ["README.md", "RUNNING.md", "CONTRIBUTING.md"]
NAME = r"[a-z][a-z0-9-]*"
MAKE_RE = re.compile(rf"\bmake\s+({NAME}(?:\s*(?:\||&&)\s*make\s+{NAME})*)")
SEP_RE = re.compile(r"\s*(?:\||&&)\s*make\s+|\s+")
SPAN_RE = re.compile(r"`([^`\n]+)`")


def makefile_targets() -> set[str]:
    targets: set[str] = set()
    for line in (ROOT / "Makefile").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_\- ]+):(?!=)", line)
        if m:
            targets.update(m.group(1).split())
    return targets


def _names(span: str) -> set[str]:
    names: set[str] = set()
    for m in MAKE_RE.finditer(span):
        names.update(w for w in SEP_RE.split(m.group(1)) if w)
    return names


def _md_spans(text: str) -> list[str]:
    fenced = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    rest = re.sub(r"```.*?```", "", text, flags=re.S)
    return fenced + SPAN_RE.findall(rest)


def referenced() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def add(span: str, rel: str) -> None:
        for n in _names(span):
            found.setdefault(n, set()).add(rel)

    paths = [ROOT / f for f in DOC_FILES] + sorted((ROOT / "docs").rglob("*.md"))
    for p in paths:
        if p.exists() and p != ROOT / "docs" / "prd.md":
            for span in _md_spans(p.read_text()):
                add(span, str(p.relative_to(ROOT)))
    for p in sorted((ROOT / "src" / "bifrost" / "cli").rglob("*.py")):
        text = p.read_text()
        rel = str(p.relative_to(ROOT))
        for lit in re.findall(r'"([^"\n]*\bmake [^"\n]*)"', text):
            add(lit, rel)
        for span in SPAN_RE.findall(text):
            add(span, rel)
    return found


def test_parser_handles_multi_name_rules():
    assert {"up", "start", "run", "down", "stop", "health", "ping"} <= makefile_targets()


def test_parser_expands_piped_and_chained_targets():
    assert _names("make up && make migrate") == {"up", "migrate"}
    assert _names("make up | make migrate") == {"up", "migrate"}
    assert _names("make logs | grep -i x") == {"logs"}


def test_every_referenced_make_target_exists():
    targets = makefile_targets()
    missing = {n: sorted(f) for n, f in referenced().items() if n not in targets}
    assert not missing, missing


def test_new_targets_are_listed_in_help():
    dev = (ROOT / "scripts" / "dev.sh").read_text()
    for t in ("migrate", "test"):
        assert f"make {t}" in dev
