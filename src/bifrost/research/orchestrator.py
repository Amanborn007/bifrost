"""Primary orchestrator agent.

Reads chat context (current question + prior turns) and emits an
``OrchestrationPlan`` that names:

  * The kind of investigation being asked for (architectural,
    impact_radius, debug, discovery, feature_estimate)
  * Seed phrases the graph-walk should use to find entry points
  * Explicitly-named target repos / squads in the question
  * Optional walk parameter overrides (depth / breadth /
    confidence floor / edge directions) — None means "no opinion,
    use settings default"
  * A 1-2 sentence reasoning string the UI can show

The plan is one Haiku call. Falls back to a no-op plan
(intent="discovery", all walk fields None) when the LLM fails so
the chat turn still proceeds with settings defaults.

Plan-vs-settings precedence:
  user-touched settings   >   plan's overrides   >   factory defaults

The "user-touched" signal is whether the conversation's stored
JSONB is non-empty. Empty JSONB ⇒ user never opened the panel,
so the plan's overrides take effect. Non-empty ⇒ user has
expressed an opinion; settings always win for the parameters they
cover. The plan still contributes intent + seed_phrases +
reasoning regardless.

Distinct from ``planner.ResearchPlan`` which is a different
NEW_SCOPE / EXTEND_BUNDLE / CLARIFY decision (about whether this
turn re-uses prior research). Naming differs to avoid collision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.api.schemas.chat_settings import ChatSettings
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


# ── Public types ────────────────────────────────────────────────


Intent = Literal[
    "architectural",
    "impact_radius",
    "debug",
    "discovery",
    "feature_estimate",
]


@dataclass
class OrchestrationPlan:
    """The orchestrator's read of the user's intent + an optional
    walk-parameter recommendation.

    Walk-parameter fields are ``Optional`` — ``None`` means "no
    opinion, fall back to chat settings default". Only fields the
    LLM explicitly set come back non-None.
    """

    intent: Intent
    seed_phrases: list[str] = field(default_factory=list)
    target_repos: list[str] = field(default_factory=list)
    target_squads: list[str] = field(default_factory=list)
    # Walk-parameter overrides. None = no opinion.
    walk_depth: int | None = None
    walk_breadth_soft_cap: int | None = None
    confidence_floor: float | None = None
    edge_directions: Literal["upstream", "downstream", "both"] | None = None
    edge_type_overrides: list[str] | None = None
    # 1-2 sentence reasoning the UI can show ("classified as
    # impact_radius — walking 3 hops upstream from FulfillmentExecutor").
    reasoning: str = ""
    # Set to True when ``run_orchestrator`` falls back to a no-op
    # plan because the LLM failed. Coordinator can choose to log
    # but should still proceed.
    is_fallback: bool = False


@dataclass
class ResolvedWalkParams:
    """The final, settings-and-plan-merged walk parameters used for
    Stage 3 (blast-radius walk)."""

    walk_depth: int
    walk_breadth_soft_cap: int
    confidence_floor: float
    edge_directions: Literal["upstream", "downstream", "both"]
    edge_types: list[str]


# ── System prompt + JSON schema ─────────────────────────────────


_ORCHESTRATOR_SYSTEM_PROMPT = f"""\
You are the orchestration agent for an engineering-knowledge system.

Given a user's chat question (and optional prior conversation turns),
emit a structured plan describing how to research the answer:

1. **intent** — one of:
   * `architectural`   — "how does X work?", "what are the
     components of Y?", "explain the structure of Z".
   * `impact_radius`   — "if I change X, what breaks?", "who
     depends on X?", "what does X call?". Walks the graph upstream
     and/or downstream from the named entity. Prefer shallow depth
     + breadth cap.
   * `debug`           — "why does X fail in this case?", "what's
     causing Y?". Narrow focus on the specific entity + its
     immediate neighbours.
   * `discovery`       — "what services do we have?", "find me
     all repos that do X". Broad walk, lower confidence floor OK.
   * `feature_estimate` — "what would it take to add Z?", "how
     long to migrate from A to B?". Mid-depth walk, both
     directions, mid-high confidence floor.

