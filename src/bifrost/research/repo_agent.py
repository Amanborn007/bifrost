"""Per-repo research sub-agent.

Spawns a `claude -p` subprocess scoped to one repo's local clone, with
Read/Glob/Grep tools + top-K entity snippets, and returns a structured
RepoReport describing that repo's role in answering the user's question.

Used by the coordinator in fan-out/fan-in mode. Each question spawns N of
these in parallel (capped at N=5 to bound cost).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import (
    ClaudeCliAuthError,
    ClaudeCliError,
    ClaudeLlmClient,
    Usage,
)

logger = logging.getLogger(__name__)


@dataclass
class GraphEdge:
    """One directed relationship between two entities, surfaced to
    downstream agents + synthesis for graph-aware reasoning."""

    rel_type: str  # e.g., "calls", "uses", "implements"
    other_name: str  # the partner entity's display name
    other_urn: str  # partner URN (stable citation target)
    other_type: str  # component | flow | dependency | ...
    # Provenance — so the LLM knows how much to trust this edge and
    # can phrase the claim appropriately (code-verified vs declared
    # vs inferred). See confidence_policy.py for tier meanings.
    confidence: float = 0.5
    source_type: str = "unknown"  # "code_verified" | "manifest" | ...
    tier: int = 5  # 1 (best) .. 5 (worst)


@dataclass
class EntitySnippet:
    """One entity with enough graph context to cite it by URN.

    Enriched form used by:
      - repo_agent (as the entity index it can skim)
      - synthesis (as the evidence block with relationships)
    """

    name: str
    entity_type: str
    purpose: str
    source_file: str | None = None
    # Graph-aware fields (added to fix thin synthesis input):
    id: str = ""  # uuid (string)
    urn: str = ""  # citation target
    metadata: dict[str, object] = field(default_factory=dict)
    outbound: list[GraphEdge] = field(default_factory=list)
    inbound: list[GraphEdge] = field(default_factory=list)


@dataclass
class EdgeVerification:
    """One graph edge audited by the repo agent.

    The agent is given the subgraph slice's edges and asked to verify
    each one via Read/Grep on the clone. This lets synthesis
    distinguish trusted claims (verified=True) from ones derived only
    from extraction metadata.
    """

    edge_desc: str  # "OrderRouter --calls--> FulfillmentExecutor"
    verified: str  # "true" | "false" | "unknown"
    evidence: str = ""  # file:line or short quote supporting the verdict


@dataclass
class MissedEdge:
    """An edge the agent discovered during audit that wasn't in the
    pre-extracted graph. Feeds back into extraction improvements over
    time + surfaces in synthesis as 'additional flows found live'."""

    source_name: str
    rel_type: str
    target_name: str
    evidence: str = ""  # file:line or quote


@dataclass
class CodeShard:
    """One slice of a repo's entities, given to a single code agent.

    When ``settings.agents_per_repo_max > 1`` and a repo has
    enough entities to warrant splitting, the coordinator builds N
    shards instead of one and dispatches a code agent per shard. The
    agent's prompt names the shard (index + total + community label
    when present) so it knows to focus there and not duplicate work
    other shards are doing.
    """

    shard_index: int  # 1-based for display ("shard 2 of 4")
    total_shards: int
    entity_urns: list[str]
    # Human-readable label when the shard was built from a Louvain
    # community ("auth-flow", "data-pipeline", …). ``None`` for
    # entity-count-balanced fallback shards.
    community_label: str | None = None


@dataclass
class RepoReport:
    """Structured output from one repo sub-agent."""

    repo_id: str
    repo_name: str
    clone_path: str
    relevance: str  # high | medium | low | none
    role_in_answer: str  # 1-3 sentences: this repo's role
    key_components: list[dict[str, str]] = field(default_factory=list)
    key_flows: list[dict[str, str]] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    evidence: str = ""  # 1-2 sentences: what was verified
    gaps: list[str] = field(default_factory=list)
    # Stage 4: audit-mode fields. When the agent is given a subgraph
    # slice, it emits per-edge verification + edges it found live but
    # weren't in the index.
    edge_verifications: list[EdgeVerification] = field(default_factory=list)
    missed_edges: list[MissedEdge] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    metadata_stale: bool = False  # True if remote has moved past extracted sha
    stale_note: str = ""  # user-visible note about staleness
    error: str | None = None
    usage: Usage | None = None  # per-subagent cost attribution

    def to_dict(self) -> dict:
        """JSON-serialisable form for the agent_results cache."""
        from dataclasses import asdict

        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "clone_path": self.clone_path,
            "relevance": self.relevance,
            "role_in_answer": self.role_in_answer,
            "key_components": [dict(c) for c in self.key_components],
            "key_flows": [dict(f) for f in self.key_flows],
            "key_files": list(self.key_files),
            "evidence": self.evidence,
            "gaps": list(self.gaps),
            "edge_verifications": [asdict(e) for e in self.edge_verifications],
            "missed_edges": [asdict(m) for m in self.missed_edges],
            "contradictions": list(self.contradictions),
            "metadata_stale": self.metadata_stale,
            "stale_note": self.stale_note,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RepoReport:
        return cls(
            repo_id=d["repo_id"],
            repo_name=d["repo_name"],
            clone_path=d.get("clone_path", ""),
            relevance=d.get("relevance", "none"),
            role_in_answer=d.get("role_in_answer", ""),
            key_components=[dict(c) for c in d.get("key_components", [])],
            key_flows=[dict(f) for f in d.get("key_flows", [])],
            key_files=list(d.get("key_files", [])),
            evidence=d.get("evidence", ""),
            gaps=list(d.get("gaps", [])),
            edge_verifications=[EdgeVerification(**e) for e in d.get("edge_verifications", [])],
            missed_edges=[MissedEdge(**m) for m in d.get("missed_edges", [])],
            contradictions=list(d.get("contradictions", [])),
            metadata_stale=bool(d.get("metadata_stale", False)),
            stale_note=d.get("stale_note", ""),
            error=d.get("error"),
        )


_REPO_AGENT_SYSTEM_PROMPT = (
    """\
