"""Router: given a question and N candidate repos, rank the relevant ones.

Purpose: avoid fanning out to 10 Sonnet sub-agents when only 3-5 repos
actually touch the topic. One cheap Haiku call picks the subset.

Input: question string + list of RepoCandidate (name, description, component
count, top few component names).

Output: ranked list of (repo_id, relevance, why). Caller caps at top-K.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


@dataclass
class RepoCandidate:
    """A repo that's in scope for research, plus summary data for routing."""

    id: str
    name: str
    description: str | None = None
    component_count: int = 0
    flow_count: int = 0
    sample_component_names: list[str] | None = None
    # a manifest's ``is_primary`` flag. The router uses this as a tie-
    # breaker — among repos that score the same relevance, prefer
    # the team's designated canonical repo.
    is_primary: bool = False


@dataclass
class RepoRanking:
    """One router decision — a repo with its relevance score."""

    repo_id: str
    repo_name: str
    relevance: str  # "high" | "medium" | "low" | "none"
    reason: str  # 1-line justification from the router


_ROUTER_SYSTEM_PROMPT = """\
You are a routing classifier for an engineering-knowledge system.

Given a user question and a list of repositories with their summaries,
decide which repos are likely to contain information relevant to the
question. Err on the side of INCLUSION when uncertain — the downstream
system has a hard cap on how many repos it investigates, so your job is
to rank rather than aggressively exclude.

For each repo, pick exactly one relevance label:
- "high":   This repo almost certainly has primary code relevant to the
            question (e.g., the question mentions its domain or asks
            about something its purpose suggests).
- "medium": Plausibly touches the question; worth a look.
- "low":    Unlikely to be directly relevant but can't rule out.
- "none":   Clearly unrelated — a different system entirely.

When a repo is flagged ``[primary]``, treat it as the canonical
service repo for its team. On close-relevance ties, prefer the
primary repo over peers (CLI tools, doc repos, generated SDKs)
that share the same squad — the user is likely asking about the
service itself, not its tooling.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured `repo_rankings` JSON matching the
provided schema. Keep `reason` to one sentence.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rankings"],
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["repo_id", "relevance", "reason"],
                "properties": {
                    "repo_id": {"type": "string"},
                    "relevance": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "none"],
                    },
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


_RELEVANCE_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _format_candidate(c: RepoCandidate) -> str:
    desc = f" — {c.description}" if c.description else ""
    samples = ""
    if c.sample_component_names:
        joined = ", ".join(c.sample_component_names[:10])
        samples = f" (components: {joined})"
    primary = " [primary]" if c.is_primary else ""
    return (
        f"- id={c.id} name={c.name}{primary}{desc} "
        f"[{c.component_count} components, {c.flow_count} flows]{samples}"
    )


async def rank_repos(
    *,
    question: str,
    candidates: list[RepoCandidate],
    top_k: int = 5,
    settings: BifrostSettings | None = None,
) -> list[RepoRanking]:
    """Rank which of `candidates` are likely relevant to `question`.

    Returns at most top_k entries, sorted by relevance descending. If the
    router fails, falls back to "include everything as medium" so chat
    still works with degraded signal.
    """
    settings = settings or get_settings()

    # Trivial short-circuit: 1 candidate means nothing to route.
    if len(candidates) <= 1:
        return [
            RepoRanking(
                repo_id=c.id,
                repo_name=c.name,
                relevance="high",
                reason="only candidate in scope",
            )
            for c in candidates
        ]

    candidate_lines = "\n".join(_format_candidate(c) for c in candidates)
    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Candidate repos:\n{candidate_lines}\n\n"
        "For each repo above, emit a ranking entry."
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_ROUTER_SYSTEM_PROMPT,
            user_message=user_prompt,
            model=settings.reasoning_model,  # premium — picks which sources to query
            json_schema=_ROUTER_SCHEMA,
            max_turns=1,
            timeout_override=60,
        )
    except ClaudeCliError as exc:
        logger.warning(
            "Router failed (%s); falling back to medium-for-all",
            exc,
        )
        return [
            RepoRanking(
                repo_id=c.id,
                repo_name=c.name,
                relevance="medium",
                reason="router failed; fallback",
            )
            for c in candidates[:top_k]
        ]

    raw = completion.parsed.get("rankings", []) or []
    # Map by repo_id for easy lookup; some routers return unknown ids, drop those
    by_id = {c.id: c for c in candidates}
    rankings: list[RepoRanking] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("repo_id", ""))
        if rid not in by_id:
            continue
        rel = str(row.get("relevance", "low")).lower()
        if rel not in _RELEVANCE_RANK:
            rel = "low"
        rankings.append(
            RepoRanking(
                repo_id=rid,
                repo_name=by_id[rid].name,
                relevance=rel,
                reason=str(row.get("reason", "")),
            )
        )

    # Backfill any candidates the router skipped — treat as "low"
    seen = {r.repo_id for r in rankings}
    for c in candidates:
        if c.id not in seen:
            rankings.append(
                RepoRanking(
                    repo_id=c.id,
                    repo_name=c.name,
                    relevance="low",
                    reason="not scored by router",
                )
            )

    # Sort by relevance (desc), tie-broken by is_primary (primary first).
    # Without the secondary key, two repos at relevance="medium" could
    # land in any order — and historically the squad's CLI tool tied
    # with its actual service repo more often than was useful.
    rankings.sort(
        key=lambda r: (
            _RELEVANCE_RANK.get(r.relevance, 0),
            1 if by_id.get(r.repo_id) and by_id[r.repo_id].is_primary else 0,
        ),
        reverse=True,
    )

    # Drop "none" unless we'd otherwise return empty
    pruned = [r for r in rankings if r.relevance != "none"]
    if not pruned:
        pruned = rankings[:top_k]
    return pruned[:top_k]
