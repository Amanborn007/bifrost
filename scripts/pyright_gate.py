#!/usr/bin/env python3
"""Fail when pyright reports a rule that indicates a runtime crash.

Usage: python scripts/pyright_gate.py [pyright args ...]

With no arguments it checks ``src/bifrost``. Any arguments are passed to
pyright as-is (e.g. ``--pythonpath /path/to/python src/bifrost``).
The other pyright rules are not gated; they are noisy on this codebase.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

# reportPossiblyUnbound is the name in pyright's config docs; the JSON
# output spells it reportPossiblyUnboundVariable.
GATED_RULES = frozenset(
    {
        "reportAttributeAccessIssue",
        "reportCallIssue",
        "reportUndefinedVariable",
        "reportPossiblyUnbound",
        "reportPossiblyUnboundVariable",
        "reportRedeclaration",
        "reportIndexIssue",
    }
)


def find_violations(report: dict[str, Any]) -> list[str]:
    """Return ``file:line:rule:message`` for every gated diagnostic."""
    out: list[str] = []
    for diag in report.get("generalDiagnostics", []):
        rule = diag.get("rule")
        if rule not in GATED_RULES:
            continue
        line = diag["range"]["start"]["line"] + 1
        message = " ".join(str(diag["message"]).split())
        out.append(f"{diag['file']}:{line}:{rule}:{message}")
    return out


def run_pyright(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    # pyright exits 1 whenever it finds errors, so the exit code is not a failure signal;
    # unparseable output is.
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"pyright produced no JSON (exit {proc.returncode}): {proc.stderr or proc.stdout}"
        ) from exc


def main(argv: list[str]) -> int:
    report = run_pyright(argv or ["src/bifrost"])
    violations = find_violations(report)
    for v in violations:
        print(v)
    print(f"pyright gate: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
