"""Reasoning-driven scope deepening — the core "follow the trail" intelligence.

A single research wave is often not enough: a code agent walks the scoped
repo(s) and discovers the load-bearing logic actually lives UPSTREAM (in a
repo it didn't walk), so the answer hedges ("the decision is in another repo,
not code-verified"). That is a one-turn, incomplete answer.

This module supplies the reasoning step that turns that into a COMPLETE answer:
given what wave-N found (per-repo relevance, role, gaps, "out-of-scope/upstream"
signals) plus a candidate list of NOT-yet-walked repos (with descriptions /
why-they're-candidates), a reasoning-tier LLM decides whether more repos must be
walked and WHICH — so the coordinator can run another wave on exactly those.

This is PURE reasoning (one no-tool ``claude -p`` call): it returns a decision,
not bundles. The coordinator owns the wave execution (clone → slice → code
agent) because that needs the heavy fan-out helpers + RepoBundle. Keeping this
module free of coordinator imports avoids a circular dependency and makes the
decision trivially unit-testable with a mocked client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


@dataclass
class DeepeningDecision:
    """One reasoning decision about whether to widen the research scope."""

    needs_more_repos: bool
    selected_repos: list[str] = field(default_factory=list)  # repo NAMES (mapped to ids by caller)
    reasoning: str = ""
    expected_gain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_more_repos": self.needs_more_repos,
            "selected_repos": list(self.selected_repos),
            "reasoning": self.reasoning,
            "expected_gain": self.expected_gain,
        }


_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "needs_more_repos": {
            "type": "boolean",
            "description": "True ONLY if a specific, load-bearing part of the "
            "answer is not yet code-verified AND a listed candidate repo "
            "plausibly contains it.",
        },
        "selected_repos": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
            "description": "Names (exactly as given in the candidate list) of the "
            "repos to walk next. Empty when needs_more_repos is false.",
        },
        "reasoning": {
            "type": "string",
            "description": "One or two sentences, user-facing: what is still "
            "unverified and why these repos resolve it (or why the answer is "
            "already complete).",
        },
        "expected_gain": {
            "type": "string",
            "description": "What walking these repos would let us confirm "
            "(e.g. 'the fail-open/closed gate in the caller'). Empty if none.",
        },
    },
    "required": ["needs_more_repos", "selected_repos", "reasoning"],
}

_SYSTEM_PROMPT = """You are the scope-deepening controller for a code-research \
system. CODE IS THE GROUND TRUTH; documentation/Jira/Slack only corroborate it.

You are given (1) the user's question, (2) what the current research wave found \
per repo (relevance, the repo's role, gaps, and any 'the decision lives \
upstream / in another repo / not code-verified / out of scope this turn' \
signals), and (3) a list of candidate repos that have NOT been walked yet, each \
with a short description and why it's a candidate (e.g. cross-repo call edges).

Decide whether the research is COMPLETE or whether a specific, load-bearing part \
of the answer is still NOT code-verified and a candidate repo would resolve it. \
Follow the trail: if a walked repo says 'X is decided by the upstream caller' \
and a candidate is that caller (or plausibly is), select it.

Crucial distinction: a walked repo reporting gaps=none or high relevance means \
it fully described ITS OWN role — NOT that the user's END-TO-END question is \
answered. If the final determination the user asked about depends on the \
behaviour of a NAMED upstream/caller/downstream repo (e.g. a walked repo says \
'X is decided by the caller <repo>' or 'the caller only allows on VALID') and \
that repo is in the candidate list and NOT yet walked, you SHOULD select it — \
the whole point is to code-verify the end-to-end path, not stop at the first \
repo that looks internally complete.

Be precise:
- needs_more_repos=true when a concrete part of the END-TO-END answer is not yet \
code-verified AND a listed candidate plausibly contains that code (especially a \
caller/upstream repo a walked agent explicitly named). Pick the FEWEST repos \
that close the gap (max 3), by NAME exactly as listed.
- needs_more_repos=false ONLY when the answer is genuinely code-grounded \
end-to-end, or when no candidate would actually help. Always fill in 'reasoning'.
Never invent repo names that aren't in the candidate list."""


