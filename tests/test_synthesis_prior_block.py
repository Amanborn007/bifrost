"""Synthesis prompt — <prior_research> block + prior-evidence block.

When a follow-up turn fires a full research pass (NEW_SCOPE or
EXTEND_BUNDLE), the prior bundle is folded into the synthesis prompt
as a metadata-only continuity block so the synthesizer can reference
URNs and sources the user has already seen rather than re-deriving
the same findings.

When a hydrated prior subgraph is *also* provided (the
EXTEND_BUNDLE narrow fan-out path), a "## Prior research evidence"
section appears at the bottom with full Evidence blocks per prior
repo so the synthesizer can do cross-turn flow reasoning.

These tests exercise just the prompt builder; full hydration is
tested in test_subgraph_hydrate.py.
"""

from __future__ import annotations

from bifrost.research.bundle_cache import BundleSource, ResearchBundle
from bifrost.research.coordinator import (
    _build_synthesis_user_message,
    _render_prior_evidence_block,
    _render_prior_research_block,
)
from bifrost.research.repo_agent import EntitySnippet, GraphEdge
from bifrost.research.subgraph_hydrate import (
    HydratedRepoSubgraph,
    HydratedSubgraph,
)

_SENTINEL: object = object()


def _make_bundle(
    *,
    urns: object = _SENTINEL,
    sources: object = _SENTINEL,
    repos_used_names: list[str] | None = None,
    last_question: str = "how does orders retry?",
    last_answer: str = "It uses an exponential-backoff queue.",
) -> ResearchBundle:
    """Sentinel-defaulting factory so tests can pass empty lists."""
    return ResearchBundle(
        conversation_id="conv-1",
        last_question=last_question,
        last_answer=last_answer,
        confidence="confirmed",
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=repos_used_names or ["orders"],
        scope_rationale="x",
        subgraph_entity_urns=(
            ["bifrost://repo/orders/component/RetryQueue"] if urns is _SENTINEL else list(urns)  # type: ignore[arg-type]
        ),
        sources=(
            [BundleSource(kind="jira", ref="SHIP-1", title="retry bug")]
            if sources is _SENTINEL
            else list(sources)  # type: ignore[arg-type]
        ),
    )


def test_render_block_includes_question_repos_urns_sources_answer() -> None:
    bundle = _make_bundle(
        urns=["bifrost://repo/orders/component/Retry"],
        sources=[
            BundleSource(kind="jira", ref="SHIP-1", title="retry"),
            BundleSource(kind="confluence", ref="page-9", title="RFC"),
        ],
    )
    block = _render_prior_research_block(bundle)
    assert "<prior_research>" in block
    assert "</prior_research>" in block
    assert "Prior question: how does orders retry?" in block
    assert "Prior repos walked: orders" in block
    assert "bifrost://repo/orders/component/Retry" in block
    assert "SHIP-1" in block
    assert "page-9" in block
    assert "exponential-backoff" in block


def test_render_block_caps_long_urn_list() -> None:
    bundle = _make_bundle(
        urns=[f"bifrost://urn/{i}" for i in range(60)],
    )
    block = _render_prior_research_block(bundle)
    # Helper trims ResearchBundle on save, so 60 → 60 (the cap is
    # applied here too, at the rendering layer).
    assert "(+ " in block and "more)" in block
    assert block.count("bifrost://urn/") <= 31


def test_render_block_caps_long_sources_list() -> None:
    bundle = _make_bundle(
        sources=[BundleSource(kind="jira", ref=f"X-{i}", title=f"t{i}") for i in range(40)],
    )
    block = _render_prior_research_block(bundle)
    assert "(+ " in block and "more)" in block
    # 20 listed + a +N more line.
    rendered_refs = block.count("[jira]")
    assert rendered_refs <= 20


def test_render_block_truncates_huge_prior_answer() -> None:
    bundle = _make_bundle(last_answer="a" * 10000)
    block = _render_prior_research_block(bundle)
    # The block caps at ~1500 chars for the answer.
    assert block.count("a") <= 1600


def test_render_block_handles_no_sources_gracefully() -> None:
    bundle = _make_bundle(sources=[], urns=[])
    block = _render_prior_research_block(bundle)
    assert "(none recorded)" in block


def test_synthesis_user_message_omits_prior_block_when_no_bundle() -> None:
    msg = _build_synthesis_user_message(
        question="What does retry queue mean?",
        bundles=[],  # no per-repo bundles in this test
        prior_bundle=None,
    )
    assert "<prior_research>" not in msg


def test_synthesis_user_message_includes_prior_block_when_bundle_provided() -> None:
    bundle = _make_bundle()
    msg = _build_synthesis_user_message(
        question="What does retry queue mean?",
        bundles=[],
        prior_bundle=bundle,
    )
    assert "<prior_research>" in msg
    assert "Prior question:" in msg
    # Block appears BEFORE the current question header so the
    # synthesizer reads the continuity context first.
    assert msg.index("<prior_research>") < msg.index("Current user question")