You are a research agent for ONE repository. You have a STRICT turn
budget — there's a hard cap on tool calls and if you exceed it you
emit NOTHING and the coordinator wastes your work.

## PRIMARY JOB

Answer: "What does THIS repo do toward the user's question?"

Fill out relevance + role_in_answer + key_components (2-4) +
key_flows (1-2) + key_files (2-4) + evidence. That's the minimum.

## OPTIONAL BONUS (only if you still have 4+ turns left)

If — and ONLY if — you've finished the primary job and still have
spare turns, add 1-3 edge_verifications: pick 1-3 of the MOST
important claims implied by the entity index, Grep ONCE to verify
each, record {edge_desc, verified, evidence: "file:line"}.

If you notice a caller/dependency NOT in the entity index during
normal exploration, drop it into missed_edges. Don't go hunting.

If the primary job took all your turns, LEAVE the audit arrays EMPTY.
Empty arrays are fine. DO NOT keep Grep'ing past turn 10.

## INPUTS

- **In-repo entities**: live here. Verify via Read/Grep.
- **Boundary entities**: live in OTHER repos. Cite their URNs when
  describing cross-repo flows. DO NOT try to Grep them locally.

## STRICT TURN DISCIPLINE

- Max 8 tool calls total. Turn 9-10 MUST emit StructuredOutput.
- One broad Grep beats five narrow ones.
- NEVER Read the same file twice.
- Do not use head_limit, offset, or multiline on Grep — rejected.

If this repo isn't relevant, return relevance="low" or "none" early.

"""
    + BOUNDARY_SENTENCE
    + """

