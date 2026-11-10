"""Follow-up planner — decides whether a chat turn needs fresh research.

When a user asks a follow-up question in the same conversation, the
naive coordinator (today) re-runs the full research pipeline: catalog
ranking, repo selection, RepoAgent fan-out, Jira/Confluence agents,
synthesis. That throws away everything the prior turn just learned.

This module classifies the follow-up against the prior turn's
``ResearchBundle`` and returns a ``ResearchPlan`` with one of:

  * ``NEW_SCOPE``      — topic shift; do a fresh research pass
  * ``EXTEND_BUNDLE``  — same topic, narrow new scope; reuse subgraph
                         and only fan out agents on extension repos
  * ``CLARIFY``        — same topic, no new info needed; answer from
                         the prior bundle

Decision algorithm:

  1. No prior bundle → ``NEW_SCOPE``.
  2. Cosine similarity ≥ 0.85 between current and prior question
     embeddings:
       * if the question mentions repo names not in the prior bundle,
         classify as ``EXTEND_BUNDLE`` with those repos;
       * otherwise ``CLARIFY``.
  3. Cosine 0.50–0.85 → ambiguous. Hand off to ``llm_classifier`` (a
     Haiku-shaped callable) if available; otherwise default to
     ``EXTEND_BUNDLE`` (cheaper than fresh research, still narrow).
  4. Cosine < 0.50 → ``NEW_SCOPE``.

The ``llm_classifier`` hook is injectable so unit tests don't need a
real Claude subprocess. The coordinator wires the real Haiku call in.

Closes part of the PRD §3.7 gap (chat that learns from prior turns —
specifically the part where the chat shouldn't re-research from
scratch on every follow-up).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field

from bifrost.research.bundle_cache import ResearchBundle

logger = logging.getLogger(__name__)


# Mode constants — strings (not Enum) so JSON serialization stays
# trivial in SSE events and the planner output is forward-compatible
# with stored bundles.
MODE_NEW_SCOPE: str = "new_scope"
MODE_EXTEND_BUNDLE: str = "extend_bundle"
MODE_CLARIFY: str = "clarify"

# Cosine-similarity bands. Tuned conservatively: we'd rather extend
# (safe — runs fewer agents) than clarify (risky — may give a stale
# answer if the user introduced a real new question we missed).
_HIGH_SIMILARITY = 0.85
# _LOW_SIMILARITY was previously 0.50, which was too aggressive: real
# follow-up questions about the same system use different surface words
# ("how does X retry?" → "what about backoff?") and routinely land in
# the 0.35–0.50 cosine band. At 0.50 they were classified NEW_SCOPE and
# triggered a full re-run (~80 seconds wasted). Lowering to 0.35 lets
# those follow-ups fall into the ambiguous zone where the LLM classifier
# (or the heuristic-extend fallback) handles them correctly. Genuine
# topic shifts (orders → auth) typically score below 0.25.
_LOW_SIMILARITY = 0.35

# Heuristic: tokens that look like repo names. Repos in the system
# tend to be lowercase-with-hyphens-or-underscores, length 3-40.
_REPO_NAME_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9._-]{2,39}\b")


@dataclass
class ResearchPlan:
    """One planner decision for a single chat turn."""

    mode: str
    reason: str
    similarity: float | None = None
    extension_repos: list[str] = field(default_factory=list)  # repo IDs (or names if IDs unknown)
    extension_repo_names: list[str] = field(default_factory=list)
    reused_bundle: ResearchBundle | None = None
    answer_from_bundle: bool = False

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        out.pop("reused_bundle", None)  # recursive payload — kept out of JSON
        if self.reused_bundle is not None:
            out["reused_bundle_conversation_id"] = self.reused_bundle.conversation_id
        return out


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float | None:
    """Cosine similarity for two L2-normalizable vectors.

    Returns ``None`` if either input is missing or empty. The MiniLM
    embeddings used elsewhere in the codebase are L2-normalized, so
    cosine reduces to a dot product, but we compute the general form
    here so this helper works on any embedding source.
    """
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return None
    return dot / (math.sqrt(na) * math.sqrt(nb))


def find_new_repo_mentions(
    question: str,
    prior_repo_names: Iterable[str],
    available_repo_names: Iterable[str],
) -> list[str]:
    """Repo-name tokens present in the question but not in the prior bundle.

    Filtered to only names that exist in ``available_repo_names`` so
    we don't pick up arbitrary lowercase tokens. Case-insensitive.
    """
    prior_lc = {n.lower() for n in prior_repo_names}
    avail_lc = {n.lower(): n for n in available_repo_names}
    tokens = {m.group(0).lower() for m in _REPO_NAME_TOKEN_RE.finditer(question.lower())}
    new = []
    for token in tokens:
        if token in prior_lc:
            continue
        if token in avail_lc:
            canonical = avail_lc[token]
            if canonical not in new:
                new.append(canonical)
    return new


# ── LLM classifier hook ───────────────────────────────────────────────


LlmClassifierResult = dict  # {"mode": "...", "reason": "..."}
LlmClassifier = Callable[[str, ResearchBundle], Awaitable[LlmClassifierResult]]


_LLM_SYSTEM_PROMPT = (
    "You classify chat follow-up questions for an engineering knowledge "
    "platform. Given a prior research bundle (which repositories, sources, "
    "and answer the previous turn produced) and the user's NEW question, "
    "decide whether the new question is:\n\n"
    "  - NEW_SCOPE: a topic shift requiring fresh research from scratch.\n"
    "  - EXTEND_BUNDLE: same topic, but the user wants more depth or a "
    "narrow expansion (e.g. 'what about retries?', 'how does X integrate?'). "
    "Reuse the prior subgraph; fan out only on extra repos.\n"
    "  - CLARIFY: same topic, the prior answer already contains the "
    "information; the user wants a rephrasing, simplification, or a "
    "specific snippet. Do NOT do new research.\n\n"
    "Return JSON. Be conservative: prefer EXTEND_BUNDLE over CLARIFY when "
    "uncertain."
)


_LLM_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": [MODE_NEW_SCOPE, MODE_EXTEND_BUNDLE, MODE_CLARIFY]},
        "reason": {"type": "string"},
    },
    "required": ["mode", "reason"],
    "additionalProperties": False,
}


async def llm_classify_followup(
    question: str,
    prior_bundle: ResearchBundle,
) -> LlmClassifierResult:
    """Real Haiku-backed classifier. Used by the coordinator at runtime.

    Tests should pass a stub callable to ``classify_followup`` instead
    of touching this — it shells out to ``claude -p`` which is too slow
    and external for unit tests.
    """
    from bifrost.cli.config import get_settings
    from bifrost.extraction.cli_client import ClaudeLlmClient

    settings = get_settings()
    client = ClaudeLlmClient(settings)
    user_message = (
        f"Previous question:\n{prior_bundle.last_question}\n\n"
        f"Previous answer (snippet): {prior_bundle.last_answer[:1500]}\n\n"
        f"Repos previously researched: {', '.join(prior_bundle.repos_used_names) or '(none)'}\n\n"
        f"New question:\n{question}\n"
    )
    completion = await client.call_with_json_schema(
        system_prompt=_LLM_SYSTEM_PROMPT,
        user_message=user_message,
        model=settings.reasoning_model,  # premium — decomposes intent into research plan
        json_schema=_LLM_JSON_SCHEMA,
        max_turns=1,
        # A short timeout makes the heuristic fall back to "extend prior
        # scope", so a team-b follow-up inherits the team-a
        # scope from the prior turn and every code agent returns "irrelevant".
        timeout_override=60.0,
    )
    parsed = completion.parsed
    if not isinstance(parsed, dict) or "mode" not in parsed:
        return {"mode": MODE_EXTEND_BUNDLE, "reason": "llm-output-malformed"}
    return parsed


# ── Public entry point ────────────────────────────────────────────────


async def classify_followup(
    *,
    question: str,
    prior_bundle: ResearchBundle | None,
    question_embedding: list[float] | None = None,
    available_repo_names: Iterable[str] | None = None,
    available_repo_id_by_name: dict[str, str] | None = None,
    llm_classifier: LlmClassifier | None = None,
) -> ResearchPlan:
    """Decide what kind of research the next chat turn needs.

    ``llm_classifier`` is the Haiku-shaped fallback for ambiguous
    cases. Pass ``llm_classify_followup`` (this module) at runtime;
    pass a stub in tests.

    ``available_repo_id_by_name`` maps lowercase repo names to repo
    UUIDs, used to populate ``ResearchPlan.extension_repos`` with
    canonical IDs when the question mentions new repos.
    """
    if prior_bundle is None:
        return ResearchPlan(
            mode=MODE_NEW_SCOPE,
            reason="no prior bundle (first turn or cleared)",
        )

    avail = list(available_repo_names or [])
    new_repo_names = find_new_repo_mentions(
        question,
        prior_bundle.repos_used_names,
        avail,
    )
    sim = cosine_similarity(
        question_embedding,
        prior_bundle.last_question_embedding,
    )

    # Path 1: high similarity — clarify or narrow extend.
    if sim is not None and sim >= _HIGH_SIMILARITY:
        if new_repo_names:
            return _build_extend(
                prior_bundle,
                new_repo_names,
                available_repo_id_by_name,
                reason=f"high similarity ({sim:.2f}), but new repo mentions",
                similarity=sim,
            )
        return ResearchPlan(
            mode=MODE_CLARIFY,
            reason=f"high similarity ({sim:.2f}), no new repo mentions",
            similarity=sim,
            reused_bundle=prior_bundle,
            answer_from_bundle=True,
        )

    # Path 2: low similarity — fresh scope.
    if sim is not None and sim < _LOW_SIMILARITY:
        return ResearchPlan(
            mode=MODE_NEW_SCOPE,
            reason=f"low similarity ({sim:.2f}) — topic shift",
            similarity=sim,
        )

    # Path 3: ambiguous middle, OR no embedding available.
    # Try LLM classifier; fall back to a heuristic.
    llm_failure: str | None = None
    if llm_classifier is not None:
        try:
            verdict = await llm_classifier(question, prior_bundle)
        except Exception as exc:  # noqa: BLE001
            logger.exception("llm_classifier failed; falling back to heuristic")
            llm_failure = f"{type(exc).__name__}: {exc}"
            verdict = None
        if isinstance(verdict, dict) and verdict.get("mode") in {
            MODE_NEW_SCOPE,
            MODE_EXTEND_BUNDLE,
            MODE_CLARIFY,
        }:
            mode = str(verdict["mode"])
            reason = str(verdict.get("reason", "llm-classified"))
            if mode == MODE_EXTEND_BUNDLE:
                return _build_extend(
                    prior_bundle,
                    new_repo_names,
                    available_repo_id_by_name,
                    reason=f"llm: {reason}",
                    similarity=sim,
                )
            if mode == MODE_CLARIFY:
                return ResearchPlan(
                    mode=MODE_CLARIFY,
                    reason=f"llm: {reason}",
                    similarity=sim,
                    reused_bundle=prior_bundle,
                    answer_from_bundle=True,
                )
            return ResearchPlan(
                mode=MODE_NEW_SCOPE,
                reason=f"llm: {reason}",
                similarity=sim,
            )
        if llm_failure is None:
            llm_failure = f"invalid verdict {verdict!r}"

    # Heuristic fallback: when in doubt, EXTEND. Cheaper than fresh
    # research and won't lie to the user — at worst we run a couple
    # of extra agents on the same prior repos.
    return _build_extend(
        prior_bundle,
        new_repo_names,
        available_repo_id_by_name,
        reason=(
            f"ambiguous similarity ({sim if sim is not None else 'n/a'}); "
            + (
                f"llm classifier failed ({llm_failure}) — heuristic extend"
                if llm_failure
                else "no llm classifier — heuristic extend"
            )
        ),
        similarity=sim,
    )


def _build_extend(
    prior_bundle: ResearchBundle,
    new_repo_names: list[str],
    available_repo_id_by_name: dict[str, str] | None,
    *,
    reason: str,
    similarity: float | None,
) -> ResearchPlan:
    """Helper: assemble an EXTEND_BUNDLE plan with extension_repo IDs."""
    id_map = {k.lower(): v for k, v in (available_repo_id_by_name or {}).items()}
    extension_ids: list[str] = []
    for name in new_repo_names:
        rid = id_map.get(name.lower())
        if rid:
            extension_ids.append(rid)
    return ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason=reason,
        similarity=similarity,
        extension_repos=extension_ids,
        extension_repo_names=list(new_repo_names),
        reused_bundle=prior_bundle,
        answer_from_bundle=False,
    )
