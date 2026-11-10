"""Every ``_update_task_status`` call site passes only kwargs the method accepts."""

from __future__ import annotations

import ast
import inspect

from bifrost.extraction import worker


def test_update_task_status_call_sites_match_signature() -> None:
    accepted = set(inspect.signature(worker.ExtractionWorker._update_task_status).parameters)
    tree = ast.parse(inspect.getsource(worker))
    bad: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_update_task_status"
        ):
            for kw in node.keywords:
                if kw.arg is not None and kw.arg not in accepted:
                    bad.append(f"line {node.lineno}: unexpected kwarg {kw.arg!r}")
    assert not bad, bad
