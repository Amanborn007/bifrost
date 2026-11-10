"""Scope matching: connect collaboration sessions to handover docs.

The clarifier consults this module to find handover docs
relevant to a session's requirement, then injects their body_md
into the prompt as grounded context. Doubts the model can answer
directly from those snippets are resolved as ``answered_by_doc``
with the handover URN as the citation.

A Haiku-driven scope classifier is the primary matcher.
A short Haiku call (~$0.001 / ~500ms — cheap-ish but **not** free,
do NOT call this on every keystroke; the clarifier already debounces
to once per session create + once per follow-up turn) is given a
list of ``<candidate_handover>`` metadata tags and the user's
free-text intent, and returns up to 3 indices ordered by relevance.

If Haiku fails (CLI error, timeout, empty result) we silently fall
back to the substring matcher: word-boundary substring match
of ``scope_value`` against the haystack. That keeps the clarifier
working even when the LLM subprocess is unavailable, at the cost of
only catching matches where the asker named the team / domain / repo
verbatim.

Future iterations:

  * Embedding similarity over the handover-doc summary, for fuzzy
    matches that the Haiku metadata-only prompt can't disambiguate
    (e.g. when ``scope_value`` is opaque like ``team-xyz``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient
from bifrost.storage.models import HandoverDoc
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


# Cap on matched handovers per session — keeps the clarifier prompt
# bounded. 3 is enough to cover repo + domain + team in the common
# cross-functional case; higher would crowd out the chat history.
_DEFAULT_MATCH_LIMIT = 3

# Cap on per-handover Markdown body sliced into the prompt. We can
# afford to include several pages of context but not entire docs;
# 4000 chars is roughly 1k tokens, which keeps a 3-handover injection
# under ~3k tokens of grounding.
_PER_HANDOVER_BODY_CHAR_CAP = 4000

# Bounded set of handover candidates considered per match call. The
# Haiku prompt sends only ``scope_kind`` + ``scope_value`` per row, so
# 200 candidates is roughly ~5KB of metadata — well within Haiku's
# context budget and cheap to classify.
_CANDIDATE_LIMIT = 200

# Haiku call budget: short timeout because this runs in front of the
# user-facing clarifier. If it doesn't return inside this window we
# bail to the substring fallback so the clarifier keeps moving.
_HAIKU_TIMEOUT_SECONDS = 15


_MATCHER_SYSTEM_PROMPT = """\
You are Bifrost's scope classifier. You receive a free-text user
intent (wrapped in <user_intent>) and a list of candidate handover
docs as self-closing <candidate_handover> tags with index, scope_kind,
and scope_value attributes.

Pick the candidate handover indices most relevant to the user_intent.
Return at most 3 indices, ordered by relevance (most relevant first).
Return an empty array if NONE of the candidates are relevant.

Rules:
  * Match on semantic intent, not just literal token overlap. E.g.
    "we need to add a carrier" should match a handover whose
    scope_value is inventory/stock or orders/carriers, even if the words
    "carrier", "inventory", and "stock" never co-occur.
  * Don't fabricate indices not present in the candidate list.
  * If two candidates look equally relevant, prefer the one whose
    scope_kind is more specific (repo > domain > team).
  * Better to return [] than guess. The substring fallback will catch
    the obvious cases.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object specified by the schema.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_MATCHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["matched_indices"],
    "properties": {
        "matched_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
    },
}


async def _haiku_match_handovers(
    haystack: str,
    candidates: Sequence[HandoverDoc],
    settings: BifrostSettings,
) -> list[int] | None:
    """Run Haiku over the haystack + candidate metadata, return matched
    1-based indices into ``candidates`` (in relevance order), or
    ``None`` on any failure (CLI error, parse error, empty result).

    The caller is responsible for falling back to substring matching
    when this returns ``None``.
    """
    if not candidates:
        return None

    # Build the candidate list as self-closing XML tags. We send only
    # metadata (scope_kind + scope_value) — handover bodies would blow
    # the prompt budget, and Haiku-from-metadata is the right level of
    # cheap precision before we'd consider an embedding lookup.
    candidate_lines = []
    for i, c in enumerate(candidates, start=1):
        # html.escape isn't strictly necessary on these DB-controlled
        # fields, but the boundary contract is consistent: any value
        # routed through the prompt must be escaped at the seam.
        scope_kind = (c.scope_kind or "").replace('"', "&quot;")
        scope_value = (c.scope_value or "").replace('"', "&quot;")
        candidate_lines.append(
            f'<candidate_handover index="{i}" '
            f'scope_kind="{scope_kind}" '
            f'scope_value="{scope_value}" />'
        )

    user_message = (
        wrap_untrusted(haystack, "user_intent")
        + "\n\n"
        + "Candidate handovers:\n"
        + "\n".join(candidate_lines)
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_MATCHER_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.triage_model,  # Haiku — cheap/fast
            json_schema=_MATCHER_SCHEMA,
            max_turns=1,
            timeout_override=_HAIKU_TIMEOUT_SECONDS,
        )
    except ClaudeCliError as exc:
        logger.warning("haiku scope_match failed: %s", exc)
        return None

    data = completion.parsed or {}
    raw_indices = data.get("matched_indices") or []
    if not isinstance(raw_indices, list):
        return None

    # Defensive validation: the schema enforces int >= 1, but a
    # mis-behaving model could still emit out-of-range indices. Drop
    # anything that doesn't point at a real candidate.
    valid: list[int] = []
    seen: set[int] = set()
    for idx in raw_indices:
        if not isinstance(idx, int):
            continue
        if idx < 1 or idx > len(candidates):
            continue
        if idx in seen:
            continue
        seen.add(idx)
        valid.append(idx)

    if not valid:
        return None
    return valid


