"""Unit tests for scripts/pyright_gate.py's diagnostic filter (pyright itself is not run)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "pyright_gate", Path(__file__).resolve().parent.parent / "scripts" / "pyright_gate.py"
)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _diag(rule: str | None, line0: int = 4, message: str = "boom") -> dict:
    d = {
        "file": "/repo/src/bifrost/x.py",
        "severity": "error",
        "message": message,
        "range": {"start": {"line": line0, "character": 0}, "end": {"line": line0, "character": 1}},
    }
    if rule is not None:
        d["rule"] = rule
    return d


@pytest.mark.parametrize("rule", sorted(gate.GATED_RULES))
def test_gated_rules_are_reported(rule: str) -> None:
    assert gate.find_violations({"generalDiagnostics": [_diag(rule)]}) == [
        f"/repo/src/bifrost/x.py:5:{rule}:boom"
    ]


def test_ungated_and_ruleless_diagnostics_are_ignored() -> None:
    report = {
        "generalDiagnostics": [
            _diag("reportArgumentType"),
            _diag("reportOptionalSubscript"),
            _diag(None, message="syntax error"),
        ]
    }
    assert gate.find_violations(report) == []


def test_multiline_message_is_collapsed_and_only_gated_kept() -> None:
    report = {
        "generalDiagnostics": [
            _diag("reportAttributeAccessIssue", 9, 'Cannot access "rowcount"\n  Attribute unknown'),
            _diag("reportArgumentType"),
        ]
    }
    assert gate.find_violations(report) == [
        '/repo/src/bifrost/x.py:10:reportAttributeAccessIssue:Cannot access "rowcount" Attribute unknown'
    ]


def test_empty_report_has_no_violations() -> None:
    assert gate.find_violations({}) == []


def test_main_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gate, "run_pyright", lambda _a: {"generalDiagnostics": [_diag("reportCallIssue")]}
    )
    assert gate.main([]) == 1
    assert "reportCallIssue" in capsys.readouterr().out

    monkeypatch.setattr(gate, "run_pyright", lambda _a: {"generalDiagnostics": []})
    assert gate.main([]) == 0