def _format_wave_findings(wave_findings: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for f in wave_findings:
        gaps = "; ".join(f.get("gaps") or [])[:300]
        lines.append(
            f"- {f.get('repo_name', '?')}: relevance={f.get('relevance', '?')} | "
            f"role={str(f.get('role_in_answer', ''))[:200]} | "
            f"gaps={gaps or 'none'}"
        )
    return "\n".join(lines) or "(no findings)"


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for c in candidates:
        lines.append(
            f"- {c.get('name', '?')}: {str(c.get('description', '') or '')[:200]} "
            f"[{c.get('why', 'candidate')}]"
        )
    return "\n".join(lines) or "(no candidates available)"


async def select_repos_to_deepen(
    *,
    question: str,
    wave_findings: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    settings: BifrostSettings | None = None,
    client: ClaudeLlmClient | None = None,
    external_context: str = "",
) -> DeepeningDecision:
    """Reason about whether to widen scope and which repos to walk next.

    ``wave_findings``: [{repo_name, relevance, role_in_answer, gaps}] from the
    last wave. ``candidates``: [{name, description, why}] of unwalked repos.
    ``external_context``: optional org-context string
    to append to the reasoning prompt — helps the model pick deepening targets
    when code alone is insufficient and org docs hint at where the logic lives.
    Returns a :class:`DeepeningDecision`. Never raises — on any LLM/parse error
    it returns ``needs_more_repos=False`` so the caller falls through to
    synthesis with what it has (no worse than today).
    """
    settings = settings or get_settings()
    if not candidates:
        return DeepeningDecision(False, reasoning="No unwalked candidate repos.")

    client = client or ClaudeLlmClient(settings)
    _ext_block = (
        f"\n\n## Org-context (external KB)\n{external_context.strip()}"
        if external_context and external_context.strip()
        else ""
    )
    user_message = (
        f"## Question\n{question}\n\n"
        f"## What the current research wave found (per repo)\n"
        f"{_format_wave_findings(wave_findings)}\n\n"
        f"## Candidate repos NOT yet walked\n"
        f"{_format_candidates(candidates)}"
        f"{_ext_block}\n\n"
        "Decide: is the answer complete, or do we need to walk specific "
        "candidate repos to code-verify the missing piece?"
    )
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.reasoning_model,
            json_schema=_DECISION_SCHEMA,
            max_turns=1,
            timeout_override=90,
        )
    except ClaudeCliError as exc:
        logger.warning("scope-deepening selection failed (%s); not widening", exc)
        return DeepeningDecision(False, reasoning="selection unavailable")

    data = completion.parsed or {}
    valid_names = {c.get("name") for c in candidates}
    selected = [
        n for n in (data.get("selected_repos") or []) if isinstance(n, str) and n in valid_names
    ]
    needs = bool(data.get("needs_more_repos")) and bool(selected)
    return DeepeningDecision(
        needs_more_repos=needs,
        selected_repos=selected,
        reasoning=str(data.get("reasoning", ""))[:600],
        expected_gain=str(data.get("expected_gain", ""))[:300],
    )


@dataclass
class FollowupSuggestion:
    """One actionable next prompt the user can run to improve the answer."""

    prompt: str
    rationale: str = ""
    repos_to_index: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "rationale": self.rationale,
            "repos_to_index": list(self.repos_to_index),
        }


_FOLLOWUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggestions": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "A concrete next question the user can "
                        "ask verbatim that would close a specific gap.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One line: what gap this closes / what "
                        "it would let us code-verify.",
                    },
                    "repos_to_index": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repo names this prompt would pull in / "
                        "(re)index (from the provided not-yet-walked / "
                        "stale / failed-extraction lists). May be empty.",
                    },
                },
                "required": ["prompt", "rationale"],
            },
        },
    },
    "required": ["suggestions"],
}

_FOLLOWUP_SYSTEM = """You generate SMART follow-up prompts for an engineering \
code-research assistant. The previous answer is code-grounded but has gaps: some \
load-bearing repos were not walked (not indexed / manifest-only / extraction \
failed), some repos are stale (remote moved since extraction), and the answer \
itself lists open questions. Your job: propose up to 4 concrete next prompts the \
user can ask VERBATIM to close those gaps and get a more complete, code-verified \
answer next turn — prioritising the ones that would index/walk the specific repos \
the answer says hold the missing logic. Each prompt must be specific and \
self-contained (mention the repo + the exact thing to verify). Don't suggest \
prompts that wouldn't change the answer. Prefer the highest-leverage gaps first."""


async def suggest_followups(
    *,
    question: str,
    answer_excerpt: str,
    open_gaps: list[str],
    unwalked_repos: list[str],
    stale_repos: list[str],
    failed_extractions: list[str],
    settings: BifrostSettings | None = None,
    client: ClaudeLlmClient | None = None,
) -> list[FollowupSuggestion]:
    """Turn the answer's gaps + what-failed into actionable next prompts.

    Never raises — returns [] on any error so it can't break the turn.
    """
    settings = settings or get_settings()
    if not (open_gaps or unwalked_repos or stale_repos or failed_extractions):
        return []
    client = client or ClaudeLlmClient(settings)
    _gaps_txt = "\n".join("- " + g for g in open_gaps) or "(none)"
    _unwalked_txt = ", ".join(unwalked_repos) or "(none)"
    _stale_txt = ", ".join(stale_repos) or "(none)"
    _failed_txt = ", ".join(failed_extractions) or "(none)"
    user_message = (
        f"## Original question\n{question}\n\n"
        f"## Answer (excerpt)\n{answer_excerpt[:1500]}\n\n"
        f"## Open gaps the answer listed\n{_gaps_txt}\n\n"
        f"## Repos NOT walked but possibly load-bearing\n{_unwalked_txt}\n\n"
        f"## Stale repos (remote moved since extraction)\n{_stale_txt}\n\n"
        f"## Repos whose extraction FAILED / not indexed\n{_failed_txt}\n\n"
        "Propose up to 4 high-leverage follow-up prompts to close these gaps."
    )
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_FOLLOWUP_SYSTEM,
            user_message=user_message,
            model=settings.reasoning_model,
            json_schema=_FOLLOWUP_SCHEMA,
            max_turns=1,
            timeout_override=90,
        )
    except ClaudeCliError as exc:
        logger.warning("follow-up suggestion failed (%s)", exc)
        return []
    out: list[FollowupSuggestion] = []
    for s in (completion.parsed or {}).get("suggestions", []) or []:
        if isinstance(s, dict) and s.get("prompt"):
            out.append(
                FollowupSuggestion(
                    prompt=str(s["prompt"])[:300],
                    rationale=str(s.get("rationale", ""))[:200],
                    repos_to_index=[str(r) for r in (s.get("repos_to_index") or [])][:5],
                )
            )
    return out
