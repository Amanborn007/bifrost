"""Unit tests for the cross-repo flow diagram helpers in
``bifrost.collaboration.handover_generator``.

We don't need a live DB — the helper accepts an ``EntityRepository``
and only touches ``repo._session.execute(stmt)``. A fake session that
returns canned rows exercises every code path: render, no-edges, name
sanitization, rel_type sanitization, and validator-rejection."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from bifrost.collaboration import handover_generator as hg


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.last_stmt = None

    async def execute(self, stmt):  # noqa: ANN001 — opaque stmt object
        self.last_stmt = stmt
        return _FakeResult(self._rows)


class _FakeRepo:
    def __init__(self, rows: list[tuple]) -> None:
        self._session = _FakeSession(rows)


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_imports_are_clean() -> None:
    """Smoke test — module + all public + new helpers import OK."""
    assert callable(hg.generate_domain_handover)
    assert callable(hg.generate_team_handover)
    assert callable(hg.generate_region_handover)
    assert callable(hg._build_cross_repo_flow_diagram)
    assert callable(hg._prepend_cross_repo_diagram)


def test_diagram_render_basic() -> None:
    rows = [
        ("repo-a", "repo-b", "calls", 3),
        ("repo-a", "repo-c", "imports", 2),
        ("repo-b", "repo-c", "depends_on", 1),
    ]
    repo = _FakeRepo(rows)
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(repo, _ids(3)))
    assert diagram is not None
    assert diagram.startswith("flowchart LR")
    # Each repo appears as a node label.
    assert '"repo-a"' in diagram
    assert '"repo-b"' in diagram
    assert '"repo-c"' in diagram
    # Each edge labeled with rel_type and count.
    assert "calls (3)" in diagram
    assert "imports (2)" in diagram
    assert "depends_on (1)" in diagram
    # The generated body validates against the existing sanitizer.
    assert hg._validate_mermaid_block(diagram)


def test_diagram_no_rows_returns_none() -> None:
    repo = _FakeRepo([])
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(repo, _ids(3)))
    assert diagram is None


def test_diagram_empty_repo_ids_returns_none() -> None:
    # No need to call the DB at all when the scope is empty.
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(_FakeRepo([]), []))
    assert diagram is None


def test_diagram_single_repo_returns_none() -> None:
    # A single repo can't have cross-repo edges by construction.
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(_FakeRepo([]), _ids(1)))
    assert diagram is None


def test_diagram_weird_names_still_validate() -> None:
    """Repo names with dots / spaces / slashes should still produce a
    valid mermaid block — the renderer slugifies node ids and quotes
    display labels."""
    rows = [
        ("repo.with.dots", "repo-with-dashes", "calls", 5),
        ("repo with spaces", "repo/with/slash", "consumes", 1),
    ]
    repo = _FakeRepo(rows)
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(repo, _ids(4)))
    assert diagram is not None
    assert hg._validate_mermaid_block(diagram)
    # Display labels preserve the original repo names verbatim.
    assert '"repo with spaces"' in diagram
    assert '"repo/with/slash"' in diagram


def test_diagram_pipe_in_rel_type_is_sanitized() -> None:
    """A rel_type containing ``|`` would break the edge-label fence;
    ensure it's stripped before we render."""
    rows = [("repo-a", "repo-b", "has|pipe", 4)]
    repo = _FakeRepo(rows)
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(repo, _ids(2)))
    assert diagram is not None
    assert hg._validate_mermaid_block(diagram)
    # The literal ``has|pipe`` should NOT appear — the pipe is dropped.
    assert "has|pipe" not in diagram
    assert "haspipe" in diagram


def test_prepend_into_existing_architecture() -> None:
    diagram = (
        'flowchart LR\n    repo_a["repo-a"]\n    repo_b["repo-b"]\n    repo_a -->|calls (3)| repo_b'
    )
    parsed = {"architecture": "The system has three components."}
    hg._prepend_cross_repo_diagram(parsed, diagram)
    arch = parsed["architecture"]
    assert arch.startswith("```mermaid")
    assert "Cross-repo dependencies derived" in arch
    assert "three components." in arch
    # The block should round-trip through the existing sanitizer.
    md = hg._sanitize_mermaid_blocks(arch, parsed)
    assert "```mermaid" in md
    # Sanitizer should NOT have replaced the (valid) block with a
    # table fallback note.
    assert "Architecture diagram not rendered" not in md


def test_prepend_into_empty_architecture() -> None:
    diagram = 'flowchart LR\n    a["a"]\n    b["b"]\n    a -->|x (1)| b'
    parsed: dict = {"architecture": ""}
    hg._prepend_cross_repo_diagram(parsed, diagram)
    arch = parsed["architecture"]
    assert arch.startswith("```mermaid")
    assert arch.rstrip().endswith("relationships._")


def test_diagram_caps_edges_at_top_15() -> None:
    """When the SQL returns the edge cap, the renderer keeps every row
    (the LIMIT is applied at the SQL level; the helper trusts it)."""
    rows = [
        (f"repo_{i}", f"repo_{(i + 1) % 16}", "calls", 16 - i)
        for i in range(hg._CROSS_REPO_DIAGRAM_EDGE_CAP)
    ]
    repo = _FakeRepo(rows)
    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(repo, _ids(16)))
    assert diagram is not None
    # Count the edge lines (those containing ``-->|``).
    edge_lines = [ln for ln in diagram.split("\n") if "-->|" in ln]
    assert len(edge_lines) == hg._CROSS_REPO_DIAGRAM_EDGE_CAP


def test_diagram_db_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SQL exception should be swallowed (logged) and return None
    so the handover generation continues without the diagram."""

    class _BoomSession:
        async def execute(self, stmt):  # noqa: ANN001
            raise RuntimeError("simulated DB failure")

    class _BoomRepo:
        _session = _BoomSession()

    diagram = asyncio.run(hg._build_cross_repo_flow_diagram(_BoomRepo(), _ids(2)))
    assert diagram is None
