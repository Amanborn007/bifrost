"""Every test module that reads banned terms must run in the leak-gate workflow.

Those tests skip when LEAK_DENYLIST is unset, so the main CI job never runs them.
A module left out of .github/workflows/leak-gate.yml would skip forever.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "leak-gate.yml"
IMPORTS_LEAK_TERMS = re.compile(
    r"^\s*(?:from tests import leak_terms|from tests\.leak_terms import|import tests\.leak_terms)",
    re.MULTILINE,
)


def test_workflow_runs_every_module_that_imports_leak_terms() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    users = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").glob("test_*.py")
        if IMPORTS_LEAK_TERMS.search(p.read_text(encoding="utf-8"))
    )
    assert users, "no test module imports tests.leak_terms; the matcher is stale"
    missing = [u for u in users if u not in workflow]
    assert missing == [], f"leak-gate.yml does not run: {missing}"
