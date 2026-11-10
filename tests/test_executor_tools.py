"""Pin the ``--tools`` argument list passed to ``claude -p``.

The agent's tool grants are the second-line defence behind verify_cmd validation and prompt-injection boundaries.
If a future refactor accidentally widens the allowlist (e.g. drops
the per-command Bash scoping), this test fails so the regression is
visible.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR = REPO_ROOT / "src" / "bifrost" / "actions" / "executor.py"
VERIFIER = REPO_ROOT / "src" / "bifrost" / "actions" / "verifier.py"
DEBUGGER = REPO_ROOT / "src" / "bifrost" / "actions" / "debugger.py"


def _tools_block(path: Path) -> str:
    """Extract the contents of the ``--tools`` argument literal."""
    text = path.read_text()
    # Match the multi-line concatenation: "--tools", ( ... ),
    m = re.search(
        r'"--tools",\s*\(\s*((?:"[^"]+"\s*)+)\s*\)',
        text,
    )
    if m is None:
        # Fall back to single-line form
        m = re.search(r'"--tools",\s*"([^"]+)"', text)
        return m.group(1) if m else ""
    # Concatenate all the quoted parts.
    parts = re.findall(r'"([^"]+)"', m.group(1))
    return "".join(parts)


class TestExecutorTools:
    """Executor agent — Edit/Write allowed, Bash narrowly scoped."""

    def test_has_read_glob_grep(self) -> None:
        s = _tools_block(EXECUTOR)
        for tool in ("Read", "Glob", "Grep"):
            assert tool in s

    def test_has_write_and_edit(self) -> None:
        s = _tools_block(EXECUTOR)
        assert "Write" in s
        assert "Edit" in s

    def test_bash_only_with_explicit_scope(self) -> None:
        """Bare ``Bash`` (no parens) would defeat the allowlist."""
        s = _tools_block(EXECUTOR)
        # Allow Bash(prefix *) but reject standalone "Bash" or "Bash *".
        assert re.search(r"\bBash\b(?!\()", s) is None, (
            f"executor --tools allows unscoped Bash: {s!r}"
        )
        assert "Bash(pytest *)" in s

    def test_bash_allowlist_known_prefixes(self) -> None:
        s = _tools_block(EXECUTOR)
        for prefix in (
            "Bash(pytest *)",
            "Bash(uv *)",
            "Bash(python *)",
            "Bash(npm test)",
            "Bash(cargo test)",
            "Bash(git *)",
        ):
            assert prefix in s, f"missing allowlist entry: {prefix}"

    @pytest.mark.parametrize(
        "forbidden_pattern",
        [
            r"Bash\(\*\)",  # wildcard Bash
            r"Bash\(curl",  # curl never legit
            r"Bash\(pip",  # pip install would let agents add backdoors
            r"Bash\(npm install",  # ditto npm
        ],
    )
    def test_does_not_contain_dangerous_patterns(
        self,
        forbidden_pattern: str,
    ) -> None:
        s = _tools_block(EXECUTOR)
        assert re.search(forbidden_pattern, s) is None, (
            f"executor --tools must not match {forbidden_pattern}: {s!r}"
        )


class TestVerifierTools:
    """Verifier is intentionally read-only."""

    def test_no_write_or_edit(self) -> None:
        s = _tools_block(VERIFIER)
        assert "Write" not in s, f"verifier must not have Write: {s!r}"
        assert "Edit" not in s, f"verifier must not have Edit: {s!r}"

    def test_has_read_glob_grep(self) -> None:
        s = _tools_block(VERIFIER)
        for tool in ("Read", "Glob", "Grep"):
            assert tool in s

    def test_bash_narrowly_scoped(self) -> None:
        s = _tools_block(VERIFIER)
        assert re.search(r"\bBash\b(?!\()", s) is None
        assert "Bash(pytest *)" in s


class TestDebuggerTools:
    """Debugger is read-only diagnostic."""

    def test_no_write_or_edit(self) -> None:
        s = _tools_block(DEBUGGER)
        assert "Write" not in s
        assert "Edit" not in s

    def test_bash_narrowly_scoped(self) -> None:
        s = _tools_block(DEBUGGER)
        assert re.search(r"\bBash\b(?!\()", s) is None
