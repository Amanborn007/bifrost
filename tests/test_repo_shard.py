"""Multi-code-agent sharding helpers.

Pure-data tests for ``shard_repo_for_code_agents`` + ``merge_repo_reports``
in ``bifrost.research.repo_agent``. No DB, no LLM — both helpers are
synchronous-data transforms.

Coverage targets:

  * sharding triggers only when ``agents_per_repo_max > 1`` AND the
    entity count exceeds the threshold
  * community-based sharding when communities ≥ minimum size exist
  * tiny communities collapse into the "other" shard (or merge into
    the last shard when the residue is itself too small)
  * fallback to entity-count balanced when no communities are
    provided
  * coverage invariant: every input URN appears in exactly one shard
  * shard cap respects ``agents_per_repo_max`` AND the safety ceiling
  * merge: relevance is the max, key_components/flows dedupe by name,
    edge_verifications resolve "true > unknown > false", error
    bubbles up
"""

from __future__ import annotations

import pytest

from bifrost.extraction.cli_client import (
    ClaudeCliAuthError,
    ClaudeCliTransientError,
    CliCompletion,
    Usage,
)
from bifrost.research import repo_agent as _ra
from bifrost.research.repo_agent import (
    EdgeVerification,
    EntitySnippet,
    MissedEdge,
    RepoReport,
    auto_code_agent_count,
    merge_repo_reports,
    research_repo,
    shard_repo_for_code_agents,
)

# ── Resilient code-agent invocation: retry transient failures ────


def _ok_completion() -> CliCompletion:
    return CliCompletion(
        parsed={"relevance": "high", "role_in_answer": "did the code walk"},
        usage=Usage(0, 0, 0, 0, 0.0),
        text="",
    )