def _substring_match_handovers(
    candidates: Sequence[HandoverDoc],
    haystack: str,
    limit: int,
) -> list[HandoverDoc]:
    """Fallback: word-boundary-ish substring match of scope_value
    against the haystack. Conservative — only fires when the asker
    named the scope verbatim. Cheap (<1ms), no LLM, no network.
    """
    needle_lower = haystack.lower()
    matched: list[HandoverDoc] = []
    for d in candidates:
        if not d.scope_value:
            continue
        # Word-boundary-ish match: ``order-intake`` should match in
        # "order-intake" or "order intake" but not "reorder-intake".
        # Build a regex that allows hyphens or spaces between tokens.
        tokens = re.findall(r"[a-z0-9]+", d.scope_value.lower())
        if not tokens:
            continue
        pattern = r"\b" + r"[\s\-_]*".join(re.escape(t) for t in tokens) + r"\b"
        if re.search(pattern, needle_lower):
            matched.append(d)
            if len(matched) >= limit:
                break
    return matched


async def find_matching_handovers(
    *,
    repo: EntityRepository,
    haystack: str,
    limit: int = _DEFAULT_MATCH_LIMIT,
) -> list[HandoverDoc]:
    """Return up to ``limit`` handover docs relevant to ``haystack``.

    Primary path: Haiku scope classifier picks the most relevant
    candidates (~$0.001, ~500ms). Fallback: substring matcher fires
    when Haiku fails or returns empty (and the substring matcher
    finds anything literal — it's strictly less powerful).

    Ordering of the candidate pool is by ``last_generated_at`` desc —
    most recently refreshed wins on ties — so the matcher prefers
    grounded recent knowledge over stale snapshots. Docs without a
    generated timestamp (e.g. stub fallbacks from the generator) are
    excluded.

    Empty haystack → empty list. Bounded ``LIMIT`` query upstream so
    we never load thousands of rows.

    NOTE on cost: each call costs roughly $0.001 in Haiku tokens
    (~500-1000 input tokens, <50 output tokens). The clarifier debounces
    this to once per session create + once per follow-up turn — do NOT
    sprinkle this in a hot loop. If you find yourself wanting that,
    move the matcher down to embeddings.
    """
    if not haystack or not haystack.strip():
        return []

    # Pull a bounded set of recent docs. Both Haiku and substring
    # paths share the same candidate pool so behavior is comparable.
    stmt = (
        select(HandoverDoc)
        .where(HandoverDoc.last_generated_at.is_not(None))
        .order_by(HandoverDoc.last_generated_at.desc())
        .limit(_CANDIDATE_LIMIT)
    )
    rows: Sequence[HandoverDoc] = (await repo._session.execute(stmt)).scalars().all()

    if len(rows) == 0:
        return []

    if len(rows) == 1:
        # No decision to make — Haiku would just be cost. Return the
        # lone candidate as long as it has a scope_value to ground on.
        only = rows[0]
        if only.scope_value:
            logger.info(
                "scope_match path=single_candidate matched=%s",
                f"{only.scope_kind}:{only.scope_value}",
            )
            return [only]
        return []

    settings = get_settings()

    # Primary path: Haiku.
    haiku_result = await _haiku_match_handovers(haystack, rows, settings)
    if haiku_result:
        # Map 1-based indices back to docs and cap at limit.
        matched = [rows[i - 1] for i in haiku_result[:limit]]
        logger.info(
            "scope_match path=haiku haystack_len=%d matched=%s",
            len(haystack),
            [f"{d.scope_kind}:{d.scope_value}" for d in matched],
        )
        return matched

    # Fallback: substring matcher. Either Haiku failed or it returned
    # an empty list — the substring path is strictly less powerful
    # (only catches verbatim mentions) but is the safety net.
    matched = _substring_match_handovers(rows, haystack, limit)
    if matched:
        logger.info(
            "scope_match path=substring_fallback haystack_len=%d matched=%s",
            len(haystack),
            [f"{d.scope_kind}:{d.scope_value}" for d in matched],
        )
    else:
        logger.info(
            "scope_match path=substring_fallback haystack_len=%d matched=[]",
            len(haystack),
        )
    return matched


def read_handover_body(doc: HandoverDoc) -> str | None:
    """Read the on-disk Markdown body, truncated to keep prompts
    bounded. Returns ``None`` if the file is gone or unreadable —
    caller should silently skip rather than fail the whole turn."""
    if not doc.fs_path:
        return None
    try:
        body = Path(doc.fs_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "couldn't read handover body at %s: %s",
            doc.fs_path,
            exc,
        )
        return None
    if len(body) > _PER_HANDOVER_BODY_CHAR_CAP:
        return body[:_PER_HANDOVER_BODY_CHAR_CAP] + f"\n\n[truncated — full doc at {doc.fs_path}]"
    return body


def handover_urn(doc: HandoverDoc) -> str:
    """Stable citation URN for a handover doc. Used as ``source_ref``
    on doubts the clarifier resolves from the doc's body."""
    return f"handover:{doc.scope_kind}/{doc.scope_value}#{doc.id}"