def test_synthesis_user_message_keeps_prior_turns_separate_from_bundle() -> None:
    """Both prior_turns_text (chat history) and prior_bundle
    (structured metadata) can coexist — the synth gets two distinct
    context sections."""
    msg = _build_synthesis_user_message(
        question="q",
        bundles=[],
        prior_turns="User: foo\nAssistant: bar",
        prior_bundle=_make_bundle(),
    )
    assert "Prior conversation" in msg
    assert "<prior_research>" in msg
    # Chat history section comes first (older context first).
    assert msg.index("Prior conversation") < msg.index("<prior_research>")


# ── Prior-evidence block (hydrated) ────────────────────────────────


def _make_snippet(
    *,
    urn: str,
    name: str,
    purpose: str = "core component",
    outbound: list[GraphEdge] | None = None,
    inbound: list[GraphEdge] | None = None,
) -> EntitySnippet:
    return EntitySnippet(
        id=f"id-{name}",
        urn=urn,
        name=name,
        entity_type="component",
        purpose=purpose,
        source_file=f"src/{name.lower()}.py",
        outbound=outbound or [],
        inbound=inbound or [],
    )


def _make_hydrated(
    repos: list[HydratedRepoSubgraph],
    *,
    missing_urns: list[str] | None = None,
) -> HydratedSubgraph:
    total_e = sum(len(r.entities) for r in repos)
    total_x = sum(r.edge_count for r in repos)
    return HydratedSubgraph(
        repos=repos,
        total_entities=total_e,
        total_edges=total_x,
        missing_urns=missing_urns or [],
    )


def test_prior_evidence_block_renders_per_repo_section() -> None:
    e1 = _make_snippet(urn="urn:test:RetryQueue", name="RetryQueue")
    e2 = _make_snippet(urn="urn:test:GatewayClient", name="GatewayClient")
    sub = _make_hydrated(
        [
            HydratedRepoSubgraph(repo_id="r1", repo_name="orders", entities=[e1, e2]),
        ]
    )
    block = _render_prior_evidence_block(sub)
    assert "## Prior research evidence" in block
    assert "### Prior repo: orders" in block
    assert "urn:test:RetryQueue" in block
    assert "urn:test:GatewayClient" in block


def test_prior_evidence_block_returns_empty_for_empty_hydration() -> None:
    sub = HydratedSubgraph(repos=[], total_entities=0, total_edges=0)
    assert _render_prior_evidence_block(sub) == ""


def test_prior_evidence_block_records_missing_urns_note() -> None:
    e1 = _make_snippet(urn="urn:test:Survives", name="Survives")
    sub = _make_hydrated(
        [HydratedRepoSubgraph(repo_id="r1", repo_name="orders", entities=[e1])],
        missing_urns=["urn:test:Gone1", "urn:test:Gone2"],
    )
    block = _render_prior_evidence_block(sub)
    assert "2 URN(s) that no longer exist" in block


def test_synthesis_user_message_appends_prior_evidence_after_per_repo_blocks() -> None:
    e = _make_snippet(urn="urn:test:Prior", name="Prior")
    sub = _make_hydrated(
        [
            HydratedRepoSubgraph(repo_id="r1", repo_name="orders", entities=[e]),
        ]
    )
    msg = _build_synthesis_user_message(
        question="What's new vs prior?",
        bundles=[],
        prior_bundle=_make_bundle(),
        prior_subgraph=sub,
    )
    assert "## Prior research evidence" in msg
    # Prior evidence is APPENDED after the closing instruction-context-
    # but-before-the-final-instruction block. Specifically, it should
    # appear AFTER the <prior_research> overview block (entries first
    # → details second).
    assert msg.index("<prior_research>") < msg.index(
        "## Prior research evidence",
    )


def test_synthesis_user_message_omits_prior_evidence_when_no_subgraph() -> None:
    msg = _build_synthesis_user_message(
        question="q",
        bundles=[],
        prior_bundle=_make_bundle(),  # no subgraph passed
    )
    assert "## Prior research evidence" not in msg


def test_synthesis_user_message_omits_prior_evidence_when_subgraph_empty() -> None:
    """Empty hydration (URNs all gone) → still suppresses the block."""
    msg = _build_synthesis_user_message(
        question="q",
        bundles=[],
        prior_bundle=_make_bundle(),
        prior_subgraph=HydratedSubgraph(repos=[], total_entities=0, total_edges=0),
    )
    assert "## Prior research evidence" not in msg


def test_prior_evidence_block_renders_multiple_repos_in_order() -> None:
    a1 = _make_snippet(urn="urn:test:A1", name="A1")
    a2 = _make_snippet(urn="urn:test:A2", name="A2")
    b1 = _make_snippet(urn="urn:test:B1", name="B1")
    sub = _make_hydrated(
        [
            HydratedRepoSubgraph(repo_id="ra", repo_name="alpha", entities=[a1, a2]),
            HydratedRepoSubgraph(repo_id="rb", repo_name="beta", entities=[b1]),
        ]
    )
    block = _render_prior_evidence_block(sub)
    assert block.index("alpha") < block.index("beta")
    assert "urn:test:A1" in block and "urn:test:B1" in block