@pytest.mark.asyncio
async def test_code_agent_retries_transient_then_succeeds(monkeypatch) -> None:
    """A transient exit-1 on the first attempts must NOT discard the code
    walk — it retries and lands code-grounded."""
    calls = {"n": 0}

    async def _flaky(self, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise ClaudeCliTransientError("claude -p transient failure (exit 1)")
        return _ok_completion()

    monkeypatch.setattr(_ra.ClaudeLlmClient, "call_with_dynamic_max_turns", _flaky)
    monkeypatch.setattr(_ra.asyncio, "sleep", lambda *_a, **_k: _noop())

    rep = await research_repo(
        question="does it fail-open on timeout?",
        repo_id="r1",
        repo_name="webhook-api",
        clone_path="/tmp/x",
        entities=[],
    )
    assert calls["n"] == 3  # retried twice, succeeded on 3rd
    assert rep.relevance == "high"  # code-grounded, not an error report
    assert rep.error is None


@pytest.mark.asyncio
async def test_code_agent_auth_error_not_retried(monkeypatch) -> None:
    """Auth errors don't self-heal — fail fast, no retry storm."""
    calls = {"n": 0}

    async def _auth_fail(self, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        raise ClaudeCliAuthError("not authenticated")

    monkeypatch.setattr(_ra.ClaudeLlmClient, "call_with_dynamic_max_turns", _auth_fail)
    rep = await research_repo(
        question="q",
        repo_id="r1",
        repo_name="r",
        clone_path="/tmp/x",
        entities=[],
    )
    assert calls["n"] == 1  # NOT retried
    assert rep.relevance == "none" and rep.error


@pytest.mark.asyncio
async def test_code_agent_returns_error_after_exhausting_retries(monkeypatch) -> None:
    """Persistent transient failure → honest error report (never a silent
    success), after exhausting all attempts."""
    calls = {"n": 0}

    async def _always_fail(self, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        raise ClaudeCliTransientError("still down")

    monkeypatch.setattr(_ra.ClaudeLlmClient, "call_with_dynamic_max_turns", _always_fail)
    monkeypatch.setattr(_ra.asyncio, "sleep", lambda *_a, **_k: _noop())
    rep = await research_repo(
        question="q",
        repo_id="r1",
        repo_name="r",
        clone_path="/tmp/x",
        entities=[],
    )
    assert calls["n"] == _ra._CODE_AGENT_MAX_ATTEMPTS
    assert rep.relevance == "none" and rep.error


async def _noop() -> None:
    return None


# ── Auto-scaling: # of code agents from code footprint ───────────


def test_auto_code_agents_small_slice_is_single_agent() -> None:
    # A small relevant slice doesn't need multiple agents.
    assert auto_code_agent_count(n_entities=50, n_files=20) == 1
    assert auto_code_agent_count(n_entities=200, n_files=600) == 1


def test_auto_code_agents_scales_with_entities() -> None:
    # 563 entities (webhook-api) → ceil(563/180) = 4 agents, no user opt-in.
    assert auto_code_agent_count(n_entities=563, n_files=0) == 4


def test_auto_code_agents_scales_with_files_when_file_heavy() -> None:
    # Entity-light but file-heavy slice still fans out (binding constraint).
    assert auto_code_agent_count(n_entities=210, n_files=1900) == 4  # ceil(1900/600)=4


def test_auto_code_agents_respects_user_floor_and_cap() -> None:
    # User override acts as a floor…
    assert auto_code_agent_count(n_entities=50, n_files=10, user_max=3) == 3
    # …and the hard cap bounds giant repos.
    assert auto_code_agent_count(n_entities=10_000, n_files=10_000) == 6


def _entity(urn: str, name: str | None = None) -> EntitySnippet:
    return EntitySnippet(
        name=name or urn.split("/")[-1],
        entity_type="component",
        purpose=f"purpose for {name or urn}",
        source_file=f"src/{name or urn}.py",
        id=urn,  # using urn as a stand-in id; it's unique
        urn=urn,
    )


def _entities(n: int, prefix: str = "urn:bifrost:repo/r/component") -> list[EntitySnippet]:
    return [_entity(f"{prefix}/E{i:04d}") for i in range(n)]


# ── Trigger conditions ───────────────────────────────────────────


def test_no_shards_when_agents_per_repo_max_is_one() -> None:
    """The default of 1 must short-circuit even for huge repos —
    users who haven't opted in shouldn't pay the orchestration cost."""
    out = shard_repo_for_code_agents(
        entities=_entities(1000),
        agents_per_repo_max=1,
    )
    assert out == []


def test_no_shards_when_entity_count_under_threshold() -> None:
    """Small repos don't shard — single agent is faster + cheaper."""
    out = shard_repo_for_code_agents(
        entities=_entities(150),  # under default threshold 200
        agents_per_repo_max=4,
    )
    assert out == []


def test_no_shards_at_exact_threshold() -> None:
    """Threshold is strict ``>``, not ``>=`` — exactly N entities
    stays single-agent."""
    out = shard_repo_for_code_agents(
        entities=_entities(200),
        agents_per_repo_max=4,
    )
    assert out == []


def test_shards_when_size_threshold_exceeded() -> None:
    out = shard_repo_for_code_agents(
        entities=_entities(500),
        agents_per_repo_max=4,
    )
    assert len(out) >= 2


# ── Community-based sharding ─────────────────────────────────────


def test_communities_drive_shard_grouping() -> None:
    """When community labels exist for ≥ minimum entities each,
    sharding groups by community label, not entity-count balance."""
    ents = _entities(400)
    labels = {}
    for i, e in enumerate(ents):
        if i < 150:
            labels[e.urn] = "auth"
        elif i < 290:
            labels[e.urn] = "billing"
        else:
            labels[e.urn] = "core"
    out = shard_repo_for_code_agents(
        entities=ents,
        agents_per_repo_max=4,
        community_label_by_urn=labels,
    )
    community_labels = {sh.community_label for sh in out}
    assert community_labels == {"auth", "billing", "core"}


def test_tiny_communities_collapse_into_other_shard() -> None:
    """Communities under the minimum-size threshold (10) shouldn't
    each get their own shard — they roll into the 'other' bucket."""
    ents = _entities(400)
    labels = {}
    for i, e in enumerate(ents):
        if i < 200:
            labels[e.urn] = "main"
        elif i < 250:
            labels[e.urn] = "secondary"
        else:
            # 150 entities split into 30 micro-communities (5 each)
            labels[e.urn] = f"micro-{i % 30}"
    out = shard_repo_for_code_agents(
        entities=ents,
        agents_per_repo_max=4,
        community_label_by_urn=labels,
    )
    labels_seen = {sh.community_label for sh in out}
    # Real communities (main, secondary) get their own shards;
    # the 30 micro-communities ALL collapse into "other".
    assert "main" in labels_seen
    assert "secondary" in labels_seen
    assert "other" in labels_seen
    # No micro labels should survive as their own shard.
    micro_labels = {l for l in labels_seen if l and l.startswith("micro-")}
    assert micro_labels == set()


def test_community_overflow_rolls_into_other() -> None:
    """When more meaningful communities exist than agents_per_repo_max,
    the smallest excess collapse into 'other' so we don't overshoot
    the user's budget."""
    ents = _entities(800)
    labels = {}
    # 5 large communities; agents_per_repo_max=3 → top 2 by size keep
    # their own shard, third shard is "other" for the rest.
    sizes = [200, 180, 160, 140, 120]
    idx = 0
    for ci, size in enumerate(sizes):
        for _ in range(size):
            labels[ents[idx].urn] = f"community-{ci}"
            idx += 1
    out = shard_repo_for_code_agents(
        entities=ents,
        agents_per_repo_max=3,
        community_label_by_urn=labels,
    )
    # Total shards must respect cap.
    assert len(out) <= 3
    # An "other" shard exists (since overflow happened).
    assert any(sh.community_label == "other" for sh in out)


# ── Fallback (entity-count balanced) ─────────────────────────────


def test_balanced_fallback_when_no_communities() -> None:
    """``community_label_by_urn=None`` → entity-count balanced shards
    with ``community_label=None``."""
    out = shard_repo_for_code_agents(
        entities=_entities(600),
        agents_per_repo_max=4,
    )
    assert len(out) >= 2
    assert all(sh.community_label is None for sh in out)


def test_orphan_urns_collect_into_other_shard() -> None:
    """If the community map covers only some URNs, the unlabeled
    ones land in an ``other`` shard rather than being dropped — the
    coverage invariant is non-negotiable, even when community data
    is incomplete."""
    ents = _entities(400)
    # Only label half the entities — the rest are orphans.
    labels = {ents[i].urn: "auth" for i in range(0, len(ents), 2)}
    out = shard_repo_for_code_agents(
        entities=ents,
        agents_per_repo_max=4,
        community_label_by_urn=labels,
    )
    label_set = {sh.community_label for sh in out}
    assert "auth" in label_set
    assert "other" in label_set
    # Coverage check happens elsewhere; here we just want both labels.
    assert len(out) == 2


def test_balanced_fallback_partition_size_close_to_threshold() -> None:
    """For 600 entities + agents_per_repo_max=4: ceil(600/200)=3
    balanced shards (NOT 4), because we don't over-shard a repo that
    only narrowly exceeds the threshold."""
    out = shard_repo_for_code_agents(
        entities=_entities(600),
        agents_per_repo_max=4,
    )
    assert len(out) == 3
    # Sizes are roughly equal (within 1 entity).
    sizes = [len(sh.entity_urns) for sh in out]
    assert max(sizes) - min(sizes) <= 1


# ── Coverage / contract ──────────────────────────────────────────


def test_every_urn_appears_in_exactly_one_shard() -> None:
    """Critical invariant: no URN is dropped or duplicated. If this
    breaks, agents miss entities silently."""
    ents = _entities(500)
    out = shard_repo_for_code_agents(
        entities=ents,
        agents_per_repo_max=4,
    )
    seen: list[str] = [u for sh in out for u in sh.entity_urns]
    assert sorted(seen) == sorted(e.urn for e in ents)


def test_coverage_invariant_holds_with_communities_and_orphans() -> None:
    """When community grouping has both kept communities AND orphan
    URNs (no label), the coverage invariant must STILL hold — every
    input URN appears in exactly one shard. Regression guard against
    the small_bucket merge logic silently dropping orphan URNs."""
    ents = _entities(400)
    labels: dict[str, str] = {}
    # 200 entities labeled "main" (kept).
    # 80 entities labeled "secondary" (kept).
    # 50 entities labeled with 10 micro-communities of 5 each (collapsed).
    # 70 entities unlabeled (orphans).
    idx = 0
    for _ in range(200):
        labels[ents[idx].urn] = "main"
        idx += 1
    for _ in range(80):
        labels[ents[idx].urn] = "secondary"
        idx += 1
    for micro in range(10):
        for _ in range(5):
            labels[ents[idx].urn] = f"micro-{micro}"
            idx += 1
    # Last 70 stay unlabeled (orphans).

    out = shard_repo_for_code_agents(
        entities=ents,
        agents_per_repo_max=4,
        community_label_by_urn=labels,
    )
    # Coverage: every input URN appears once across all shards.
    seen: list[str] = [u for sh in out for u in sh.entity_urns]
    assert sorted(seen) == sorted(e.urn for e in ents)
    # No duplicates.
    assert len(seen) == len(set(seen))


def test_shards_are_indexed_and_total_consistent() -> None:
    out = shard_repo_for_code_agents(
        entities=_entities(800),
        agents_per_repo_max=4,
    )
    n = len(out)
    assert n > 0
    # 1-based, no gaps.
    assert [sh.shard_index for sh in out] == list(range(1, n + 1))
    # All shards report the same total.
    assert {sh.total_shards for sh in out} == {n}


def test_safety_cap_holds() -> None:
    """The 8-shard ceiling protects against pathological combos
    (e.g. user types 8 + huge repo with 5000 entities)."""
    out = shard_repo_for_code_agents(
        entities=_entities(5000),
        agents_per_repo_max=8,
    )
    assert len(out) <= 8


# ── merge_repo_reports ───────────────────────────────────────────


def _shard_report(
    repo_id: str = "r1",
    repo_name: str = "svc",
    *,
    relevance: str = "medium",
    role: str = "Handles X",
    components: list[dict[str, str]] | None = None,
    flows: list[dict[str, str]] | None = None,
    files: list[str] | None = None,
    evidence: str = "",
    gaps: list[str] | None = None,
    edges: list[EdgeVerification] | None = None,
    missed: list[MissedEdge] | None = None,
    contradictions: list[str] | None = None,
    error: str | None = None,
    usage: Usage | None = None,
) -> RepoReport:
    return RepoReport(
        repo_id=repo_id,
        repo_name=repo_name,
        clone_path="/tmp/clone",
        relevance=relevance,
        role_in_answer=role,
        key_components=components or [],
        key_flows=flows or [],
        key_files=files or [],
        evidence=evidence,
        gaps=gaps or [],
        edge_verifications=edges or [],
        missed_edges=missed or [],
        contradictions=contradictions or [],
        error=error,
        usage=usage,
    )


def test_merge_single_report_returns_it_unchanged() -> None:
    r = _shard_report(relevance="high")
    assert merge_repo_reports([r]) is r


def test_merge_empty_raises() -> None:
    with pytest.raises(ValueError):
        merge_repo_reports([])


def test_merge_picks_highest_relevance() -> None:
    a = _shard_report(relevance="low")
    b = _shard_report(relevance="high")
    c = _shard_report(relevance="medium")
    assert merge_repo_reports([a, b, c]).relevance == "high"


def test_merge_dedupes_components_by_name() -> None:
    a = _shard_report(components=[{"name": "A", "why": "first"}])
    b = _shard_report(
        components=[
            {"name": "A", "why": "second"},  # duplicate by name
            {"name": "B", "why": "new"},
        ],
    )
    out = merge_repo_reports([a, b])
    names = [c["name"] for c in out.key_components]
    assert names == ["A", "B"]
    # First-seen wins.
    assert out.key_components[0]["why"] == "first"


def test_merge_unions_files_preserving_order() -> None:
    a = _shard_report(files=["a.py", "b.py"])
    b = _shard_report(files=["b.py", "c.py"])
    out = merge_repo_reports([a, b])
    assert out.key_files == ["a.py", "b.py", "c.py"]


def test_merge_concatenates_role_in_answer_dedup() -> None:
    a = _shard_report(role="Routes auth")
    b = _shard_report(role="Routes auth")  # duplicate
    c = _shard_report(role="Logs metrics")
    out = merge_repo_reports([a, b, c])
    assert out.role_in_answer == "Routes auth Logs metrics"


def test_merge_resolves_edge_verdicts_strongest_wins() -> None:
    """Two shards inspected the same edge, disagree on the verdict —
    merge takes the strongest signal (true > unknown > false)."""
    a = _shard_report(
        edges=[
            EdgeVerification(edge_desc="X→Y", verified="false", evidence="not found"),
        ],
    )
    b = _shard_report(
        edges=[
            EdgeVerification(edge_desc="X→Y", verified="true", evidence="src/x.py:42"),
        ],
    )
    c = _shard_report(
        edges=[
            EdgeVerification(edge_desc="X→Y", verified="unknown", evidence=""),
        ],
    )
    out = merge_repo_reports([a, b, c])
    assert len(out.edge_verifications) == 1
    assert out.edge_verifications[0].verified == "true"
    assert out.edge_verifications[0].evidence == "src/x.py:42"


def test_merge_dedupes_missed_edges_by_triple() -> None:
    a = _shard_report(
        missed=[MissedEdge(source_name="X", rel_type="calls", target_name="Y")],
    )
    b = _shard_report(
        missed=[
            MissedEdge(source_name="X", rel_type="calls", target_name="Y"),
            MissedEdge(source_name="X", rel_type="reads", target_name="Y"),
        ],
    )
    out = merge_repo_reports([a, b])
    assert len(out.missed_edges) == 2  # X-calls-Y, X-reads-Y


def test_merge_bubbles_up_first_error() -> None:
    a = _shard_report(relevance="high")
    b = _shard_report(error="Sub-agent timeout")
    c = _shard_report(relevance="medium")
    out = merge_repo_reports([a, b, c])
    assert out.error == "Sub-agent timeout"


def test_merge_sums_usage() -> None:
    a = _shard_report(
        usage=Usage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=20,
            total_cost_usd=0.01,
        ),
    )
    b = _shard_report(
        usage=Usage(
            input_tokens=200,
            output_tokens=100,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=40,
            total_cost_usd=0.02,
        ),
    )
    out = merge_repo_reports([a, b])
    assert out.usage is not None
    assert out.usage.input_tokens == 300
    assert out.usage.output_tokens == 150
    assert out.usage.cache_creation_input_tokens == 10
    assert out.usage.cache_read_input_tokens == 60
    assert out.usage.total_cost_usd == pytest.approx(0.03)


def test_merge_preserves_repo_identity_from_first_non_failed() -> None:
    """If shard 1 fails but shard 2 succeeds, the merged report
    should still identify the repo correctly — pulled from the first
    successful shard, not the failed one."""
    a = _shard_report(
        repo_id="x",
        repo_name="failed-shard",
        error="boom",
        role="(sub-agent failed)",
    )
    b = _shard_report(
        repo_id="r1",
        repo_name="svc-orders",
        role="Routes orders",
    )
    out = merge_repo_reports([a, b])
    assert out.repo_id == "r1"
    assert out.repo_name == "svc-orders"
    # Error still bubbles up so chat surfaces the shard failure.
    assert out.error == "boom"
