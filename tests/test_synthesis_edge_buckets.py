"""Edge-tier bucketing in the evidence block.

Smoke tests for ``_render_evidence_block`` in
``bifrost.research.coordinator``. The synthesizer relies on the
block being split into three tier sections so it can pick hedging
tone per-claim. These tests exercise:

  * mixed-tier input → three sections, correct counts per bucket
  * all-T1 input → only High-confidence section appears
  * all-T5 input → only Low-confidence section appears
  * cross-repo edges sort first WITHIN their bucket
  * the prompt header sentence describes the new structure
"""

from __future__ import annotations

from bifrost.research.coordinator import _render_evidence_block
from bifrost.research.repo_agent import EntitySnippet, GraphEdge


def _entity(
    urn: str,
    name: str,
    *,
    out: list[GraphEdge] | None = None,
    inb: list[GraphEdge] | None = None,
) -> EntitySnippet:
    return EntitySnippet(
        name=name,
        entity_type="component",
        purpose=f"purpose for {name}",
        source_file=f"src/{name}.py",
        id=name + "-id",
        urn=urn,
        outbound=out or [],
        inbound=inb or [],
    )


def _edge(
    other_urn: str,
    *,
    tier: int,
    rel: str = "calls",
    name: str | None = None,
    source_type: str = "code_verified",
) -> GraphEdge:
    return GraphEdge(
        rel_type=rel,
        other_name=name or other_urn.split(":")[-1],
        other_urn=other_urn,
        other_type="component",
        confidence=0.95 - (tier - 1) * 0.15,
        source_type=source_type,
        tier=tier,
    )


# ── Header content ───────────────────────────────────────────────


def test_block_header_describes_three_buckets() -> None:
    """The prompt's intro paragraph must mention the three sections so
    the synthesizer knows what to expect — without this, the LLM may
    just see one big edge dump."""
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[_edge("urn:bifrost:repo/repo-a/component/B", tier=1)],
    )
    block = _render_evidence_block([a])
    assert "Relationships are split into three sections" in block
    assert "High-confidence" in block
    assert "Medium" in block
    assert "Low" in block


# ── Bucket routing ───────────────────────────────────────────────


def test_t1_t2_edges_go_to_high_bucket() -> None:
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[
            _edge("urn:bifrost:repo/repo-a/component/B", tier=1, source_type="code_verified"),
            _edge(
                "urn:bifrost:repo/repo-a/component/C",
                tier=2,
                source_type="manifest",
                rel="depends-on",
            ),
        ],
    )
    block = _render_evidence_block([a])
    assert "### High-confidence edges (T1/T2" in block
    assert "[T1 code]" in block
    assert "[T2 manifest]" in block
    # Medium + Low must NOT appear since no T3/T4/T5 edges exist.
    assert "### Medium-confidence" not in block
    assert "### Low-confidence" not in block


def test_t3_edges_go_to_medium_bucket() -> None:
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[_edge("urn:bifrost:repo/repo-a/component/B", tier=3, source_type="metadata_match")],
    )
    block = _render_evidence_block([a])
    assert "### Medium-confidence edges (T3" in block
    assert "[T3 meta]" in block
    assert "### High-confidence" not in block
    assert "### Low-confidence" not in block


def test_t4_t5_edges_go_to_low_bucket() -> None:
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[
            _edge("urn:bifrost:repo/repo-a/component/B", tier=4, source_type="confluence"),
            _edge("urn:bifrost:repo/repo-a/component/C", tier=5, source_type="fuzzy_match"),
        ],
    )
    block = _render_evidence_block([a])
    assert "### Low-confidence edges (T4/T5" in block
    assert "[T4 conf]" in block
    assert "[T5 fuzzy]" in block
    assert "### High-confidence" not in block
    assert "### Medium-confidence" not in block


def test_mixed_tiers_render_all_three_sections() -> None:
    """Realistic synthesis input has edges across all tiers — make
    sure the block separates them cleanly so the synthesizer can hedge
    appropriately."""
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[
            _edge("urn:bifrost:repo/repo-a/component/B", tier=1, source_type="code_verified"),
            _edge("urn:bifrost:repo/repo-a/component/C", tier=2, source_type="manifest"),
            _edge("urn:bifrost:repo/repo-a/component/D", tier=3, source_type="metadata_match"),
            _edge("urn:bifrost:repo/repo-a/component/E", tier=4, source_type="jira"),
            _edge("urn:bifrost:repo/repo-a/component/F", tier=5, source_type="fuzzy_match"),
        ],
    )
    block = _render_evidence_block([a])
    assert "### High-confidence" in block
    assert "### Medium-confidence" in block
    assert "### Low-confidence" in block
    # Sections must appear in confidence order — high first, low last.
    high_idx = block.index("### High-confidence")
    med_idx = block.index("### Medium-confidence")
    low_idx = block.index("### Low-confidence")
    assert high_idx < med_idx < low_idx


# ── Cross-repo prioritization within bucket ──────────────────────


def test_cross_repo_edges_sort_first_within_bucket() -> None:
    """Cross-repo edges carry product-narrative weight, so they appear
    before in-repo edges within the same tier bucket."""
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[
            # In-repo (same repo prefix)
            _edge("urn:bifrost:repo/repo-a/component/B", tier=1),
            # Cross-repo (different prefix)
            _edge("urn:bifrost:repo/repo-b/component/Z", tier=1),
        ],
    )
    block = _render_evidence_block([a])
    # The "Cross-repo:" subheading exists when at least one cross-repo
    # edge is in the bucket; it must appear before "In-repo:".
    cross_idx = block.find("Cross-repo:")
    in_repo_idx = block.find("In-repo:")
    assert cross_idx >= 0
    assert in_repo_idx >= 0
    assert cross_idx < in_repo_idx
    # And the cross-repo line itself must precede the in-repo line.
    z_idx = block.index("urn:bifrost:repo/repo-b/component/Z")
    b_idx = block.index("urn:bifrost:repo/repo-a/component/B")
    assert z_idx < b_idx


# ── Hedging guidance reaches the synthesizer ─────────────────────


def test_each_bucket_carries_its_hedging_directive() -> None:
    """Each section's italicized note tells the synthesizer how to
    hedge claims supported by that tier. The notes are the
    machine-readable contract for the prompt's HARD RULE 1.5."""
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        out=[
            _edge("urn:bifrost:repo/repo-a/component/B", tier=1, source_type="code_verified"),
            _edge("urn:bifrost:repo/repo-a/component/C", tier=3, source_type="metadata_match"),
            _edge("urn:bifrost:repo/repo-a/component/D", tier=4, source_type="jira"),
        ],
    )
    block = _render_evidence_block([a])
    assert "Cite these with definite language" in block
    assert "appears to" in block or "likely" in block
    assert "documented but not code-verified" in block


# ── Boundary / robustness ────────────────────────────────────────


def test_empty_input_returns_empty_string() -> None:
    assert _render_evidence_block([]) == ""


def test_unknown_tier_falls_into_low_bucket() -> None:
    """Defensive: a malformed edge with no tier (default 5) should
    still land somewhere — the low bucket — never silently disappear."""
    a = _entity(
        "urn:bifrost:repo/repo-a/component/A",
        "A",
        # GraphEdge default tier=5
        out=[
            GraphEdge(
                rel_type="calls",
                other_name="B",
                other_urn="urn:bifrost:repo/repo-a/component/B",
                other_type="component",
            )
        ],
    )
    block = _render_evidence_block([a])
    assert "### Low-confidence" in block
    assert "urn:bifrost:repo/repo-a/component/B" in block