2. **seed_phrases** — 1-5 short phrases the graph-walk should
   use to find seed entities. Pull these from the question's
   nouns and named entities. Example: "OrderRouter, tenant plan
   routing, order-intake". Don't paraphrase — preserve original
   capitalisation when the user named a class / file / repo
   literally.

3. **target_repos** — repository names mentioned literally in
   the question (e.g. "order-intake", "inventory-platform"). Empty list
   if none mentioned.

4. **target_squads** — team / squad names mentioned literally
   (e.g. "SHIP", "INV", "OPS"). Empty list if none.

5. **Walk-parameter overrides** — these are OPTIONAL. Only set
   them when the question genuinely benefits from a non-default
   value. Otherwise leave them out (the system uses the user's
   settings).
   * `walk_depth` (1-6): hops from seeds. impact_radius typically 2-3;
     architectural 2-3; discovery 1; debug 2.
   * `walk_breadth_soft_cap` (20-2000): max nodes per walk before
     warning. discovery may need 500+; debug needs <50.
   * `confidence_floor` (0.0-1.0): minimum edge confidence.
     impact_radius wants 0.7+ (high-trust edges only) so the user
     doesn't get false positives. discovery can drop to 0.4.
   * `edge_directions` ("upstream" | "downstream" | "both"):
     impact-of-changing-X is downstream; who-depends-on-X is upstream.
   * `edge_type_overrides`: rare. Only when the question is
     about a specific kind of relationship (e.g. "what events
     does X produce" → ["produces_event", "consumes_event"]).

6. **reasoning** — 1-2 sentences naming what you decided and why.
   The UI shows this so the user knows what the system is doing.

{BOUNDARY_SENTENCE}

Respond ONLY with the structured ``OrchestrationPlan`` JSON.
"""


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["intent", "reasoning"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "architectural",
                "impact_radius",
                "debug",
                "discovery",
                "feature_estimate",
            ],
        },
        "seed_phrases": {"type": "array", "items": {"type": "string"}},
        "target_repos": {"type": "array", "items": {"type": "string"}},
        "target_squads": {"type": "array", "items": {"type": "string"}},
        "walk_depth": {"type": ["integer", "null"], "minimum": 1, "maximum": 6},
        "walk_breadth_soft_cap": {
            "type": ["integer", "null"],
            "minimum": 20,
            "maximum": 2000,
        },
        "confidence_floor": {
            "type": ["number", "null"],
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "edge_directions": {
            "type": ["string", "null"],
            "enum": ["upstream", "downstream", "both", None],
        },
        "edge_type_overrides": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "reasoning": {"type": "string"},
    },
}


# ── Public entry points ─────────────────────────────────────────


async def run_orchestrator(
    *,
    question: str,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
) -> OrchestrationPlan:
    """One Haiku call, falls back to no-op on failure.

    The LLM never sees raw user content unwrapped — both
    ``question`` and ``prior_turns`` go inside XML boundary tags
    so prompt-injection attempts in chat history can't redirect
    the orchestrator.
    """
    settings = settings or get_settings()

    user_msg_parts: list[str] = [
        wrap_untrusted(question, "user_question"),
    ]
    if prior_turns:
        user_msg_parts.append(
            wrap_untrusted(prior_turns, "prior_conversation"),
        )
    user_message = "\n\n".join(user_msg_parts)

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_ORCHESTRATOR_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.reasoning_model,  # premium — picks the entire run shape
            json_schema=_PLAN_SCHEMA,
            max_turns=1,
            timeout_override=45,
        )
    except ClaudeCliError as exc:
        logger.warning(
            "Orchestrator fell back to no-op plan: %s",
            str(exc)[:200],
        )
        return OrchestrationPlan(
            intent="discovery",
            reasoning="Orchestrator LLM failed; using settings defaults.",
            is_fallback=True,
        )
    except Exception as exc:  # noqa: BLE001 — last-mile guard
        logger.warning(
            "Orchestrator unexpected failure: %s",
            str(exc)[:200],
        )
        return OrchestrationPlan(
            intent="discovery",
            reasoning="Orchestrator LLM failed; using settings defaults.",
            is_fallback=True,
        )

    data = completion.parsed or {}
    return OrchestrationPlan(
        intent=str(data.get("intent", "discovery")),  # type: ignore[arg-type]
        seed_phrases=list(data.get("seed_phrases") or []),
        target_repos=list(data.get("target_repos") or []),
        target_squads=list(data.get("target_squads") or []),
        walk_depth=_int_or_none(data.get("walk_depth")),
        walk_breadth_soft_cap=_int_or_none(data.get("walk_breadth_soft_cap")),
        confidence_floor=_float_or_none(data.get("confidence_floor")),
        edge_directions=_dir_or_none(data.get("edge_directions")),
        edge_type_overrides=(
            list(data.get("edge_type_overrides")) if data.get("edge_type_overrides") else None
        ),
        reasoning=str(data.get("reasoning", "")),
        is_fallback=False,
    )


def merge_plan_with_settings(
    plan: OrchestrationPlan,
    chat_settings: ChatSettings,
    *,
    user_touched_settings: bool,
    settings: BifrostSettings | None = None,
) -> ResolvedWalkParams:
    """Apply the plan-vs-settings precedence rule.

    When the user has touched the settings panel
    (``user_touched_settings=True``), their settings always win for
    the walk parameters. The plan still contributes intent +
    seed_phrases + reasoning at the call-site, just not walk shape.

    When the user hasn't touched settings (stored JSONB empty), the
    plan's parameters override the factory defaults wherever it
    expresses an opinion.

    **Known limitation:** ``user_touched_settings``
    is a BINARY signal — derived from "is the conversation's stored
    JSONB dict empty?" — not per-field. If the user PATCHed only
    ``walk_depth`` but left every other slider at its factory
    default, all of those untouched sliders still lock in this
    conversation: the plan can't suggest a smarter
    ``confidence_floor`` for an impact-radius question, for example.

    The trade-off is intentional for V1: per-field detection would
    require either (a) storing only the diff from defaults in JSONB
    (breaks the route's "merge full ChatSettings" pattern), or
    (b) tracking which fields were explicitly set on each PATCH
    request (extra metadata layer). Both are bigger lifts than the
    binary signal warrants for the current UX scope. If user
    feedback shows the binary limitation hurts answer quality,
    revisit by switching storage to diff-from-defaults — the
    Pydantic merge layer in ``settings_from_stored`` already
    handles partial dicts gracefully, so the route layer is the
    only file that needs to change.
    """
    server = settings or get_settings()
    if user_touched_settings:
        return ResolvedWalkParams(
            walk_depth=chat_settings.walk_depth,
            walk_breadth_soft_cap=clamp_walk_breadth(chat_settings.walk_breadth_soft_cap, server),
            confidence_floor=chat_settings.confidence_floor,
            edge_directions=chat_settings.edge_directions,
            edge_types=list(chat_settings.edge_types),
        )
    # Plan overrides defaults where it has an opinion.
    return ResolvedWalkParams(
        walk_depth=plan.walk_depth or chat_settings.walk_depth,
        walk_breadth_soft_cap=clamp_walk_breadth(
            plan.walk_breadth_soft_cap or chat_settings.walk_breadth_soft_cap, server
        ),
        confidence_floor=(
            plan.confidence_floor
            if plan.confidence_floor is not None
            else chat_settings.confidence_floor
        ),
        edge_directions=plan.edge_directions or chat_settings.edge_directions,
        edge_types=(
            list(plan.edge_type_overrides)
            if plan.edge_type_overrides
            else list(chat_settings.edge_types)
        ),
    )


def clamp_walk_breadth(value: int, settings: BifrostSettings) -> int:
    """Cap ``value`` at the server's ``research_walk_breadth_ceiling``.

    PATCH /settings rejects values above it; this is the same bound
    enforced at use for stored settings and plan overrides.
    """
    ceiling = settings.research_walk_breadth_ceiling
    if value <= ceiling:
        return value
    logger.warning(
        "walk_breadth_soft_cap=%d exceeds research_walk_breadth_ceiling=%d; clamping",
        value,
        ceiling,
    )
    return ceiling


# ── Helpers ─────────────────────────────────────────────────────


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dir_or_none(v: Any) -> Literal["upstream", "downstream", "both"] | None:
    if v in ("upstream", "downstream", "both"):
        return v  # type: ignore[return-value]
    return None