Respond with ONLY the structured JSON matching the provided schema.
"""
)


_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["relevance", "role_in_answer"],
    "properties": {
        "relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },
        "role_in_answer": {"type": "string"},
        "key_components": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "why"],
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
        "key_flows": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "summary"],
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "key_files": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "string"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        # Stage 4: audit mode. All optional so a "low relevance" early
        # return doesn't need to populate them.
        "edge_verifications": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["edge_desc", "verified"],
                "properties": {
                    "edge_desc": {"type": "string"},
                    "verified": {
                        "type": "string",
                        "enum": ["true", "false", "unknown"],
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "missed_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_name", "rel_type", "target_name"],
                "properties": {
                    "source_name": {"type": "string"},
                    "rel_type": {"type": "string"},
                    "target_name": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "contradictions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


def _format_entity_context(
    entities: list[EntitySnippet],
    boundary: list[EntitySnippet] | None = None,
    max_in_repo: int = 15,
    max_boundary: int = 10,
    max_len: int = 2500,
) -> str:
    """Compact text representation of the entity index for the sub-agent.

    Trimmed for cost: capping counts + shortening lines keeps the
    initial prompt small (~2.5K chars vs 5K+ before). Repeat queries
    against the same clone thereafter benefit from prompt cache.

    Split into two sections when a subgraph slice was provided:
      * **In-repo entities** — live in this clone; the agent should
        verify 1-3 key edges via Read/Grep if time permits.
      * **Boundary entities** — live in OTHER repos; list only NAME+
        URN so the agent knows cross-repo refs exist without inflating
        the prompt.
    """
    if not entities and not boundary:
        return "(no pre-extracted entities for this repo)"

    lines: list[str] = []
    total = 0

    if entities:
        lines.append("**In-repo entities** (verify claims via Read/Grep):")
        for i, e in enumerate(entities[:max_in_repo]):
            # Trim purpose aggressively; URN + name + short purpose only.
            purpose = (e.purpose or "")[:80]
            src = f" @ {e.source_file}" if e.source_file else ""
            line = f"- `{e.urn}` {e.name}{src} — {purpose}"
            if total + len(line) > max_len:
                lines.append(f"... (+{len(entities) - i} more in-repo truncated)")
                break
            lines.append(line)
            total += len(line)

    if boundary:
        lines.append("")
        lines.append("**Boundary entities** (in OTHER repos — cite URNs, don't Grep):")
        for i, e in enumerate(boundary[:max_boundary]):
            # Name + URN only — the agent only needs to know they exist.
            line = f"- `{e.urn}` {e.name}"
            if total + len(line) > max_len:
                lines.append(f"... (+{len(boundary) - i} more boundary truncated)")
                break
            lines.append(line)
            total += len(line)

    return "\n".join(lines)


async def research_repo(
    *,
    question: str,
    repo_id: str,
    repo_name: str,
    clone_path: str,
    entities: list[EntitySnippet],
    boundary_entities: list[EntitySnippet] | None = None,
    metadata_stale: bool = False,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
    shard: CodeShard | None = None,
) -> RepoReport:
    """Spawn one sub-agent to answer "what is this repo's role in this q?"

    When the coordinator has a master subgraph (Stage 3), it passes
    ``boundary_entities`` — entities in OTHER repos that are
    edge-connected to this repo's nodes. The agent uses them as
    context for cross-repo citations but does NOT try to Read them
    locally (they aren't in this clone).

    ``shard`` activates sharded mode. The agent's user message
    names the shard's index/total + community label so it knows to
    focus on its slice and not duplicate work other shards are doing.
    The caller is responsible for filtering ``entities`` to
    ``shard.entity_urns`` before invocation.

    Hard-bounded: 5 tool-use turns, 120s wall clock, ~10K input tokens.
    Never raises — failures come back as RepoReport(error=...) so the
    coordinator can synthesize with partial results.
    """
    settings = settings or get_settings()

    stale_note = ""
    if metadata_stale:
        stale_note = (
            "NOTE: The remote repo has newer commits since this repo's "
            "knowledge was extracted. Local code is up-to-date; extracted "
            "entity metadata may be slightly behind."
        )

    # entity_text and prior_turns both contain content sourced
    # from prior LLM extraction passes that ran on attacker-controlled
    # source files. Wrap them in tagged boundaries so the agent treats
    # them as data, not instructions.
    entity_text = _format_entity_context(entities, boundary_entities)
    prior_section = (
        "## Prior conversation (for context — do NOT answer prior questions)\n"
        + wrap_untrusted(prior_turns, "conversation_turn")
        + "\n\n"
        if prior_turns
        else ""
    )
    # Shard framing — when the repo was split, tell the agent
    # which slice it owns + give the cohabiting shards' scope so it
    # doesn't redundantly explore the whole repo. Empty string when
    # not sharded so single-agent prompts are byte-identical to before.
    shard_framing = ""
    if shard is not None:
        if shard.community_label:
            shard_framing = (
                f"## Shard scope\n"
                f"You are shard {shard.shard_index} of {shard.total_shards} "
                f"for this repo — focus on entities in the "
                f"**{shard.community_label}** community ("
                f"{len(shard.entity_urns)} URNs). Other shards are "
                f"covering the remaining communities; do NOT explore "
                f"unrelated areas of the codebase. Cite only URNs from "
                f"the In-repo entities list below.\n\n"
            )
        else:
            shard_framing = (
                f"## Shard scope\n"
                f"You are shard {shard.shard_index} of {shard.total_shards} "
                f"for this repo — focus on the {len(shard.entity_urns)} "
                f"URNs in the In-repo entities list below. Other shards "
                f"are handling the rest of the repo's entities; do NOT "
                f"redo their work.\n\n"
            )
    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"{prior_section}"
        f"## Current user question\n"
        f"{wrap_untrusted(question, 'user_intent')}\n\n"
        f"Repository: {repo_name} (id={repo_id})\n"
        f"Local clone: {clone_path}\n"
        f"{stale_note}\n\n"
        f"{shard_framing}"
        f"## Query subgraph slice\n"
        f"{wrap_untrusted(entity_text, 'entity')}\n\n"
        f"Investigate ONLY the role of THIS repo. Use Grep/Read/Glob on "
        f"{clone_path} to verify in-repo claims; treat boundary entities "
        f"as known facts (they were extracted from other repos — do not "
        f"try to Grep them locally). When a flow leaves this repo, name "
        f"the boundary entity's URN explicitly in key_flows. Produce the "
        f"structured JSON report."
    )

    client = ClaudeLlmClient(settings)

    # Resilient invocation. The code agent is the user-facing critical path and
    # it MUST land code-grounded across all conditions, not just on a quiet
    # machine. Two failure classes are retried here:
    #   1. ``error_max_turns`` — handled INSIDE call_with_dynamic_max_turns
    #      (grows the turn budget ~16→26, cache-warm).
    #   2. Transient / contention exit-1 — when several agents + the extraction
    #      worker hammer the one Claude CLI OAuth session, the code agent can
    #      die with a transient ``exit 1`` (rate-limit, connection, an
    #      interrupted stream). Historically that was discarded on the FIRST
    #      failure, so the whole repo's code findings vanished and the answer
    #      silently dropped to doc-tier. We now retry with backoff so a
    #      transient blip doesn't cost us the code walk.
    # Auth errors are NOT retried (they don't self-heal).
    completion = None
    last_exc: ClaudeCliError | None = None
    for attempt in range(_CODE_AGENT_MAX_ATTEMPTS):
        try:
            completion = await client.call_with_dynamic_max_turns(
                initial_max_turns=16,  # audit is opt-in; primary job needs ~8 turns
                max_attempts=2,
                system_prompt=_REPO_AGENT_SYSTEM_PROMPT,
                user_message=user_message,
                model=settings.agent_model,  # Sonnet — reasoning over code
                json_schema=_REPORT_SCHEMA,
                timeout_override=150,
                allowed_tools=["Read", "Glob", "Grep"],
                add_dirs=[clone_path],
            )
            break
        except ClaudeCliAuthError as exc:
            last_exc = exc
            logger.warning(
                "Sub-agent for repo %s (%s) auth-failed (not retried): %s",
                repo_name,
                repo_id,
                exc,
            )
            break
        except ClaudeCliError as exc:
            last_exc = exc
            if attempt < _CODE_AGENT_MAX_ATTEMPTS - 1:
                backoff = _CODE_AGENT_RETRY_BACKOFF_S * (attempt + 1)
                logger.warning(
                    "code agent for %s transient failure (attempt %d/%d): %s — retrying in %.0fs",
                    repo_name,
                    attempt + 1,
                    _CODE_AGENT_MAX_ATTEMPTS,
                    str(exc)[:160],
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue
            logger.warning(
                "Sub-agent for repo %s (%s) failed after %d attempts: %s",
                repo_name,
                repo_id,
                _CODE_AGENT_MAX_ATTEMPTS,
                exc,
            )

    if completion is None:
        return RepoReport(
            repo_id=repo_id,
            repo_name=repo_name,
            clone_path=clone_path,
            relevance="none",
            role_in_answer="(sub-agent failed)",
            metadata_stale=metadata_stale,
            stale_note=stale_note,
            error=str(last_exc) if last_exc else "code agent failed",
        )

    data = completion.parsed or {}

    # Stage 4 audit-mode parsing — all optional. If the agent decided
    # the repo was low-relevance it may have skipped the audit arrays.
    edge_verifications: list[EdgeVerification] = []
    for row in data.get("edge_verifications") or []:
        if not isinstance(row, dict):
            continue
        edge_verifications.append(
            EdgeVerification(
                edge_desc=str(row.get("edge_desc", ""))[:200],
                verified=str(row.get("verified", "unknown")),
                evidence=str(row.get("evidence", ""))[:200],
            )
        )
    missed_edges: list[MissedEdge] = []
    for row in data.get("missed_edges") or []:
        if not isinstance(row, dict):
            continue
        missed_edges.append(
            MissedEdge(
                source_name=str(row.get("source_name", ""))[:100],
                rel_type=str(row.get("rel_type", ""))[:40],
                target_name=str(row.get("target_name", ""))[:100],
                evidence=str(row.get("evidence", ""))[:200],
            )
        )
    contradictions = [
        str(c)[:300] for c in (data.get("contradictions") or []) if isinstance(c, str)
    ]

    return RepoReport(
        repo_id=repo_id,
        repo_name=repo_name,
        clone_path=clone_path,
        relevance=str(data.get("relevance", "low")),
        role_in_answer=str(data.get("role_in_answer", "")),
        key_components=list(data.get("key_components") or []),
        key_flows=list(data.get("key_flows") or []),
        key_files=list(data.get("key_files") or []),
        evidence=str(data.get("evidence", "")),
        gaps=list(data.get("gaps") or []),
        edge_verifications=edge_verifications,
        missed_edges=missed_edges,
        contradictions=contradictions,
        metadata_stale=metadata_stale,
        stale_note=stale_note,
        usage=completion.usage,
    )


# ─── Multi-code-agent sharding ──────────────────────────────────


# Below this size we don't shard — single-agent traversal is faster
# and cheaper than the orchestration overhead of N parallel agents.
# Tied to the 200-component threshold called out in the sharding spec; bump
# if Sonnet's context-window economics change.
_SHARD_SIZE_THRESHOLD = 200

# A community must have at least this many entities to deserve its
# own shard. Smaller communities get rolled into the "other" shard so
# we don't pay LLM startup cost for a 3-entity slice.
_MIN_COMMUNITY_SHARD_SIZE = 10

# Hard ceiling on shards-per-repo. Even when ``agents_per_repo_max``
# is set higher, more than 8 parallel agents on one repo wastes more
# in coordination overhead than it saves; the spec already maps to
# this via ``agents_per_repo_max``'s ``le=8`` validator.
_MAX_SHARDS_PER_REPO = 8

# Auto-sharding: the code layer is ALWAYS a big scan — a
# single agent can't reliably Grep/Read a large repo within one turn
# budget (and starves under concurrent load → exit-1 / max_turns). So we
# scale the number of code agents to the repo's actual CODE FOOTPRINT
# (files + entities), rather than relying on the per-conversation
# ``agents_per_repo_max`` knob (default 1). Each agent gets a bounded,
# community-scoped slice it can actually finish.
#
# Per-agent scan budget. We pick the BINDING constraint across signals so a
# repo that is file-heavy OR entity-heavy both fan out appropriately.
# Resilient code-agent invocation: retry transient/contention
# exit-1 failures so a momentary blip (rate-limit, interrupted stream while the
# extraction worker + sibling agents share the CLI) doesn't discard the repo's
# code walk and silently drop the answer to doc-tier.
_CODE_AGENT_MAX_ATTEMPTS = 3
_CODE_AGENT_RETRY_BACKOFF_S = 3.0

_CODE_AGENT_TARGET_ENTITIES = 180  # ~entities one agent can index + reason over
_CODE_AGENT_TARGET_FILES = 600  # ~distinct source files one agent can scan
_CODE_AGENT_TARGET_KLOC = 40  # ~thousands of LOC one agent can scan
# Cap auto-scaling so a giant repo doesn't fan out unboundedly.
_MAX_AUTO_CODE_AGENTS = 6


def auto_code_agent_count(
    *,
    n_entities: int,
    n_files: int = 0,
    n_kloc: float = 0.0,
    user_max: int = 1,
) -> int:
    """Intelligently scale code-analysis agents to a repo's code footprint.

    Big repos are big scans: one agent walking 10k+ files within a single
    turn/timeout budget is the failure mode we keep hitting (and it's the
    first thing starved when extraction load competes for the CLI). Decide
    how many subagents to release from *how much code we'd ask each one to
    scan* — files, KLOC, and entities — and spawn ``ceil(size / budget)``
    for whichever signal is the binding constraint.

    - Tiny slices (<= ``_SHARD_SIZE_THRESHOLD`` entities AND <=
      ``_CODE_AGENT_TARGET_FILES`` files) → 1 agent; sharding wouldn't pay off.
    - Otherwise → max over (files/target, kloc/target, entities/target),
      capped at ``_MAX_AUTO_CODE_AGENTS``.
    - Never below the user's explicit ``agents_per_repo_max`` (the knob is a
      floor/override, not the sole driver).
    """
    user_floor = user_max if user_max and user_max > 0 else 1
    if n_entities <= _SHARD_SIZE_THRESHOLD and n_files <= _CODE_AGENT_TARGET_FILES:
        return user_floor

    def _ceil_div(a: float, b: float) -> int:
        return int(-(-a // b)) if a > 0 and b > 0 else 0

    by_entities = _ceil_div(n_entities, _CODE_AGENT_TARGET_ENTITIES)
    by_files = _ceil_div(n_files, _CODE_AGENT_TARGET_FILES)
    by_kloc = _ceil_div(n_kloc, _CODE_AGENT_TARGET_KLOC)
    auto = max(by_entities, by_files, by_kloc, 1)
    auto = min(auto, _MAX_AUTO_CODE_AGENTS)
    return max(auto, user_floor)


def shard_repo_for_code_agents(
    *,
    entities: list[EntitySnippet],
    agents_per_repo_max: int,
    community_label_by_urn: dict[str, str] | None = None,
    size_threshold: int = _SHARD_SIZE_THRESHOLD,
) -> list[CodeShard]:
    """Split a repo's entities into N shards for parallel code agents.

    **Signature note vs spec:** the sharding spec named this
    ``shard_repo_for_code_agents(repo, entities, settings)``. The
    actual signature drops the ``repo`` argument and replaces
    ``settings`` with the two values it actually consumes
    (``agents_per_repo_max`` + the optional pre-loaded community
    map). Rationale: keeps the function pure (no DB I/O, no model
    coupling) so it's trivially testable, and lets the coordinator
    centralize the community-data load it has to do anyway. The
    semantics match the spec exactly.

    Returns ``[]`` when sharding is not warranted — callers should
    interpret the empty list as "use the single-agent path". Sharding
    triggers ONLY when:

      * ``agents_per_repo_max > 1``, AND
      * the repo has more than ``size_threshold`` entities

    Two strategies, in priority order:

      1. **Louvain communities** — when ``community_label_by_urn`` is
         provided, group entities by community label. Communities with
         fewer than ``_MIN_COMMUNITY_SHARD_SIZE`` entities collapse
         into a single "other" shard so the cost-per-shard stays
         reasonable. Capped at ``agents_per_repo_max``.
      2. **Entity-count balanced** — fallback. Sort by URN, split into
         ``min(agents_per_repo_max, ceil(total/size_threshold))``
         contiguous shards. The URN sort makes the partition stable so
         re-runs of the same query hit the same shards' caches.

    Args:
        entities: The full in-repo entity list for one repo (after the
            master-subgraph slice; boundary entities are NOT included).
        agents_per_repo_max: From ``ChatSettings.agents_per_repo_max``.
            ``1`` means no sharding; ``2-8`` enables it.
        community_label_by_urn: Optional precomputed map from each
            entity URN to its Louvain community label. The coordinator
            loads this from ``entity_communities`` in a single SQL
            query before calling. ``None`` falls back to balanced
            sharding without touching the DB.
        size_threshold: Override for tests; production uses the
            module-level default.

    Returns:
        A list of ``CodeShard`` ordered by ``shard_index`` (1-based),
        OR an empty list when sharding is not warranted.
    """
    if agents_per_repo_max <= 1:
        return []
    if len(entities) <= size_threshold:
        return []

    # Cap: never produce more shards than the user asked for, and
    # never exceed the safety ceiling.
    cap = min(agents_per_repo_max, _MAX_SHARDS_PER_REPO)

    # Strategy 1 — community-based.
    if community_label_by_urn:
        groups: dict[str, list[str]] = {}
        small_bucket: list[str] = []
        urn_set = {e.urn for e in entities}
        for e in entities:
            label = community_label_by_urn.get(e.urn)
            if label:
                groups.setdefault(label, []).append(e.urn)
            else:
                small_bucket.append(e.urn)
        # Communities with fewer than the minimum size aren't worth
        # their own shard — fold them into the "other" bucket. This
        # avoids the worst case where every entity is in its own
        # singleton community (e.g. cold-start, no real clustering).
        for label, urns in list(groups.items()):
            if len(urns) < _MIN_COMMUNITY_SHARD_SIZE:
                small_bucket.extend(urns)
                del groups[label]

        # If ANY meaningful community survived, we've got our strategy.
        # Keep ``cap`` largest communities (by entity count); roll the
        # rest into "other". Reserve a slot for "other" when we know
        # there will be overflow OR there's already a non-empty
        # small_bucket — otherwise we'd produce ``cap + 1`` shards.
        if groups:
            ranked = sorted(
                groups.items(),
                key=lambda kv: len(kv[1]),
                reverse=True,
            )
            need_other_slot = bool(small_bucket) or len(ranked) > cap
            kept_budget = max(1, cap - (1 if need_other_slot else 0))
            kept = ranked[:kept_budget]
            overflow = ranked[len(kept) :]
            for _, urns in overflow:
                small_bucket.extend(urns)
            shards: list[CodeShard] = []
            total = len(kept) + (1 if small_bucket else 0)
            for i, (label, urns) in enumerate(kept, start=1):
                shards.append(
                    CodeShard(
                        shard_index=i,
                        total_shards=total,
                        entity_urns=sorted(urns),
                        community_label=label,
                    )
                )
            if small_bucket:
                # Only add the "other" shard when it has ≥ the minimum
                # — otherwise the smaller pieces are tiny enough to
                # absorb into the LAST community shard rather than
                # spawning an extra subprocess for almost nothing.
                if len(small_bucket) >= _MIN_COMMUNITY_SHARD_SIZE:
                    shards.append(
                        CodeShard(
                            shard_index=total,
                            total_shards=total,
                            entity_urns=sorted(small_bucket),
                            community_label="other",
                        )
                    )
                else:
                    last = shards[-1]
                    merged = sorted(set(last.entity_urns) | set(small_bucket))
                    shards[-1] = CodeShard(
                        shard_index=last.shard_index,
                        total_shards=len(shards),
                        entity_urns=merged,
                        community_label=last.community_label,
                    )
                    # Fix up totals on the rest after dropping "other".
                    new_total = len(shards)
                    shards = [
                        CodeShard(
                            shard_index=s.shard_index,
                            total_shards=new_total,
                            entity_urns=s.entity_urns,
                            community_label=s.community_label,
                        )
                        for s in shards
                    ]
            # Coverage check: every URN in the input must land in
            # exactly one shard. If not, fall through to the balanced
            # path rather than silently dropping entities.
            covered = {u for s in shards for u in s.entity_urns}
            if covered == urn_set:
                return shards
            # else: community grouping had drift (orphan URNs). Fall
            # through to balanced.

    # Strategy 2 — entity-count balanced fallback.
    import math

    n_shards = min(cap, max(2, math.ceil(len(entities) / size_threshold)))
    sorted_urns = sorted(e.urn for e in entities)
    shard_size = math.ceil(len(sorted_urns) / n_shards)
    shards: list[CodeShard] = []
    for i in range(n_shards):
        slice_urns = sorted_urns[i * shard_size : (i + 1) * shard_size]
        if not slice_urns:
            continue
        shards.append(
            CodeShard(
                shard_index=i + 1,
                total_shards=n_shards,
                entity_urns=slice_urns,
                community_label=None,
            )
        )
    # Re-stamp total_shards in case the last partition was empty
    # (rare with shard_size derived from ceil, but possible when
    # entity count is just slightly over the threshold).
    actual_total = len(shards)
    if actual_total != n_shards:
        shards = [
            CodeShard(
                shard_index=s.shard_index,
                total_shards=actual_total,
                entity_urns=s.entity_urns,
                community_label=s.community_label,
            )
            for s in shards
        ]
    return shards


def merge_repo_reports(reports: list[RepoReport]) -> RepoReport:
    """Merge N shard reports into a single RepoReport for synthesis.

    The synthesizer doesn't know about shards — it sees one report per
    repo. So when a repo was sharded into N agents, we union their
    findings here before handing off.

    Merge rules:
      * ``relevance``: highest of any shard ("high" beats "medium"
        beats "low" beats "none"). One shard finding "high" relevance
        is enough to mark the repo high-relevance.
      * ``role_in_answer``: concatenate non-empty rationales,
        de-duplicated. The first non-empty value usually carries the
        most weight; trailing shards add detail.
      * ``key_components`` / ``key_flows``: union, de-duplicated by
        ``name``. Equal-name entries from different shards prefer the
        first occurrence (earlier shard's framing wins).
      * ``key_files``: union, order preserved (set semantics).
      * ``evidence``: concatenate non-empty entries (one line each).
      * ``gaps`` / ``contradictions``: union, de-duplicated by string.
      * ``edge_verifications``: union, de-duplicated by ``edge_desc``;
        when two shards disagree on ``verified``, "true" wins over
        "unknown" wins over "false" — the strongest signal we have.
      * ``missed_edges``: union, de-duplicated by
        (source_name, rel_type, target_name).
      * ``usage``: summed across shards.
      * ``error``: bubble up the FIRST non-None error so the chat
        surfaces a failure even if other shards succeeded.
      * ``metadata_stale`` / ``stale_note``: same across shards (one
        repo) — take from first.

    Empty input returns a sensible-defaults report (relevance=none,
    role_in_answer notes the merge failure). Single-element input
    returns that element verbatim — no work.
    """
    if not reports:
        raise ValueError("merge_repo_reports requires at least one report")
    if len(reports) == 1:
        return reports[0]

    # ── Provenance: first non-failed report seeds repo identity. ──
    seed = next((r for r in reports if r.error is None), reports[0])

    # ── relevance: max ──
    rel_rank = {"high": 3, "medium": 2, "low": 1, "none": 0}
    best_rel = max(reports, key=lambda r: rel_rank.get(r.relevance, 0)).relevance

    # ── role_in_answer: dedupe non-empty ──
    seen_roles: set[str] = set()
    role_lines: list[str] = []
    for r in reports:
        if r.role_in_answer and r.role_in_answer not in seen_roles:
            seen_roles.add(r.role_in_answer)
            role_lines.append(r.role_in_answer)
    merged_role = " ".join(role_lines)

    # ── key_components / key_flows: union by name ──
    def _union_by_name(rows: list[list[dict[str, str]]]) -> list[dict[str, str]]:
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for row_list in rows:
            for d in row_list:
                name = str(d.get("name", "")).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append(d)
        return out

    merged_components = _union_by_name([r.key_components for r in reports])
    merged_flows = _union_by_name([r.key_flows for r in reports])

    # ── key_files: union, preserving first-seen order ──
    seen_files: set[str] = set()
    merged_files: list[str] = []
    for r in reports:
        for f in r.key_files:
            if f and f not in seen_files:
                seen_files.add(f)
                merged_files.append(f)

    # ── evidence / gaps / contradictions: dedupe strings ──
    def _union_strings(rows: list[list[str]]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for row_list in rows:
            for s in row_list:
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out

    merged_evidence_lines = _union_strings([[r.evidence] if r.evidence else [] for r in reports])
    merged_evidence = " ".join(merged_evidence_lines)
    merged_gaps = _union_strings([r.gaps for r in reports])
    merged_contradictions = _union_strings([r.contradictions for r in reports])

    # ── edge_verifications: dedupe by edge_desc, prefer strongest verdict ──
    verdict_rank = {"true": 2, "unknown": 1, "false": 0}
    by_desc: dict[str, EdgeVerification] = {}
    for r in reports:
        for ev in r.edge_verifications:
            existing = by_desc.get(ev.edge_desc)
            if existing is None:
                by_desc[ev.edge_desc] = ev
                continue
            if verdict_rank.get(ev.verified, 0) > verdict_rank.get(
                existing.verified,
                0,
            ):
                by_desc[ev.edge_desc] = ev
    merged_verifications = list(by_desc.values())

    # ── missed_edges: dedupe by triple ──
    seen_triples: set[tuple[str, str, str]] = set()
    merged_missed: list[MissedEdge] = []
    for r in reports:
        for me in r.missed_edges:
            triple = (me.source_name, me.rel_type, me.target_name)
            if triple in seen_triples:
                continue
            seen_triples.add(triple)
            merged_missed.append(me)

    # ── usage: sum ──
    merged_usage: Usage | None = None
    usages = [r.usage for r in reports if r.usage is not None]
    if usages:
        merged_usage = Usage(
            input_tokens=sum(u.input_tokens for u in usages),
            output_tokens=sum(u.output_tokens for u in usages),
            cache_creation_input_tokens=sum(u.cache_creation_input_tokens or 0 for u in usages),
            cache_read_input_tokens=sum(u.cache_read_input_tokens or 0 for u in usages),
            total_cost_usd=sum(u.total_cost_usd or 0.0 for u in usages),
        )

    # ── error: first non-None bubbles up ──
    merged_error = next((r.error for r in reports if r.error), None)

    return RepoReport(
        repo_id=seed.repo_id,
        repo_name=seed.repo_name,
        clone_path=seed.clone_path,
        relevance=best_rel,
        role_in_answer=merged_role,
        key_components=merged_components,
        key_flows=merged_flows,
        key_files=merged_files,
        evidence=merged_evidence,
        gaps=merged_gaps,
        edge_verifications=merged_verifications,
        missed_edges=merged_missed,
        contradictions=merged_contradictions,
        metadata_stale=seed.metadata_stale,
        stale_note=seed.stale_note,
        error=merged_error,
        usage=merged_usage,
    )
