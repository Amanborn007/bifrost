"""Intent classifier — decides what to do with every user message.

Four output classes:
  ask      → current chat flow (Q&A)
  plan     → new Task, stop at plan_ready (no execution)
  execute  → new Task, full loop through PR
  quick    → new Task, single-plan shortcut (Layer 10)

Cheap Haiku call (~200ms, <$0.001/request) runs on every chat message.
Explicit prefixes bypass the classifier:

  /ask X     → force ask
  /plan X    → force plan
  /do X      → force execute
  /quick X   → force quick

Jira URL detection is cheap-regex first; Haiku only runs when prefix
and URL detection don't resolve cleanly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import JiraSourceConfig
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


Intent = str  # "ask" | "plan" | "execute" | "quick" | "clarify"


@lru_cache(maxsize=32)
def _jira_url_re(host_pattern: str) -> re.Pattern[str]:
    return re.compile(
        rf"https?://{host_pattern}/browse/([A-Z][A-Z0-9_]+-\d+)",
        re.IGNORECASE,
    )


_JIRA_KEY_PATTERN = re.compile(
    r"\b([A-Z]{2,10}-\d+)\b",
)
_IMPERATIVE_VERBS = re.compile(
    r"^\s*(add|fix|implement|refactor|change|update|remove|"
    r"delete|write|create|build|wire|migrate|upgrade|rename)\b",
    re.IGNORECASE,
)


@dataclass
class IntentResult:
    intent: Intent
    confidence: float  # 0..1
    reasoning: str
    jira_key: str | None = None
    # When intent == "clarify", a single question to ask the user before
    # committing to a direction.
    clarifying_question: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "jira_key": self.jira_key,
            "clarifying_question": self.clarifying_question,
        }


_PREFIX_MAP: dict[str, Intent] = {
    "/ask": "ask",
    "/plan": "plan",
    "/do": "execute",
    "/execute": "execute",
    "/quick": "quick",
}


def _check_explicit_prefix(text: str) -> tuple[Intent, str] | None:
    """Return (intent, stripped_text) if message starts with a recognized
    prefix, else None."""
    stripped = text.lstrip()
    for prefix, intent in _PREFIX_MAP.items():
        if stripped.lower().startswith(prefix):
            remainder = stripped[len(prefix) :].strip()
            return intent, remainder
    return None


def _extract_jira_key(text: str, jira: JiraSourceConfig | None = None) -> str | None:
    jira = jira or get_settings().sources.jira
    m = _jira_url_re(jira.host_pattern()).search(text)
    if m:
        return m.group(1).upper()
    m = _JIRA_KEY_PATTERN.search(text)
    if m:
        # Only treat bare keys as Jira if message is short — in longer
        # prose bare tokens like "SHIP-1234" are often just mentions.
        if len(text) < 300:
            return m.group(1).upper()
    return None


_ROUTER_SYSTEM_PROMPT = """\
You are Bifrost's intent classifier. Every user message gets one of
four intents:

  ask       — user wants an explanation / answer. No code will change.
  plan      — user wants a plan or spec drafted but NOT executed.
              Examples: "plan the migration to X", "draft a spec for Y",
              "what would it take to Z".
  execute   — user wants Bifrost to IMPLEMENT the change (may write
              code, open PRs). Examples: "implement X", "fix SHIP-1234",
              "add retry to Y".
  quick     — small/trivial code change a senior engineer would do in
              <10 minutes. Examples: "fix typo in config", "rename
              variable foo to bar", "bump version to 2.3".
  clarify   — ambiguous; you need ONE clarifying question from the user
              before committing.

Guidance:
  * A Jira URL alone → prefer `clarify`: "Summarize, plan, or implement?"
  * "Implement + Jira" → `execute`.
  * A question ending in "?" almost always → `ask`.
  * Imperative verbs at the start (add/fix/implement/refactor) → likely
    `execute` unless the message is short and the scope is obviously a
    one-liner → `quick`.
  * "plan for ..." / "what would it take ..." → `plan`.

Confidence should reflect how clear the intent is. <0.7 = ambiguous.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object specified by the schema.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["intent", "confidence", "reasoning"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["ask", "plan", "execute", "quick", "clarify"],
        },
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "clarifying_question": {"type": "string"},
    },
}


async def classify_intent(
    message: str,
    *,
    settings: BifrostSettings | None = None,
) -> IntentResult:
    """Classify a single user message into an Intent.

    Fast path: explicit prefix → deterministic return, no LLM call.
    Fast path: pure question (ends in ?) with no imperative → ask.
    LLM path: everything else.
    """
    settings = settings or get_settings()

    # 1. Explicit prefix fast path
    explicit = _check_explicit_prefix(message)
    if explicit is not None:
        intent, remainder = explicit
        return IntentResult(
            intent=intent,
            confidence=1.0,
            reasoning=f"Explicit prefix '{intent}'",
            jira_key=_extract_jira_key(remainder, settings.sources.jira),
        )

    jira_key = _extract_jira_key(message, settings.sources.jira)

    # 2. Pure Q&A heuristic (ends in ?, no imperative verb, no Jira URL)
    stripped = message.strip()
    has_imperative = bool(_IMPERATIVE_VERBS.match(stripped))
    if stripped.endswith("?") and not has_imperative and not jira_key:
        return IntentResult(
            intent="ask",
            confidence=0.9,
            reasoning="Question-shaped, no imperative, no ticket",
            jira_key=None,
        )

    # 3. LLM classification for everything else
    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_ROUTER_SYSTEM_PROMPT,
            user_message=f"Message to classify:\n\n{message}",
            model=settings.reasoning_model,  # premium — picks which Bifrost path
            json_schema=_INTENT_SCHEMA,
            max_turns=1,
            # Opus needs more than 15s for a structured JSON verdict.
            timeout_override=60,
        )
    except ClaudeCliError as exc:
        logger.warning("intent router LLM failed: %s", exc)
        # Fallback: if there's a Jira URL → clarify, else ask.
        return IntentResult(
            intent="clarify" if jira_key else "ask",
            confidence=0.4,
            reasoning=f"Classifier failed ({exc}); defaulting",
            jira_key=jira_key,
            clarifying_question=(
                "Do you want me to summarize this ticket, draft a plan, or try to implement it?"
                if jira_key
                else None
            ),
        )

    data = completion.parsed or {}
    return IntentResult(
        intent=str(data.get("intent", "ask")),
        confidence=float(data.get("confidence", 0.0) or 0.0),
        reasoning=str(data.get("reasoning", "")),
        jira_key=jira_key,
        clarifying_question=data.get("clarifying_question") or None,
    )
