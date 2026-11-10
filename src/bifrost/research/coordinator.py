"""Multi-repo research coordinator (fan-out → synthesis).

This replaces the single-agent chat path for scoped-to-domain (or multi-repo
or unscoped) questions. Flow:

    question → scope resolution → freshness/clone ensure (parallel)
             → router (Haiku)    → rank top-K relevant repos
             → fan-out sub-agents (Sonnet × N, parallel)
             → synthesis agent   (Sonnet, streamed)
             → SSE events out

The coordinator is a pure coroutine — it takes a conversation context and
yields dicts that the query_worker wraps as SSE events. It does NOT touch
DB persistence; that stays in query_worker.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import JiraSourceConfig
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient, Usage
from bifrost.research.agent_cache import (
    cached_agent,
    compute_scope_hash,
    cost_from_usage,
)
from bifrost.research.bundle_cache import (
    BundleSource,
    ResearchBundle,
    get_bundle_cache,
)
from bifrost.research.clarify import (
    _is_eligible as _is_clarify_eligible,
)
from bifrost.research.clarify import (
    run_clarify_synthesis,
)
from bifrost.research.confluence_agent import (
    ConfluenceReport,
    research_confluence_for_repo,
)
from bifrost.research.external_agent import (
    ExternalFindings,
    research_external,
)
from bifrost.research.freshness import (
    RepoFreshness as RepoFreshState,
)
from bifrost.research.freshness import (
    ensure_fresh_clones,
)
from bifrost.research.gdocs_agent import (
    GDocsReport,
    research_gdocs_for_repo,
)
from bifrost.research.github_agent import (
    GitHubReport,
    research_github_for_repo,
)
from bifrost.research.jira_agent import (
    JiraReport,
    research_jira_for_repo,
)
from bifrost.research.planner import (
    MODE_CLARIFY,
    MODE_EXTEND_BUNDLE,
    classify_followup,
    llm_classify_followup,
)
from bifrost.research.repo_agent import (
    CodeShard,
    EntitySnippet,
    GraphEdge,
    RepoReport,
    merge_repo_reports,
    research_repo,
    shard_repo_for_code_agents,
)
from bifrost.research.router import (
    RepoCandidate,
    RepoRanking,
    rank_repos,
)
from bifrost.research.slack_agent import (
    SlackReport,
    research_slack_for_repo,
)
from bifrost.research.subgraph_hydrate import (
    HydratedSubgraph,
    hydrate_prior_subgraph,
    hydrated_summary,
)
from bifrost.storage.models import (
    ConfluencePage,
    Conversation,
    Domain,
    Entity,
    EntityCommunity,
    EntityRelationship,
    JiraIssue,
    OrgRepo,
    Repo,
    RepoOwnership,
)
from bifrost.storage.models import (
    RepoFreshness as FreshnessRow,
)
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


# Cross-repo concurrency cap — the most repo-bundle subprocesses we'll
# run in parallel. The *number of repos discovered* per question is no
# longer hardcoded (replaced the prior `MAX_REPOS_PER_QUESTION = 5`
# with the user's ``walk_breadth_soft_cap`` setting); this is just the
# scheduler ceiling so we don't spawn hundreds of `claude -p` processes
# at once.
_FANOUT_CONCURRENCY = 5

# Per-agent in-flight task registry. Keyed by
# ``(conv_id, repo_id, source)`` — e.g. ``("abc-123", "r1-uuid",
# "confluence")``. Lets the cancel endpoint reach a specific
# Jira/Confluence/GitHub/Slack agent task and call ``Task.cancel()``
# on it while the rest of the conversation's research keeps running.
# Cleared per task in a ``finally`` block so a finished task doesn't
# linger and accidentally claim a future cancel.
_AGENT_TASKS: dict[tuple[str, str, str], asyncio.Task[Any]] = {}


def _register_agent_task(
    conv_id: str,
    repo_id: str,
    source: str,
    task: asyncio.Task[Any],
) -> None:
    """Track an in-flight per-agent task so the cancel endpoint can
    reach it. The caller is responsible for clearing this on
    completion (typically via a wrapper coroutine that calls
    ``_unregister_agent_task`` in ``finally``)."""
    _AGENT_TASKS[(conv_id, repo_id, source)] = task


def _unregister_agent_task(conv_id: str, repo_id: str, source: str) -> None:
    """Drop a per-agent task once it's done. Idempotent."""
    _AGENT_TASKS.pop((conv_id, repo_id, source), None)


def cancel_agent_task(
    conv_id: str,
    repo_id: str,
    source: str,
) -> bool:
    """Cancel a single in-flight per-agent task.

    Returns True if a running task was cancelled; False if no such
    task exists or it's already finished. The task's ``finally``
    block will clear the registry entry asynchronously.
    """
    key = (conv_id, repo_id, source)
    task = _AGENT_TASKS.get(key)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def _stream_with_heartbeat(
    stream: AsyncIterator[dict[str, Any]],
    interval_s: float = 10.0,
) -> AsyncIterator[dict[str, Any]]:
    """Wrap an async iterator so that any silence longer than
    ``interval_s`` between consecutive items emits a sentinel
    ``{"type": "_heartbeat"}``. Used during synthesis to keep the
    frontend watchdog rearmed even when Claude pauses 60-120s
    between tokens on a complex answer.

    The original stream's items are passed through unchanged.
    On generator close (caller breaks early), the underlying
    producer is cancelled cleanly.
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    DONE = object()

    async def _producer() -> None:
        try:
            async for item in stream:
                await queue.put(("item", item))
        except BaseException as exc:  # noqa: BLE001 — propagate via queue
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", DONE))

    prod_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(
                    queue.get(),
                    timeout=interval_s,
                )
            except TimeoutError:
                yield {"type": "_heartbeat"}
                continue
            if kind == "done":
                return
            if kind == "error":
                raise val  # type: ignore[misc]
            yield val
    finally:
        if not prod_task.done():
            prod_task.cancel()
            try:
                await prod_task
            except (asyncio.CancelledError, BaseException):  # noqa: BLE001
                pass


async def _tracked_external_agent(
    *,
    conv_id: str,
    repo_id: str,
    source: str,
    coro: Any,
    cancelled_report: Any,
) -> Any:
    """Run one external agent (jira/confluence/github/slack) under
    cancel tracking.

    Registers the wrapper task in ``_AGENT_TASKS`` so the
    ``/agents/<key>/cancel`` endpoint can call ``Task.cancel()`` on
    it. On user-initiated cancel, swallows ``CancelledError`` and
    returns ``cancelled_report`` — a synthetic "I was stopped"
    report so the bundle's other agents (code, jira, etc.) keep
    running and synthesis still gets a marker for the cancelled
    source. ``finally`` always clears the registry entry.

    NOTE: this intentionally does NOT re-raise ``CancelledError`` —
    per-agent cancel is a fire-and-forget user gesture; the
    conversation as a whole continues. To cancel the entire
    conversation, use ``/conversations/<id>/cancel`` (which
    cancels the outer query task).
    """
    task = asyncio.current_task()
    if task is not None:
        _register_agent_task(conv_id, repo_id, source, task)
    try:
        return await coro
    except asyncio.CancelledError:
        return cancelled_report
    finally:
        _unregister_agent_task(conv_id, repo_id, source)


# Total cross-shard concurrency cap for a single conversation.
# `_FANOUT_CONCURRENCY` bounds the number of *repo bundles* in flight,
# but each bundle can spawn up to `agents_per_repo_max=8` shards. A
# 5-repo × 8-shard fan-out would peak at 40 concurrent Sonnet
# subprocesses without any throttle here. Setting this to ~8 means we
# spend on at most one max-sharded repo at full breadth at a time;
# others queue. The cap is intentionally a per-conversation shared
# semaphore (created in `run_research`, passed into `_build_bundle`)
# so an unsharded multi-repo turn isn't slowed by it.
_SHARD_CONCURRENCY = 8

# Cap on agents WITHIN a single repo bundle.
# Code is always run; Jira + Confluence are optional per the ownership manifest.
MAX_AGENTS_PER_REPO = 3

# How many top-K entities to feed each repo's CodeAgent. Too many wastes
# tokens; too few and the agent has to Grep from scratch. 25 works.
ENTITIES_PER_SUBAGENT = 25

# ── Dataclasses for the pipeline ────────────────────────────────────────


@dataclass
class RepoBundle:
    """All per-repo agent reports for one question + repo pair.

    The coordinator fans out to N repos × M agents; each repo's outputs
    are bundled here before synthesis. Synthesis receives a list of
    these, which keeps cross-repo structure readable.
    """

    repo_id: str
    repo_name: str
    code: RepoReport | None = None
    jira: JiraReport | None = None
    confluence: ConfluenceReport | None = None
    github: GitHubReport | None = None
    slack: SlackReport | None = None
    gdocs: GDocsReport | None = None
    # Raw entity snapshot (with 1-hop graph) used by the code agent — kept
    # here so synthesis can ground claims in URNs and cite specific
    # components rather than re-paraphrasing the agent's summary.
    entities: list[EntitySnippet] = field(default_factory=list)
    # room to add: incidents, runtime, ...


@dataclass
class SynthesisUsage:
    """Aggregated usage across router + all sub-agents + synthesis."""

    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    n_calls: int = 0

    def add(self, u: Usage | None) -> None:
        if u is None:
            return
        self.total_cost_usd += float(u.total_cost_usd or 0.0)
        self.input_tokens += int(u.input_tokens or 0)
        self.output_tokens += int(u.output_tokens or 0)
        self.cache_read_tokens += int(u.cache_read_input_tokens or 0)
        self.n_calls += 1


# ── Scope resolution ───────────────────────────────────────────────────


# Stop-words dropped from question keyword extraction. Tokens that are
# too common to discriminate ("team", "service", "code") drag the
# matcher into matching everything → nothing useful. Keep this list
# minimal — over-pruning loses signal.
_QUESTION_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "this",
        "that",
        "are",
        "what",
        "how",
        "why",
        "when",
        "where",
        "who",
        "does",
        "do",
        "did",
        "team",
        "service",
        "system",
        "code",
        "repo",
        "all",
        "some",
        "any",
        "you",
        "your",
        "our",
        "their",
        "show",
        "tell",
        "give",
        "list",
        "explain",
        "describe",
        "about",
        "between",
        "across",
        "etc",
    }
)


# Above this candidate count, the Haiku router can't rank everything in
# one shot — its structured-output budget caps the rankings array. The
# coordinator then backfills the unranked tail as "low" relevance,
# stable-sort keeps the alphabetical input order, and top_k=5 always
# picks the same 5 alphabetical-first repos regardless of question.
# When the candidate pool is bigger than this, we keyword-prefilter
# down before sending to the router.
_ROUTER_PREFILTER_THRESHOLD = 50

# ── Jira ticket chain pre-fetcher ────────────────────────────────────────────
import re as _re

_JIRA_CLOUD_HOST = r"[a-z0-9.-]+\.atlassian\.net"


@lru_cache(maxsize=32)
def _jira_url_re(host_pattern: str) -> _re.Pattern[str]:
    return _re.compile(
        rf"https?://{host_pattern}/browse/([A-Z][A-Z0-9]+-\d+)",
        _re.IGNORECASE,
    )


_JIRA_KEY_RE = _re.compile(r"(?<![/\w])([A-Z][A-Z0-9]{1,9}-\d+)(?!\w)")

_JIRA_CHAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "type": {"type": "string"},
                    "assignee": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "string"},
                    "linked_tickets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "summary": {"type": "string"},
                                "description": {"type": "string"},
                                "link_type": {"type": "string"},
                                "status": {"type": "string"},
                            },
                            "required": ["key", "summary"],
                        },
                    },
                },
                "required": ["key", "summary"],
            },
        }
    },
    "required": ["tickets"],
}

_JIRA_CHAIN_TOOL_NAMES = (
    "getJiraIssue",
    "getJiraIssueRemoteIssueLinks",
    "searchJiraIssuesUsingJql",
    "getAccessibleAtlassianResources",
)

_JIRA_CHAIN_SYSTEM = """\
You are a Jira data collector. Use {mcp:atlassian:getJiraIssue} to fetch each
requested ticket. For EACH ticket also fetch:
  1. Every issue in its issuelinks array (linked, blocking/blocked-by, clones, etc.)
  2. All subtasks listed in the subtasks field
  3. The parent epic (parent field value) if present

Use fields=["summary","description","status","assignee","issuetype","labels",
"subtasks","issuelinks","parent","comment"] on every getJiraIssue call.
Return ALL fetched tickets in the JSON schema. Do not truncate descriptions.
"""


_EPIC_INSTRUCTION = "  3. The parent epic (parent field value) if present\n"
_EPIC_FIELDS = '"issuelinks","parent",'


def _jira_chain_system(settings: BifrostSettings) -> str:
    """Jira chain prompt; adds ``sources.jira.epic_link_field`` only when configured."""
    prompt = _JIRA_CHAIN_SYSTEM
    field = settings.sources.jira.epic_link_field
    if field:
        for old, new in (
            (
                _EPIC_INSTRUCTION,
                f"  3. The parent epic (parent or {field} field value) if present\n",
            ),
            (_EPIC_FIELDS, f'"issuelinks","parent","{field}",'),
        ):
            if old not in prompt:
                raise RuntimeError(f"Jira chain prompt lost the anchor {old!r}")
            prompt = prompt.replace(old, new, 1)
    return settings.mcp.render(prompt)


def _extract_jira_keys(question: str, jira: JiraSourceConfig | None = None) -> set[str]:
    jira = jira or get_settings().sources.jira
    keys: set[str] = set()
    for m in _jira_url_re(jira.host_pattern(_JIRA_CLOUD_HOST)).finditer(question):
        keys.add(m.group(1).upper())
    for m in _JIRA_KEY_RE.finditer(question):
        keys.add(m.group(1).upper())
    return keys


async def _prefetch_jira_chain(question: str, settings: BifrostSettings) -> str:
    """Detect Jira keys/URLs in the question, fetch the full ticket chain
    (ticket + all linked tickets + subtasks + parent epic) via MCP, and
    return a context block to prepend to the question.

    Non-fatal — returns empty string on any failure.
    """
    keys = _extract_jira_keys(question)
    if not keys:
        return ""

    from bifrost.extraction.cli_client import ClaudeLlmClient

    client = ClaudeLlmClient(settings)
    system_prompt = _jira_chain_system(settings)
    try:
        # Hard 90s ceiling: inherit_user_mcp=True can hang indefinitely when
        # called from a server-side context without an active user MCP session.
        # asyncio.wait_for gives a guaranteed timeout that the subprocess-level
        # timeout_override cannot provide.
        result = await asyncio.wait_for(
            client.call_with_json_schema(
                system_prompt=system_prompt,
                user_message=(
                    f"Fetch the full ticket chain for: {', '.join(sorted(keys))}\n\n"
                    "For each ticket fetch all linked tickets, subtasks, and parent epic "
                    "with full descriptions."
                ),
                model=settings.triage_model,
                json_schema=_JIRA_CHAIN_SCHEMA,
                max_turns=20,
                timeout_override=80,
                allowed_tools=settings.mcp.tools("atlassian", _JIRA_CHAIN_TOOL_NAMES),
                inherit_user_mcp=True,
            ),
            timeout=90.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("jira_chain_prefetch failed (non-fatal)")
        return ""

    tickets = (result.parsed or {}).get("tickets") or []
    if not tickets:
        return ""

    lines = ["## Jira ticket context (auto-fetched)\n"]
    for t in tickets:
        lines.append(f"### {t.get('key')} — {t.get('summary')}")
        lines.append(
            f"**Status:** {t.get('status', '?')}  "
            f"**Type:** {t.get('type', '?')}  "
            f"**Assignee:** {t.get('assignee', '?')}"
        )
        if t.get("labels"):
            lines.append(f"**Labels:** {', '.join(t['labels'])}")
        if t.get("description"):
            lines.append(f"\n{t['description'][:2000]}")
        if t.get("acceptance_criteria"):
            lines.append(f"\n**Acceptance criteria:**\n{t['acceptance_criteria'][:1000]}")
        linked = t.get("linked_tickets") or []
        if linked:
            lines.append("\n**Linked tickets:**")
            for lk in linked:
                lines.append(f"- {lk.get('key')} [{lk.get('link_type', '')}]: {lk.get('summary')}")
                if lk.get("description"):
                    lines.append(f"  {lk['description'][:400]}")
        lines.append("")
    return "\n".join(lines) + "\n---\n"


def _question_keywords(question: str) -> list[str]:
    """Tokenise a question into discriminating keywords. Lower-cased
    alpha-numeric tokens of length ≥ 3, stop-words dropped. Used by
    both the squad-name filter and the repo-name keyword prefilter."""
    import re

    return [
        t.lower()
        for t in re.findall(r"[A-Za-z0-9]+", question or "")
        if len(t) >= 3 and t.lower() not in _QUESTION_STOP_WORDS
    ]


async def _squad_match_filter(
    session: AsyncSession,
    candidates: list[Repo],
    question: str,
) -> tuple[list[Repo], list[str]]:
    """When question tokens match a squad name in ``RepoOwnership``,
    narrow ``candidates`` to that squad's repos.

    Returns ``(filtered_repos, matched_squad_names)``:
      * If a squad name (case-insensitive, normalised on
        whitespace / underscore / hyphen) matches one or more
        question tokens AND that squad owns at least one repo in
        ``candidates``, returns the narrowed slice plus the squad
        names found.
      * Otherwise returns the input unchanged + an empty squad
        list, so the keyword prefilter / router still runs.

    Why this exists: the keyword prefilter only looks at
    ``Repo.name`` + ``Repo.description``. A user asking "what does
    the ship team do?" carries ``ship`` as a token, but most
    ship-owned repos don't have "ship" in their name. Without this
    helper, the prefilter matches 0 and pads with random repos.
    Squad metadata is the right index for team-scoped questions.
    """
    import re

    tokens = _question_keywords(question)
    if not tokens or not candidates:
        return candidates, []

    from sqlalchemy import func as _func
    from sqlalchemy import or_
    from sqlalchemy import select as _sel

    candidate_ids = [r.id for r in candidates]
    norm_squad = _func.regexp_replace(
        _func.lower(RepoOwnership.squad),
        r"[\s_\-]+",
        "",
        "g",
    )
    likes = []
    for tok in tokens:
        norm_tok = re.sub(r"[\s_\-]+", "", tok.lower())
        if not norm_tok:
            continue
        likes.append(norm_squad.like(f"%{norm_tok}%"))
    if not likes:
        return candidates, []

    stmt = (
        _sel(RepoOwnership.repo_id, RepoOwnership.squad)
        .where(RepoOwnership.repo_id.in_(candidate_ids))
        .where(or_(*likes))
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return candidates, []

    matched_repo_ids: set[Any] = {r[0] for r in rows}
    matched_squads = sorted({r[1] for r in rows if r[1]})

    filtered = [r for r in candidates if r.id in matched_repo_ids]
    if not filtered:
        # Squad name matched but the squad has no candidates in scope —
        # fall through so the rest of the pipeline still runs.
        return candidates, []
    logger.info(
        "squad match: tokens=%s matched_squads=%s candidates %d → %d",
        tokens[:4],
        matched_squads[:4],
        len(candidates),
        len(filtered),
    )
    return filtered, matched_squads


async def _keyword_prefilter_repos(
    session: AsyncSession,
    question: str,
    candidates: list[Repo],
    *,
    max_count: int = _ROUTER_PREFILTER_THRESHOLD,
) -> tuple[list[Repo], int, str]:
    """Cut a large candidate pool down to ~max_count via SQL ILIKE on
    repo name + description before the LLM router runs.

    Returns ``(filtered_repos, prefilter_drop_count, reason)``. The
    drop count is informational; the reason tells the caller WHY a
    given output shape came back so it can route differently. Reasons:

      * ``"under_threshold"`` — input was already small, returned as-is.
      * ``"no_tokens"`` — only stop-words in the question; padded
        alphabetical-first as a last-ditch.
      * ``"no_keyword_match"`` — tokens existed but matched 0 repos.
        Returns ``([], len(candidates), reason)`` so the caller can
        emit a "no match — please rephrase" error and STOP rather
        than push 50 random repos through the rest of the pipeline.
      * ``"matched_capped"`` — matched ≥ max_count, returned the cap.
      * ``"matched_padded"`` — matched ≥ 1 but < max_count, padded with
        non-matched alphabetical to round out the router's view.

    Behaviour:
      * Tokenises ``question`` into alpha-numeric tokens of length ≥ 3,
        lower-cased, with the stop-word list dropped.
      * If at least one keyword survives: builds an OR-of-ILIKE over
        ``Repo.name`` and ``Repo.description`` and intersects with the
        candidate set. Primary repos sort first; alphabetical
        tie-break.

    The matched=0 short-circuit is the critical fix: prior versions
    fell through to alphabetical-first padding, which sent 50
    semantically-unrelated repos into the (slow, expensive) router +
    freshness pipeline. The user always saw a 4-minute hang followed
    by a useless answer. Returning empty + a clear error lets the
    user rephrase IMMEDIATELY.
    """
    if len(candidates) <= max_count:
        return candidates, 0, "under_threshold"

    tokens = _question_keywords(question)

    if not tokens:
        logger.warning(
            "router prefilter: no usable keywords in %r; falling back "
            "to alphabetical first %d (router will likely just return "
            "the alphabetical first 5 — narrow the scope or rephrase)",
            (question or "")[:80],
            max_count,
        )
        return (
            candidates[:max_count],
            len(candidates) - max_count,
            "no_tokens",
        )

    from sqlalchemy import func as _func
    from sqlalchemy import or_
    from sqlalchemy import select as _sel

    candidate_ids = [r.id for r in candidates]
    likes = []
    for tok in tokens:
        pat = f"%{tok}%"
        likes.append(Repo.name.ilike(pat))
        likes.append(_func.coalesce(Repo.description, "").ilike(pat))

    stmt = (
        _sel(Repo)
        .where(Repo.id.in_(candidate_ids))
        .where(or_(*likes))
        .order_by(Repo.is_primary.desc(), Repo.name)
        .limit(max_count)
    )
    matched = list((await session.execute(stmt)).scalars().all())

    if not matched:
        # CRITICAL: prior versions padded with alphabetical-first 50
        # here, which guaranteed a slow, useless query. Now we
        # short-circuit and let the caller emit a clear error.
        logger.warning(
            "router prefilter: 0 matches for tokens=%s among %d "
            "candidates — returning empty for caller to surface a "
            "rephrase prompt",
            tokens[:4],
            len(candidates),
        )
        return [], len(candidates), "no_keyword_match"

    if len(matched) >= max_count:
        return (
            matched[:max_count],
            len(candidates) - max_count,
            "matched_capped",
        )

    # Pad with non-matched candidates so the router still sees a
    # representative slice if keywords were too narrow. This path is
    # ONLY taken when matched ≥ 1 — a partial-match signal is real
    # signal, so padding to fill the budget is fine.
    matched_ids = {r.id for r in matched}
    pad_pool = [r for r in candidates if r.id not in matched_ids]
    pad_n = max_count - len(matched)
    out = matched + pad_pool[:pad_n]
    logger.info(
        "router prefilter: %d candidates → %d (matched=%d, padded=%d) tokens=%s",
        len(candidates),
        len(out),
        len(matched),
        min(pad_n, len(pad_pool)),
        tokens[:6],
    )
    return out, len(candidates) - len(out), "matched_padded"


async def _resolve_candidates(
    session: AsyncSession,
    conv: Conversation,
    explicit_scope_repo_ids: list[str] | None,
) -> list[Repo]:
    """Turn the conversation's scope into a list of Repo ORM rows.

    Precedence: explicit_scope_repo_ids > conv.scope_repo_ids > conv.scope_domain_id
    > all repos. When falling back to "all repos", filter to
    **code-indexed** repos only (those with at least one extracted
    component) — the manifest-only service entities still show up
    in the master subgraph for synthesis citation, but we don't try to
    clone + fan out an agent on a repo that has no extracted code.
    """
    from sqlalchemy import select

    # 1. explicit per-turn scope
    rids: list[str] = list(explicit_scope_repo_ids or [])

    # 2. conversation-level scope_repo_ids
    if not rids and conv.scope_repo_ids:
        rids = list(conv.scope_repo_ids)

    if rids:
        stmt = select(Repo).where(Repo.id.in_([_uuid.UUID(r) for r in rids]))
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    # 3. domain scope — no code-index filter here because picking a
    # domain is already a narrow commitment; the user wants that
    # domain's repos regardless of extraction depth.
    if conv.scope_domain_id:
        stmt = select(Repo).where(Repo.domain_id == conv.scope_domain_id)
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    # 4. All repos — include every manifest-managed repo in scope. The
    # router picks top-K by name/description relevance; once picked,
    # the coordinator classifies them as deep-indexed (Phase A-I
    # complete) vs manifest-only (service entity + manifest metadata
    # only). Manifest-only picks trigger auto-extraction in the
    # background so follow-up queries get depth, while the current
    # query still answers via community summaries + service graph.
    stmt = select(Repo).order_by(Repo.name)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _catalog_candidates_for_domain(
    session: AsyncSession,
    domain: Domain | None,
    registered_names: set[str],
) -> list[OrgRepo]:
    """Find catalog (org-wide) repos that match a domain's squad but
    aren't yet registered. Enables chat to surface repos we haven't
    formally indexed.

    Squad-name match is normalized so "ship-hub", "ship_hub", "SHIP HUB",
    "ShipHub", and "shiphub" all match the same domain. Without this,
    casing/punctuation drift between the ownership manifest and the
    domain row silently misses the team's repos.
    """
    from sqlalchemy import func, select

    if domain is None or not domain.name:
        return []
    want = domain.name.strip()
    if not want:
        return []

    # Both sides normalized: lowercased + spaces / hyphens / underscores
    # stripped. Mirrors the squad-name normalization in manifest writers.
    norm_want = func.regexp_replace(func.lower(want), r"[\s_\-]+", "", "g")
    norm_squad = func.regexp_replace(
        func.lower(OrgRepo.squad),
        r"[\s_\-]+",
        "",
        "g",
    )
    stmt = (
        select(OrgRepo)
        .where(norm_squad == norm_want)
        .where(OrgRepo.archived.is_(False))
        .where(OrgRepo.is_fork.is_(False))
    )
    rows = (await session.execute(stmt)).scalars().all()
    # Filter out repos that are already registered
    return [r for r in rows if r.name not in registered_names]


async def _auto_register_from_catalog(
    session_factory: async_sessionmaker[AsyncSession],
    catalog_row: OrgRepo,
    domain_id: _uuid.UUID | None,
) -> Repo:
    """Create a Repo row from a catalog entry so we can research it.

    Does NOT clone or extract — those are the caller's responsibility.
    Idempotent: returns the existing row if one already matches the URL.
    """
    from sqlalchemy import select

    async with session_factory() as session:
        existing = (
            await session.execute(
                select(Repo).where(Repo.url == catalog_row.url),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        repo = Repo(
            id=_uuid.uuid4(),
            urn=f"urn:bifrost:repo:{catalog_row.name}",
            name=catalog_row.name,
            url=catalog_row.url,
            domain_id=domain_id,
            description=(catalog_row.description or "")[:500] or None,
            is_primary=bool(getattr(catalog_row, "is_primary", False)),
            adapter_config={
                "auto_registered_from_catalog": True,
                "squad": catalog_row.squad,
            },
        )
        session.add(repo)
        await session.flush()
        # Backlink the catalog row → registered_repo_id
        catalog_row.registered_repo_id = repo.id
        session.add(catalog_row)
        await session.commit()
    return repo


async def _load_freshness_rows(
    session: AsyncSession,
    repo_ids: list[_uuid.UUID],
) -> dict[_uuid.UUID, FreshnessRow]:
    """Load repo_freshness rows keyed by repo_id for bulk last-sha lookup."""
    from sqlalchemy import select

    if not repo_ids:
        return {}
    stmt = select(FreshnessRow).where(FreshnessRow.repo_id.in_(repo_ids))
    rows = (await session.execute(stmt)).scalars().all()
    return {r.repo_id: r for r in rows}


async def _load_community_labels_for_repo(
    session: AsyncSession,
    repo_id: str,
    snippets: list[EntitySnippet],
) -> dict[str, str]:
    """Build the URN → community label map for sharding.

    Returns ``{}`` when this repo has no Louvain communities yet
    (cold-start, or community-detection didn't run). The caller falls
    through to entity-count-balanced sharding in that case.

    Joins ``entities.community_id`` to ``entity_communities.name``
    keyed by repo, then maps back through the snippet list by URN.
    """
    from sqlalchemy import select as _sel

    if not snippets:
        return {}
    snippet_ids = [_uuid.UUID(e.id) for e in snippets if e.id]
    if not snippet_ids:
        return {}
    # Single JOIN: entity → community on the same repo.
    stmt = (
        _sel(Entity.id, Entity.urn, EntityCommunity.name)
        .join(
            EntityCommunity,
            Entity.community_id == EntityCommunity.id,
        )
        .where(Entity.id.in_(snippet_ids))
    )
    rows = (await session.execute(stmt)).all()
    return {urn: name for (_, urn, name) in rows if urn and name}


async def _fetch_entity_snippets_for_repo(
    session: AsyncSession,
    repo_id: _uuid.UUID,
    *,
    question: str | None = None,
    limit: int = ENTITIES_PER_SUBAGENT,
    question_keywords: list[str] | None = None,
) -> list[EntitySnippet]:
    """Pull the query subgraph rooted in this repo.

    Delegates to ``graph_retrieval.build_query_subgraph`` — vectors seed
    the walk, then bounded BFS over the pre-extracted relationship graph
    yields a multi-hop subgraph. Converts to ``EntitySnippet`` for
    back-compat with the repo sub-agent.

    Falls back to raw vector/keyword/recency retrieval if the graph
    walk returns nothing (e.g., an un-seeded repo with entities but no
    relationships yet). This keeps the function behavioural-compat with
    pre-graph callers.
    """
    from sqlalchemy import desc, func, or_, select

    from bifrost.research.graph_retrieval import build_query_subgraph

    # ── 1. Graph-first path: seeds (vector) → BFS ───────────────
    if question:
        try:
            subgraph = await build_query_subgraph(
                session,
                question,
                repo_ids=[str(repo_id)],
                depth=2,
                max_nodes=limit,
                seeds=min(8, limit // 3 or 4),
            )
            if subgraph.nodes:
                logger.info(
                    "graph retrieval for repo %s: %d nodes, %d edges (%d cross-repo), depth=%d",
                    repo_id,
                    subgraph.stats.get("total_nodes", 0),
                    subgraph.stats.get("total_edges", 0),
                    subgraph.stats.get("cross_repo_edges", 0),
                    subgraph.stats.get("max_depth_reached", 0),
                )
                return subgraph.to_snippets()
        except Exception:
            logger.exception("graph retrieval failed; falling back to flat scan")

    # ── 2. Keyword fallback (repo has no embeddings / no graph) ──
    stmt = select(Entity).where(Entity.scope_repo_id == repo_id)
    if question_keywords:
        clauses = []
        for kw in question_keywords:
            if len(kw) < 3:
                continue
            clauses.append(func.lower(Entity.name).like(f"%{kw.lower()}%"))
        if clauses:
            narrowed = stmt.where(or_(*clauses)).order_by(desc(Entity.updated_at)).limit(limit)
            rows = (await session.execute(narrowed)).scalars().all()
            if rows:
                snips = _rows_to_snippets(rows)
                return await _attach_graph_edges(session, snips)

    # ── 3. Recency fallback ─────────────────────────────────────
    stmt = stmt.order_by(desc(Entity.updated_at)).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    snips = _rows_to_snippets(rows)
    return await _attach_graph_edges(session, snips)


def _rows_to_snippets(rows) -> list[EntitySnippet]:  # type: ignore[no-untyped-def]
    """Map ORM entity rows to snippets — without graph edges.

    Callers that want relationships should use ``_attach_graph_edges``
    immediately after. Split so the semantic-retrieval path stays cheap
    when the caller only needs raw entities (e.g., tests).
    """
    snippets: list[EntitySnippet] = []
    for e in rows:
        meta = e.entity_metadata or {}
        snippets.append(
            EntitySnippet(
                id=str(e.id),
                urn=e.urn,
                name=e.name,
                entity_type=e.entity_type,
                purpose=str(meta.get("purpose", "") or "")[:300],
                source_file=meta.get("source_file"),
                metadata=dict(meta),
            )
        )
    return snippets


async def _attach_graph_edges(
    session: AsyncSession,
    snippets: list[EntitySnippet],
) -> list[EntitySnippet]:
    """Hydrate 1-hop outbound+inbound edges onto each snippet.

    One query: pull every EntityRelationship where source_id or target_id
    is in our retrieved set. Build a urn+name index so edges can be
    resolved even when the partner is OUTSIDE the retrieved set (we still
    cite it by URN — the frontend resolves the detail via /entities/{urn}).
    """
    if not snippets:
        return snippets
    from sqlalchemy import or_, select

    ids = [_uuid.UUID(s.id) for s in snippets if s.id]
    if not ids:
        return snippets

    # Pull relationships touching any of our entities
    rel_stmt = select(EntityRelationship).where(
        or_(
            EntityRelationship.source_id.in_(ids),
            EntityRelationship.target_id.in_(ids),
        )
    )
    rels = (await session.execute(rel_stmt)).scalars().all()
    if not rels:
        return snippets

    # Resolve partner entity names/URNs for whatever end we don't already
    # have in the snippets set. Cap at 400 edges to keep the prompt bounded.
    snippet_by_id: dict[str, EntitySnippet] = {s.id: s for s in snippets}
    partner_ids: set[_uuid.UUID] = set()
    for r in rels[:400]:
        if str(r.source_id) not in snippet_by_id:
            partner_ids.add(r.source_id)
        if str(r.target_id) not in snippet_by_id:
            partner_ids.add(r.target_id)

    partner_meta: dict[str, tuple[str, str, str]] = {}  # id -> (name, urn, type)
    if partner_ids:
        partner_stmt = select(Entity.id, Entity.name, Entity.urn, Entity.entity_type).where(
            Entity.id.in_(partner_ids)
        )
        for pid, pname, purn, ptype in (await session.execute(partner_stmt)).all():
            partner_meta[str(pid)] = (pname, purn, ptype)

    def _pack_partner(eid: str) -> tuple[str, str, str] | None:
        if eid in snippet_by_id:
            s = snippet_by_id[eid]
            return (s.name, s.urn, s.entity_type)
        return partner_meta.get(eid)

    for r in rels[:400]:
        src_id = str(r.source_id)
        tgt_id = str(r.target_id)
        # Outbound edge on the source snippet
        src = snippet_by_id.get(src_id)
        if src is not None:
            tgt = _pack_partner(tgt_id)
            if tgt:
                src.outbound.append(
                    GraphEdge(
                        rel_type=r.rel_type,
                        other_name=tgt[0],
                        other_urn=tgt[1],
                        other_type=tgt[2],
                    )
                )
        # Inbound edge on the target snippet
        tgt_snip = snippet_by_id.get(tgt_id)
        if tgt_snip is not None:
            srcp = _pack_partner(src_id)
            if srcp:
                tgt_snip.inbound.append(
                    GraphEdge(
                        rel_type=r.rel_type,
                        other_name=srcp[0],
                        other_urn=srcp[1],
                        other_type=srcp[2],
                    )
                )
    return snippets


def _extract_keywords(question: str) -> list[str]:
    """Cheap keyword extraction for the entity retrieval step.

    Splits on non-alpha, drops common stopwords + overly short tokens.
    Not trying to be smart — just avoiding zero-signal retrieval.
    """
    import re

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]+", question)
    stop = {
        "the",
        "and",
        "for",
        "with",
        "how",
        "what",
        "does",
        "this",
        "that",
        "are",
        "from",
        "into",
        "when",
        "where",
        "which",
        "why",
        "use",
        "used",
        "show",
        "tell",
        "can",
        "should",
        "will",
        "have",
        "has",
        "was",
        "were",
        "you",
        "your",
        "our",
        "its",
        "his",
        "her",
        "them",
        "their",
        "they",
        "there",
        "these",
        "those",
        "some",
        "any",
        "all",
        "one",
        "two",
    }
    return [t for t in tokens if len(t) > 2 and t.lower() not in stop]


# ── Synthesis prompt ───────────────────────────────────────────────────


_SYNTHESIS_SYSTEM_PROMPT = """\
You are a senior staff engineer writing the definitive answer to a
co-worker's engineering question by synthesising evidence from multiple
specialized research agents + a graph of pre-extracted entities.

Your input may contain UP TO THREE TOP-LEVEL sections BEFORE the
per-repo details:
  - **Architectural context — relevant communities**: GraphRAG-style
    Louvain-cluster summaries of the codebase. Use these to write a
    framing TL;DR and to understand the big picture.
  - **Pre-computed flow paths**: DFS chains through the query
    subgraph. The FIRST path is the mandatory skeleton of your
    ### End-to-end flow section (one numbered step per node).
  - **Per-repo research**: individual repo reports + audits + evidence.

Every repo's section contains UP TO THREE blocks:
  (A) A per-agent summary (code, Jira, Confluence, GitHub, Slack).
  (B) A **Code audit** block (only when the code agent ran in audit mode):
      edges it literally verified on disk, contradictions it found, and
      new edges it discovered not in the graph. This is the HIGHEST
      trust source — prefer it over (A) whenever they overlap. Use
      contradictions to inform the Caveats section.
  (C) An **Evidence block**: a table of extracted entities with URNs and
      a relationship list. Use this as the source of truth for
      citations — the summaries are reconnaissance; the evidence is ground.

## HARD RULES

1. **CODE IS THE GROUND TRUTH.** Per-repo code agents and the entity
   graph (extracted directly from the working production source)
   are tier T1 / T2 — the highest-trust signal. Confluence, Jira,
   GitHub, and Slack are tier T4 — *supporting context* that
   describes what the code does, but is often stale or aspirational.
   Build the answer FROM CODE BACKWARDS: what the source actually
   shows is the spine; external docs are corroboration or
   contradiction. NEVER lead with a Confluence claim that the
   code agent / evidence block doesn't back. If the code agent had
   no signal for a relevant repo, say so plainly — don't paper over
   the gap with doc-derived speculation.

   **Hedge by tier.** The Evidence block splits relationships into
   three sections: High-confidence (T1/T2), Medium-confidence (T3),
   and Low-confidence (T4/T5). Claims supported ONLY by edges in
   the Medium-confidence section MUST use hedging language
   ("appears to", "likely"). Claims supported ONLY by Low-confidence
   edges MUST be marked "(documented but not code-verified)". When
   tiers disagree on the same fact, the higher tier wins — see the
   "Cross-source alignment" rule. Claims that span multiple tier
   buckets should cite the highest tier that supports them.

2. **Every non-trivial factual claim MUST have a GFM footnote citation.**
   Write claims like "OrderRouter routes by tenant plan [^1]" and define
   [^1] in the final Sources section with a URN or URL.

   **EVIDENCE-CONSISTENCY GUARD.** Your headline confidence (the field
   the coordinator emits as ``confidence``) MUST track the WEAKEST
   evidence cited in the answer body, not the strongest. If your
   "Source coverage" or "Caveats" sections will mention any of:

     - "search failed" / "unavailable" / "timed out"
     - "not routed" / "not in scope" / "not walked"
     - "exact caller unknown" / "couldn't determine"
     - "documented but not code-verified" / "treat as speculative"
     - "first commit/PR not actually located"

   …then your headline confidence MUST be **inferred** at most — never
   **confirmed**. Confirmed is reserved for answers where every load-
   bearing claim has T1/T2 (code/git) corroboration AND no source the
   answer touched returned an error or a "not routed" signal. When in
   doubt, drop a tier. A tech lead reading this answer must be able to
   trust that "Confirmed" means "I walked the code and verified" —
   never "I read a Confluence page that said this is how it works."

   **BOUNDARY-PRESERVATION GUARD.** When the question crosses service
   boundaries (e.g., "how does A call B?", "what's the handoff between
   X and Y?", "trace the flow from A through B to C"), name the actual
   caller/callee at each hop. If you don't know who the upstream caller
   is — say so explicitly with the phrase **"exact caller unknown"**
   instead of describing the handoff as if you knew. NEVER blur which
   service does what role: identity creation (e.g. CustomerRegistry) is not the
   same as policy decision (e.g. PolicyEngine), order
   orchestration (e.g. OrderOrchestrator) is not the same as stock-state mutation
   (e.g. InventoryService), and downstream sinks (e.g.
   AuditLedger, AnalyticsSink) are not the same as the upstream callers that
   write into them. If your evidence covers only one side of a
   boundary, describe THAT side and explicitly mark the other side as
   "out of scope this turn" — don't synthesise both sides from
   incomplete signal. The reader is making decisions based on these
   names; conflating them is a worse failure than admitting "I don't
   know who calls this."

3. **Prefer URNs from the Evidence block** over vague repo-name citations.
   Bad: "Carrier-gateway handles routing."
   Good: "`OrderRouter` [^1] routes requests by tenant plan, invoking
   `FulfillmentExecutor` [^2]." where [^1] is the component's URN.

4. **Follow this EXACT structure** (use these headings verbatim):

   ### TL;DR
   A 2-4 sentence direct answer. No lists, no preamble.

   ### How it works
   Step-by-step explanation with footnote citations on key claims. Walk
   the graph — if code agent shows `A --calls--> B`, describe that edge.

   ### Key components
   A markdown table with columns: **Component** | **Repo** | **Role** |
   **Source**. One row per high-signal entity. Link the component name
   to its URN.

   ### End-to-end flow
   Numbered steps (1., 2., 3., …) that trace the request/event through
   components. Each step cites the involved URN(s). **If a
   "Pre-computed flow paths" section was provided in the input, the
   first path is the authoritative skeleton — one numbered step per
   node in that path, narrating the edge leading into each step.**
   Additional paths from the input may be referenced as "alternatively"
   branches.

   **EMIT A MERMAID DIAGRAM** immediately before the numbered prose
   whenever the answer spans 3+ services OR describes a non-trivial
   decision tree / state transition / concurrent branching flow.
   The CANONICAL DIAGRAM STANDARDS block appended below specifies
   exactly which diagram type to use (HLD vs LLD), required header
   directive, styling classes, edge labeling rules, and examples —
   follow it verbatim. If an answer has both system-level and
   implementation-level concerns, emit TWO diagrams: HLD first
   (orientation), then LLD (mechanism).

   For single-service questions or trivially simple flows, skip the
   diagram — do not force one.

   ### Cross-source alignment
   Did code, Jira, Confluence, GitHub, and Slack tell a consistent story?
   Where they agree, say so once. Where they disagree or one source is
   silent, call it out explicitly. **CODE IS GROUND TRUTH** — when an
   external doc disagrees with what the code agent verified on disk,
   the code agent wins and the doc is flagged outdated. Never the
   other way around. Skip this section only if you had just one
   source type with signal — don't invent disagreement.

   ### Source coverage
   A short list — one bullet per external source — saying exactly
   what each contributed AND which ones FAILED. The "Source coverage
   (audit trail)" table near the top of your input has the per-source
   counts; **you MUST surface them** so the user knows what was
   actually consulted versus silently absent. Required format:

     - **Code (per-repo agents):** contributed for N repos, failed
       for M repos[, skipped for K repos]. (If any failed, name the
       repos.)
     - **Confluence:** contributed N pages from M repos, failed for
       K repos with reason "<error>". If failed: explicitly say
       "Confluence was UNAVAILABLE this turn — answer falls back
       to code/Jira/etc."
     - **Jira:** same shape.
     - **GitHub:** same shape.
     - **Slack:** same shape.
     - **Org-wide external:** OK / FAILED with reason.

   If a source FAILED, the user MUST be told in plain language —
   never let a failed lookup masquerade as "we checked, found
   nothing". Reproduce the verbatim failure message from the
   "Source failures" list in your input.

   ### Caveats & staleness
   Bullets. Things the reader should NOT over-extrapolate:
     - Staleness: which repos had `metadata_stale=true` and what that means.
     - Scope limits: what the evidence covers vs. what it doesn't.
     - Known gaps surfaced by agents.
   If none apply, write: "No significant caveats."

   ### Gaps & unknowns
   Bullets of questions the user might reasonably ask next that THIS
   evidence cannot answer. Be specific ("the carrier adapter is referenced
   but not in scope" > "we don't know everything").
   If implementing a UI feature, the first gap bullet should address reachability:
   does the change actually reach the users or configurations in scope (routing, feature flags, config)?

   ### Sources
   GFM footnote definitions — one per citation used above. Format:
     [^1]: `urn:bifrost:component:carrier-gateway::OrderRouter` — routes
     carrier requests by tenant plan. `src/router/OrderRouter.kt`.
     [^2]: [SHIP-1234](https://…) — bug: retry loop on auth failure.

## VERIFICATION DISCIPLINE

When a design document (Confluence tech design, Jira description) makes a specific code claim
(a file path, prop name, selector name, API field, method signature), you MUST cross-reference
it against T1/T2 evidence in the code agents or entity graph. If the code evidence contradicts
the design doc:
- State the contradiction explicitly: "The tech design says X but the code agent found Y"
- Mark the design claim as **[DESIGN — not code-verified this run]**
- This is load-bearing: engineers copy-paste from design docs; silent errors ship as bugs.

## ROUTING COMPLETENESS

When the question asks about adding something to a specific screen or component:
1. State which routing decision tree determines whether users reach that component
2. Name any plan-enabled checks, experiment gates, or flow routing that could bypass it
3. If the routing is API/config-driven and cannot be determined from code, say:
   "**First check before any code**: confirm which requests or configurations actually reach
    [component]. If routing bypasses it, the change has no effect."

## STRATEGIC FIT

For feature-build questions, add a ## Devil's advocate section that:
- Checks if the work described is already being done (blocked ticket, in-flight PR)
- States the critical path honestly: is the blocker in the code, or a dependency outside it (a decision, a review, another team)?
- Names the upside realistically, including any reviewer pushback seen in Jira comments
- States whether you should WAIT FOR something vs start building now

## STYLE

- Markdown, streaming-friendly (the UI renders as it arrives).
- Mono-font identifiers for class/function/file names.
- Do NOT paraphrase away evidence. If code shows a specific flow, name it.
- Do NOT use headings other than the ones above.

### DEPTH

The reader is a senior engineer making decisions. They need substance,
not summary. Specifically:

- Explain the **WHY** behind each claim — not just what the code does,
  but why it's structured that way and what trade-off it implies. If a
  service routes by tenant plan, say which tenant plans, why tenant plan was
  chosen as the routing axis, and what happens when a new one is
  added.
- Name **concrete artifacts**: file paths, class names, function
  signatures, Jira keys, Confluence titles, Slack channels, PR
  numbers. Vague phrases like "the routing service" are a smell — say
  `OrderRouter` (`src/router/OrderRouter.kt:42-87`) instead.
- For each major section, connect at least one sentence back to the
  user's question explicitly. The reader should never have to ask
  "why is this in the answer?". If the question was about adding a
  new tenant plan, every paragraph in How it works / End-to-end
  flow should be framed in those terms.
- Don't truncate at "TL;DR-level depth" in the body. The TL;DR is the
  4-sentence preview; **How it works** and **End-to-end flow** are
  where the real exposition lives. Aim for 3-6 substantive paragraphs
  per section when the evidence supports it.

### PARAGRAPH STRUCTURE

- Use full paragraphs of 3-6 sentences for explanatory content.
  Bullets are for **enumerable** facts (lists of components, alerts,
  steps); prose is for explanations.
- AVOID the "**Bold lead phrase.** body sentence." pattern as a
  paragraph opener. Use it sparingly — at most once per section. When
  you need a structural anchor inside a paragraph, prefer an inline
  italic or `code` reference, or split into a sub-list.
- Bold (`**`) is for genuinely critical emphasis (a hard guarantee, a
  warning, a contradiction). It is NOT decoration. Two or three bold
  phrases per section is plenty; eight is too many. The reader should
  be able to scan only the bold tokens and learn the headline facts —
  if every other phrase is bold, that scan signal is gone.
- Italics (`*`) for nuance, hedging, or term-of-art definitions
  ("*staleness* here means…"). Reserved.
"""

# Append canonical policy blocks — same rulesets that any future
# prompt generating graph-grounded claims or diagrams should import.
# Keeps grammar + tier language + visual style consistent across
# the product.
from bifrost.research.confidence_policy import CODE_IS_TRUTH_POLICY  # noqa: E402
from bifrost.research.diagram_standards import DIAGRAM_RULES_BLOCK  # noqa: E402

_SYNTHESIS_SYSTEM_PROMPT = (
    _SYNTHESIS_SYSTEM_PROMPT
    + "\n\n"
    + CODE_IS_TRUTH_POLICY
    + "\n\n"
    + DIAGRAM_RULES_BLOCK
    + "\n\n"
    + (
        "Continuity rule: if the user message contains a "
        "<prior_research> block, treat its contents as background from "
        "the previous turn — useful for referring to URNs and sources "
        "the user has already seen, but NOT authoritative coverage of "
        "the current question. The current question may need fresh "
        "evidence from the per-repo blocks below the <prior_research>; "
        "lean on those for any new claims. Do not paraphrase the prior "
        "answer — build on it."
    )
)


# ── Main coordinator ───────────────────────────────────────────────────


@dataclass
class ResearchDeps:
    """Everything the coordinator needs injected from the outer layer."""

    session_factory: async_sessionmaker[AsyncSession]
    settings: BifrostSettings


def _format_prior_turns(prior: list, *, max_turns: int = 4) -> str:
    """Compact textual representation of the last N turns.

    Limited to preserve context without blowing budget. Each turn trimmed
    to 400 chars so a single long answer doesn't dominate.
    """
    if not prior:
        return ""
    # Last N user+assistant pairs
    recent = list(prior)[-max_turns * 2 :]
    lines = []
    for m in recent:
        speaker = "User" if getattr(m, "role", None) == "user" else "Assistant"
        content = getattr(m, "content", "") or ""
        lines.append(f"{speaker}: {content[:400]}")
    return "\n".join(lines)


def _format_prior_evidence(
    prior_bundle: ResearchBundle | None,
    *,
    kind_filter: str | None = None,
    max_per_kind: int = 25,
) -> str:
    """Render the structured evidence (URNs, Jira keys, Confluence pages,
    GitHub PRs, Slack threads) gathered in PRIOR turns of this conversation
    as a "do-not-re-search-this" hint for the per-source agents.

    Without this block, every follow-up turn's per-source agent re-runs
    the same MCP search and re-finds the same Jira tickets / Confluence
    pages / Slack threads, wasting budget and producing answers that
    redundantly cite what the prior turn already cited. With this
    block, the agent treats the listed items as known and pivots to
    finding NEW evidence beyond them.

    ``kind_filter`` lets a per-source agent receive ONLY the
    already-found evidence for its own kind (avoids polluting a Jira
    agent's prompt with Confluence URNs). Pass None to include all
    kinds (used by repo agents that synthesise across sources).

    ``max_per_kind`` caps the bullet count per kind so a 200-source
    prior bundle doesn't blow the agent's prompt budget.
    """
    if prior_bundle is None:
        return ""
    sources = prior_bundle.sources or []
    subgraph = list(prior_bundle.subgraph_entity_urns or [])
    if not sources and not subgraph:
        return ""

    by_kind: dict[str, list[Any]] = {}
    for s in sources:
        kind = getattr(s, "kind", None) or (s.get("kind") if isinstance(s, dict) else None)
        if kind is None:
            continue
        if kind_filter is not None and kind != kind_filter and kind != "code":
            # Always pass code URNs through; per-source agents benefit
            # from knowing which components prior turns already cited.
            continue
        by_kind.setdefault(kind, []).append(s)

    if not by_kind and not subgraph:
        return ""

    out = ["## Evidence already found in prior turns of this conversation"]
    out.append("")
    out.append(
        "DO NOT re-search or re-fetch the items below — they were "
        "cited in earlier turns and the user already has them. Use "
        "them as known context, then find NEW evidence: more recent "
        "items, deeper sub-issues, items at a different angle, or "
        "items that extend/contradict what's listed. If your search "
        'would only re-surface these, return relevance: "none" and '
        "explain that prior coverage is sufficient."
    )

    def _label(s: Any) -> str:
        if isinstance(s, dict):
            ref = s.get("ref") or s.get("urn") or s.get("id") or ""
            title = (s.get("title") or s.get("name") or "")[:80]
        else:
            ref = getattr(s, "ref", None) or getattr(s, "urn", None) or getattr(s, "id", None) or ""
            title = (getattr(s, "title", None) or getattr(s, "name", None) or "")[:80]
        if ref and title:
            return f"`{ref}` — {title}"
        return ref or title

    if (kind_filter is None or kind_filter == "code") and subgraph:
        out.append("")
        out.append("### Code components / URNs already in scope:")
        for urn in subgraph[:max_per_kind]:
            out.append(f"- `{urn}`")

    for kind in ("jira", "confluence", "github", "slack", "external"):
        if kind_filter is not None and kind != kind_filter:
            continue
        bucket = by_kind.get(kind) or []
        if not bucket:
            continue
        out.append("")
        label = {
            "jira": "Jira tickets",
            "confluence": "Confluence pages",
            "github": "GitHub PRs / issues",
            "slack": "Slack threads",
            "external": "External / org-wide hits",
        }[kind]
        out.append(f"### {label} already cited:")
        for s in bucket[:max_per_kind]:
            out.append(f"- {_label(s)}")

    return "\n".join(out)


async def _wait_and_integrate_extractions(
    *,
    worker: Any,
    queued_repos: list[tuple[str, str, Any]],
    wait_s: float,
    session_factory: async_sessionmaker[AsyncSession],
    promoted_out: list[Any],
    promoted_ids_out: set[str],
) -> AsyncIterator[dict[str, Any]]:
    """Block on a set of in-flight auto-extractions for up to ``wait_s``
    seconds, yielding an SSE-shaped dict per task as it resolves.

    On entry: ``queued_repos`` holds ``(task_id, repo_id, repo)`` tuples
    for each manifest-only repo whose extraction has been submitted. The
    worker's ``wait_for_task`` coroutine is awaited in parallel; the
    helper yields one event per task, then a ``extraction_timeout``
    event for any task that didn't finish in the budget.

    For tasks that finish in ``"success"``, the helper additionally
    runs a quick entity-count query against the same DB; only repos
    that actually produced entities are appended to ``promoted_out``
    (so synthesis doesn't try to walk a code-empty repo).

    Extracted out of ``run_research`` so the wait/integrate logic can
    be unit tested without spinning up the full coordinator.
    """
    pending: dict[asyncio.Task[Any], tuple[str, str, Any]] = {
        asyncio.create_task(worker.wait_for_task(t_id, timeout=wait_s)): (t_id, r_id, repo)
        for t_id, r_id, repo in queued_repos
    }
    loop_deadline = asyncio.get_event_loop().time() + wait_s

    while pending:
        remaining = loop_deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        done, _still_pending = await asyncio.wait(
            pending.keys(),
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            break
        for fut in done:
            t_id, r_id, repo = pending.pop(fut)
            try:
                final_state = fut.result()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "auto-extract wait failed for %s",
                    repo.name,
                )
                final_state = None

            if final_state == "success":
                count = await _count_code_entities(session_factory, repo.id)
                if count > 0:
                    promoted_out.append(repo)
                    promoted_ids_out.add(str(repo.id))
                    yield {
                        "type": "extraction_completed_inline",
                        "repo_id": r_id,
                        "repo_name": repo.name,
                        "task_id": t_id,
                        "entities": count,
                        "message": (
                            f"{repo.name}: deep extraction finished "
                            f"({count} entities) — including in this "
                            "answer."
                        ),
                    }
                else:
                    yield {
                        "type": "extraction_no_entities",
                        "repo_id": r_id,
                        "repo_name": repo.name,
                        "task_id": t_id,
                        "message": (
                            f"{repo.name}: extraction finished but "
                            "produced no entities; falling back to "
                            "manifest metadata for this answer."
                        ),
                    }
            elif final_state in {"failed", "cancelled"}:
                yield {
                    "type": "extraction_failed_inline",
                    "repo_id": r_id,
                    "repo_name": repo.name,
                    "task_id": t_id,
                    "state": final_state,
                    "message": (
                        f"{repo.name}: deep extraction "
                        f"{final_state}; falling back to manifest "
                        "metadata for this answer."
                    ),
                }
            else:
                yield {
                    "type": "extraction_failed_inline",
                    "repo_id": r_id,
                    "repo_name": repo.name,
                    "task_id": t_id,
                    "state": "unknown",
                    "message": (
                        f"{repo.name}: extraction state unknown; falling back to manifest metadata."
                    ),
                }

    for fut, (t_id, r_id, repo) in pending.items():
        fut.cancel()
        yield {
            "type": "extraction_timeout",
            "repo_id": r_id,
            "repo_name": repo.name,
            "task_id": t_id,
            "wait_seconds": wait_s,
            "message": (
                f"{repo.name}: extraction still running after "
                f"{int(wait_s)}s — answering from manifest metadata "
                "now; follow-ups will use the deep result."
            ),
        }


async def _load_prior_bundle(
    repo: EntityRepository,
    conversation_id: _uuid.UUID,
) -> ResearchBundle | None:
    """Best-effort load of the prior research bundle for a conversation.

    Tries the in-memory cache first (fast hit on the same process),
    falls back to the conversation_research_state row written by the
    coordinator at the end of the previous turn.
    """
    cache = get_bundle_cache()
    cached = await cache.load(str(conversation_id))
    if cached is not None:
        return cached
    state = await repo.load_research_state(conversation_id)
    if state is None:
        return None
    try:
        bundle = ResearchBundle.from_dict(state.bundle)
    except Exception:  # noqa: BLE001
        logger.exception(
            "research bundle in DB row %s was unparseable",
            conversation_id,
        )
        return None
    if state.last_question_embedding and not bundle.last_question_embedding:
        bundle.last_question_embedding = list(state.last_question_embedding)
    await cache.save(bundle)
    return bundle


async def _embed_question_async(question: str) -> list[float] | None:
    """Compute the MiniLM embedding for a chat question without
    blocking the event loop. Returns None if the model isn't available
    (e.g. during unit tests with no sentence-transformers install)."""
    try:
        from bifrost.research.embeddings import embed_one

        return await asyncio.to_thread(embed_one, question)
    except Exception:  # noqa: BLE001
        logger.exception("failed to embed chat question; planner will fall back")
        return None


def _bundle_sources_from_payload(sources_payload: list[dict[str, Any]]) -> list[BundleSource]:
    """Project the synthesis-level sources list onto BundleSource entries.

    Drops fields that aren't useful for follow-up classification (the
    bundle stays metadata-only).
    """
    out: list[BundleSource] = []
    for s in sources_payload:
        out.append(
            BundleSource(
                kind=str(s.get("entity_type", "repo")),
                ref=str(s.get("urn", "")),
                title=str(s.get("name", ""))[:200],
            )
        )
    return out


async def _persist_research_bundle(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: _uuid.UUID,
    bundle: ResearchBundle,
) -> None:
    """Save the bundle to both the in-memory cache and the DB row.

    Failures are logged but not raised — losing a bundle write is
    less bad than failing the chat answer the user is reading.

    Order is intentional (bundle-persistence-race fix): DB is committed
    first; the in-memory cache is only populated on DB success so a
    process crash between the two operations never leaves the cache
    holding a bundle the DB does not have.
    """
    # 1. Persist to DB first.
    db_ok = False
    try:
        async with session_factory() as session:
            repo = EntityRepository(session)
            await repo.save_research_state(
                conversation_id=conversation_id,
                bundle=bundle.to_dict(),
                last_question_embedding=bundle.last_question_embedding,
            )
            await session.commit()
        db_ok = True
    except Exception:  # noqa: BLE001
        logger.exception(
            "conversation_research_state persist failed for %s",
            conversation_id,
        )

    # 2. Only warm the cache after a confirmed DB commit.
    if db_ok:
        cache = get_bundle_cache()
        try:
            await cache.save(bundle)
        except Exception:  # noqa: BLE001
            logger.exception("BundleCache save failed for %s", conversation_id)


async def _record_chat_citations(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: _uuid.UUID,
    walked_entity_urns: list[str],
    max_urns: int = 50,
) -> None:
    """Record one ``confidence_deltas`` row per cited entity URN as
    audit trail for the Learning dashboard.

    This is the priority-1 wire-up of the continuous-learning loop.
    Before this, the ``Learning`` admin tab was a Potemkin village
    (4 deltas across the project's lifetime) because nothing in the
    chat path ever called ``learning.record_delta``. Now every chat
    turn writes a delta per entity URN the synthesizer walked, so:

      * Operators can see real citation volume per entity in the
        dashboard ("X was cited 47 times across 31 conversations").
      * The audit trail records WHEN each URN was useful — feeds
        future governance decisions about confidence promotion.
      * No confidence values move from chat alone. The deltas are
        recorded at the noise floor (proposed = current) so they
        sit as audit rows; only verified-task confirmations can
        shift the underlying entity's confidence. That's the right
        policy: chat is implicit positive evidence, not proof.

    Bounded to ``max_urns`` (default 50) so a turn that walked a
    large subgraph doesn't fire 200 inserts. Beyond the cap, the
    URNs are still in the bundle's ``subgraph_entity_urns`` for
    follow-up reuse — only the per-URN delta record is capped.

    Always non-fatal: caller wraps this in try/except so a learning
    failure never breaks the chat response.
    """
    if not walked_entity_urns:
        return
    from bifrost.actions import learning as _learning

    actor = f"chat:{conversation_id}"
    capped = walked_entity_urns[:max_urns]
    written = 0
    skipped = 0
    async with session_factory() as session:
        repo_api = EntityRepository(session)
        for urn in capped:
            entity = await repo_api.get_entity_by_urn(urn)
            if entity is None:
                # URN didn't resolve — could be stale (entity was
                # deleted since extraction) or a synthesizer-invented
                # URN that doesn't exist. Skip silently; it's not a
                # learning signal.
                skipped += 1
                continue
            try:
                # Proposed = current_confidence so diff = 0 → noise
                # floor → row is inserted with applied_by="noise:..."
                # but no shift to entity.confidence. Pure audit.
                from decimal import Decimal as _Dec

                cur_conf = _Dec(str(entity.confidence or 0)).quantize(_Dec("0.01"))
                await _learning.record_delta(
                    session,
                    target_kind="entity",
                    target_id=str(entity.id),
                    proposed_confidence=cur_conf,
                    reason="audit_confirmed",
                    task_id=None,  # chat-only signal
                    actor=actor,
                )
                written += 1
            except Exception:  # noqa: BLE001
                # Per-URN isolation — one bad row shouldn't kill
                # the whole turn's audit.
                logger.exception(
                    "record_delta failed for urn=%s (non-fatal)",
                    urn,
                )
        await session.commit()
    if written or skipped:
        logger.info(
            "chat citations recorded conv=%s written=%d skipped=%d",
            conversation_id,
            written,
            skipped,
        )


async def _await_prior_hydration(
    hydration_task: asyncio.Task[HydratedSubgraph] | None,
) -> tuple[HydratedSubgraph | None, dict[str, Any] | None]:
    """Await an in-flight prior-subgraph hydration kicked off by §1e
    of run_research.

    Returns ``(subgraph, sse_event)`` where:
      * ``subgraph`` is the HydratedSubgraph (or ``None`` if hydration
        failed / wasn't kicked off) — passed verbatim to the synthesis
        prompt builder.
      * ``sse_event`` is the ``prior_subgraph_hydrated`` payload to
        yield, or ``None`` when the hydration was empty / absent /
        failed (no event = no UI surfacing for that turn).

    Extracted so the failure-mode + empty-mode + happy-mode wiring
    is unit-testable without spinning up the full coordinator.
    """
    if hydration_task is None:
        return None, None
    try:
        sub = await hydration_task
    except Exception:  # noqa: BLE001
        logger.exception("prior-subgraph hydration failed")
        return None, None
    if sub.is_empty():
        return sub, None
    return sub, {"type": "prior_subgraph_hydrated", **hydrated_summary(sub)}


def _maybe_narrow_for_extend(
    rankings: list[RepoRanking],
    plan: Any,
    prior_bundle: ResearchBundle | None,
) -> tuple[list[RepoRanking], dict[str, Any] | None]:
    """Filter ``rankings`` to the planner's extension repos when this
    turn is a valid EXTEND_BUNDLE follow-up.

    Returns ``(rankings, event)``. ``event`` is the SSE-shaped
    ``scope_narrowed`` payload to yield when narrowing happened, or
    ``None`` to leave rankings untouched. Conditions for narrowing:

      * planner classified EXTEND_BUNDLE
      * plan has at least one extension repo ID
      * a prior bundle exists (the source of "the rest of the answer")
      * ≥ 1 ranking row matches an extension repo (otherwise the
        rankings would be empty after the filter — bad)

    Extracted out of run_research so the decision is unit-testable
    without faking the full coordinator surface.
    """
    if plan.mode != MODE_EXTEND_BUNDLE or not plan.extension_repos or prior_bundle is None:
        return rankings, None
    extension_ids = {str(rid) for rid in plan.extension_repos}
    narrowed = [rk for rk in rankings if rk.repo_id in extension_ids]
    if not narrowed:
        return rankings, None
    event = {
        "type": "scope_narrowed",
        "from_count": len(rankings),
        "to_count": len(narrowed),
        "to_repos": [rk.repo_name for rk in narrowed],
        "prior_repos": list(prior_bundle.repos_used_names),
        "message": (
            f"Narrowed fan-out from {len(rankings)} to "
            f"{len(narrowed)} repo(s); reusing prior research for "
            f"the {len(prior_bundle.repos_used_names)} repo(s) "
            "already walked."
        ),
    }
    return narrowed, event


def _should_run_clarify_shortcut(
    plan: Any,
    prior_bundle: ResearchBundle | None,
) -> bool:
    """Coordinator-side decision gate for the CLARIFY shortcut.

    True only when the planner classified the turn as CLARIFY *and*
    the prior bundle passes the safety guard (high similarity, non-
    trivial prior answer). For any other combination, the coordinator
    falls through to the full research pipeline.
    """
    if plan.mode != MODE_CLARIFY or prior_bundle is None:
        return False
    return _is_clarify_eligible(prior_bundle, plan.similarity)


async def _run_clarify_path(
    *,
    question: str,
    conversation_id: _uuid.UUID,
    prior_bundle: ResearchBundle,
    question_embedding: list[float] | None,
    prior_turns_text: str,
    settings: BifrostSettings,
    session_factory: async_sessionmaker[AsyncSession],
    clarify_runner: Any = run_clarify_synthesis,
) -> AsyncIterator[dict[str, Any]]:
    """Stream the clarify shortcut and persist a continuation bundle.

    Caller consumes events and forwards them to the SSE client. On
    successful completion, this helper persists a fresh
    ``ResearchBundle`` so a follow-up to *this* clarification still
    benefits from continuity. On Haiku failure, it yields a
    ``_clarify_fallback`` sentinel (and a user-facing status event)
    so the caller knows to drop into the full research pipeline.

    Extracted out of ``run_research`` so the wrap-and-persist logic is
    unit-testable without faking the entire research deps surface.
    """
    accumulated_answer: list[str] = []
    clarify_done: dict[str, Any] | None = None
    failed = False

    async for evt in clarify_runner(
        question=question,
        prior_bundle=prior_bundle,
        prior_turns_text=prior_turns_text,
        settings=settings,
    ):
        etype = evt.get("type")
        if etype == "error":
            failed = True
            logger.warning(
                "clarify shortcut failed; falling back to full pipeline: %s",
                evt.get("error"),
            )
            yield {
                "type": "status",
                "message": (
                    "Clarify shortcut couldn't produce an answer — running full research now."
                ),
            }
            continue  # don't surface raw error
        if etype == "synthesis_delta":
            accumulated_answer.append(str(evt.get("text", "")))
        elif etype == "done":
            clarify_done = evt
        yield evt

    if failed or clarify_done is None:
        yield {"type": "_clarify_fallback"}
        return

    try:
        bundle_to_save = ResearchBundle(
            conversation_id=str(conversation_id),
            last_question=question,
            last_answer="".join(accumulated_answer) or str(clarify_done.get("answer", "")),
            confidence=str(clarify_done.get("confidence", prior_bundle.confidence)),
            repos_used=list(prior_bundle.repos_used),
            repos_used_names=list(prior_bundle.repos_used_names),
            scope_rationale=(
                f"clarify shortcut from prior bundle (conv {prior_bundle.conversation_id})"
            ),
            subgraph_entity_urns=list(prior_bundle.subgraph_entity_urns),
            sources=list(prior_bundle.sources),
            # BUG FIX (embedding-similarity-uses-stale): preserve the
            # prior bundle's last_question_embedding so the NEXT turn's
            # similarity check compares against the ORIGINAL question's
            # embedding, not the clarifying question's embedding.
            # Using question_embedding here would cause turn N+1 to compare
            # against the CLARIFY turn instead of the research turn.
            last_question_embedding=prior_bundle.last_question_embedding,
        )
        await _persist_research_bundle(
            session_factory=session_factory,
            conversation_id=conversation_id,
            bundle=bundle_to_save,
        )
    except Exception:  # noqa: BLE001
        logger.exception("clarify-path bundle persist failed")


async def _count_code_entities(
    session_factory: async_sessionmaker[AsyncSession],
    repo_id: Any,
) -> int:
    """Quick count of code-shaped entities (component / flow / dependency)
    for a single repo. Used after an auto-extraction finishes to decide
    whether the repo is now safe to promote to deep-indexed."""
    async with session_factory() as session:
        from sqlalchemy import func as _f
        from sqlalchemy import select as _s

        stmt = (
            _s(_f.count(Entity.id))
            .where(Entity.scope_repo_id == repo_id)
            .where(Entity.entity_type.in_(["component", "flow", "dependency"]))
        )
        return int((await session.execute(stmt)).scalar() or 0)


async def run_research(
    *,
    question: str,
    conversation_id: _uuid.UUID,
    deps: ResearchDeps,
    explicit_scope_repo_ids: list[str] | None = None,
    research_run_id: _uuid.UUID | None = None,
    turn_id: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Execute the full research pipeline. Yields stream events.

    Event types yielded:
      {"type": "status", "message": str}
      {"type": "scope", "candidates": [{id,name}, ...]}
      {"type": "freshness", "repos": [{name,stale,cloned_now}, ...]}
      {"type": "route", "selected": [{id,name,relevance,reason}, ...]}
      {"type": "subagent_start", "repo_name": str}
      {"type": "subagent_done", "repo_name": str, "relevance": str, "files": int}
      {"type": "synthesis_delta", "text": str}           (streamed tokens)
      {"type": "done", "answer": str, "sources": [...],
                        "usage": {cost,input,output,cache_read,n_calls},
                        "confidence": str, "reports": [RepoReport...]}
      {"type": "error", "error": str}
    """
    settings = deps.settings
    agg = SynthesisUsage()

    # ── -1. Jira ticket chain pre-fetch ──────────────────────────────────────
    # Fetch the linked Jira ticket chain up front so the router, planner and
    # synthesiser see it without the user pasting the description.
    jira_context = await _prefetch_jira_chain(question, settings)
    had_jira_chain = bool(jira_context)
    if jira_context:
        question = jira_context + "\n## Original question\n" + question
        yield {
            "type": "status",
            "message": "Fetched Jira ticket chain — researching relevant repos…",
        }

    # ── 0. Fast-path: simple entity/ownership lookup (no agent fan-out) ──────
    from bifrost.research.fast_path import _try_fast_path, check_already_built

    fast_path_gen = await _try_fast_path(
        question=question,
        conversation_id=conversation_id,
        session_factory=deps.session_factory,
        settings=settings,
        scope_repo_ids=explicit_scope_repo_ids,
    )
    if fast_path_gen is not None:
        fast_path_ok = True
        async for evt in fast_path_gen:
            if evt.get("type") == "_fast_path_synthesis_error":
                # Synthesis failed — fall through to full pipeline
                fast_path_ok = False
                break
            yield evt
        if fast_path_ok:
            return

    # ── 0b. Already-built detector: advisory pre-check before full pipeline ──
    # Emits "already_built" SSE if the question is a build request and we find
    # matching entities. Main pipeline continues regardless.
    async for evt in check_already_built(question, deps.session_factory, settings):
        yield evt

    # ── 1. Resolve scope + load prior turns ──────────────────────────
    yield {"type": "status", "message": "Resolving scope…"}
    prior_turns_text = ""
    prior_bundle: ResearchBundle | None = None
    async with deps.session_factory() as session:
        repo = EntityRepository(session)
        conv = await repo.get_conversation(conversation_id)
        if conv is None:
            yield {"type": "error", "error": "Conversation not found"}
            return
        # Load the per-conversation chat settings.
        from bifrost.api.schemas.chat_settings import settings_from_stored

        stored_settings = await repo.get_chat_settings(conversation_id)
        chat_settings = settings_from_stored(stored_settings)
        # "User-touched settings" signal for plan-vs-settings
        # precedence: empty stored dict → plan can override defaults;
        # non-empty → settings are the user's opinion and always win.
        user_touched_settings = bool(stored_settings)
        logger.info(
            "chat_settings conv=%s walk_depth=%d confidence_floor=%.2f "
            "edge_directions=%s agents_per_repo_max=%d external_sources=%s "
            "auto_extract=%s user_touched=%s",
            conversation_id,
            chat_settings.walk_depth,
            chat_settings.confidence_floor,
            chat_settings.edge_directions,
            chat_settings.agents_per_repo_max,
            chat_settings.external_sources,
            chat_settings.auto_extract_missing,
            user_touched_settings,
        )

        # The orchestrator reads chat context, emits an
        # OrchestrationPlan with optional walk-parameter overrides.
        # Falls back to no-op plan on LLM failure (chat continues with
        # settings defaults). The plan is plumbed below into the
        # master subgraph build via merge_plan_with_settings.
        from bifrost.research.orchestrator import (
            merge_plan_with_settings,
            run_orchestrator,
        )

        orch_plan = await run_orchestrator(
            question=question,
            prior_turns=prior_turns_text,
            settings=settings,
        )
        resolved_walk = merge_plan_with_settings(
            orch_plan,
            chat_settings,
            user_touched_settings=user_touched_settings,
            settings=settings,
        )
        # Surface the plan to the UI so the user knows what kind of
        # research is happening. ``intent`` + 1-line ``reasoning``.
        yield {
            "type": "plan",
            "intent": orch_plan.intent,
            "reasoning": orch_plan.reasoning,
            "is_fallback": orch_plan.is_fallback,
            "resolved_walk": {
                "walk_depth": resolved_walk.walk_depth,
                "walk_breadth_soft_cap": resolved_walk.walk_breadth_soft_cap,
                "confidence_floor": resolved_walk.confidence_floor,
                "edge_directions": resolved_walk.edge_directions,
            },
        }
        candidates = await _resolve_candidates(session, conv, explicit_scope_repo_ids)

        # Detect unscoped fallback up front — both the squad match
        # and the prior-bundle inheritance below check this.
        is_unscoped_fallback = (
            not explicit_scope_repo_ids and not conv.scope_repo_ids and conv.scope_domain_id is None
        )

        # ── PRIOR-BUNDLE SCOPE INHERITANCE — runs BEFORE keyword/squad
        # filters so a follow-up turn doesn't get its scope re-derived
        # from this question's keywords alone.
        #
        # Follow-up keywords rarely match the prior repos by name, so the
        # prefilter would pad with unrelated repos. Planner-detected repos
        # outside the pool (``extension_repo_names``) are added back below.
        prior_bundle_early = await _load_prior_bundle(repo, conversation_id)
        inherited_from_prior = False
        if (
            prior_bundle_early is not None
            and prior_bundle_early.repos_used
            and is_unscoped_fallback
        ):
            prior_repo_ids = {rid for rid in prior_bundle_early.repos_used if rid}
            kept = [r for r in candidates if str(r.id) in prior_repo_ids]
            if kept:
                original_pool = len(candidates)
                candidates = kept
                inherited_from_prior = True
                yield {
                    "type": "scope_inherited_from_prior",
                    "from_count": original_pool,
                    "to_count": len(candidates),
                    "repo_names": [r.name for r in candidates[:8]],
                    "message": (
                        f"Continuing from the prior turn — narrowed from "
                        f"{original_pool} to {len(candidates)} repo(s). "
                        "Mention a different repo by name to widen the scope."
                    ),
                }

        # Squad match runs first: a squad name lives in
        # ``RepoOwnership.squad``, not in repo names, so narrow by it
        # before the keyword/router step sees a padded pool.
        squad_match_names: list[str] = []
        if (
            len(candidates) > _ROUTER_PREFILTER_THRESHOLD
            and not explicit_scope_repo_ids
            and not conv.scope_repo_ids
        ):
            candidates, squad_match_names = await _squad_match_filter(
                session,
                candidates,
                question,
            )
            if squad_match_names:
                yield {
                    "type": "scope_squad_matched",
                    "squads": squad_match_names,
                    "candidate_count": len(candidates),
                    "message": (
                        f"Matched squad{'s' if len(squad_match_names) > 1 else ''}: "
                        f"{', '.join(squad_match_names[:3])}"
                        + (
                            f" (+{len(squad_match_names) - 3} more)"
                            if len(squad_match_names) > 3
                            else ""
                        )
                        + f" — narrowed scope to {len(candidates)} "
                        + "owned repo(s)."
                    ),
                }

        # Router can't usefully rank hundreds of candidates — Haiku's
        # structured-output budget runs out, the unranked tail backfills
        # as "low", and the stable sort returns the alphabetical-first
        # 5 every time regardless of the question. Keyword-prefilter
        # down to ~50 first so the router actually does its job.
        #
        # Skip the keyword prefilter once ``_squad_match_filter`` narrowed
        # the set: filtering a squad's repos by name keywords drops repos
        # that don't spell the keyword. The router's top_k has the headroom.
        if len(candidates) > _ROUTER_PREFILTER_THRESHOLD and not squad_match_names:
            (
                candidates,
                prefilter_drop,
                prefilter_reason,
            ) = await _keyword_prefilter_repos(
                session,
                question,
                candidates,
            )
            if prefilter_reason == "no_keyword_match":
                # No keyword matches a repo and no squad matched: stop and ask
                # instead of padding with random repos and running the pipeline.
                tokens = _question_keywords(question)
                yield {
                    "type": "error",
                    "error": (
                        "Bifrost couldn't find any repos matching "
                        f"the keywords in your question ({', '.join(repr(t) for t in tokens[:3])}"
                        + ("…" if len(tokens) > 3 else "")
                        + "). Try one of:\n"
                        "  • Mention a specific repo by name "
                        '(e.g. "in order-service, …")\n'
                        "  • Pick a domain or squad from the scope "
                        "dropdown above the chat\n"
                        "  • Rephrase using terms that appear in repo "
                        "names or descriptions"
                    ),
                }
                return
            if prefilter_drop > 0:
                yield {
                    "type": "status",
                    "message": (
                        f"Keyword prefilter: {prefilter_drop} repos dropped "
                        f"({len(candidates)} kept for routing)"
                    ),
                }
        # NOTE: candidates may legitimately be empty for a domain-scoped
        # turn whose registered repos haven't landed yet — the org
        # catalog (manifest-derived ``OrgRepo`` rows) often has the
        # team's repos before we've explicitly registered them in
        # ``Repo``. Catalog expansion in §1b auto-registers them on
        # the fly, so DON'T error out here for the domain case;
        # defer the empty-check to after expansion.
        if not candidates and conv.scope_domain_id is None:
            if explicit_scope_repo_ids or conv.scope_repo_ids:
                yield {
                    "type": "error",
                    "error": (
                        "The selected repos aren't registered. Register "
                        "them or pick from the existing repo list."
                    ),
                }
            else:
                yield {"type": "error", "error": "No repositories in scope."}
            return
        # Surface the org-wide fallback ONLY when we didn't already
        # inherit a scope from the prior turn. The inherit branch
        # narrows the pool AND yields its own banner; if both fired
        # the UI would show "searching across N repos" right next to
        # "narrowed from N to K", which is confusing.
        if is_unscoped_fallback and not inherited_from_prior:
            yield {
                "type": "scope_fallback",
                "scope": "org_wide",
                "candidate_count": len(candidates),
                "message": (
                    f"No domain or repo selected — searching across "
                    f"{len(candidates)} repo(s). Pick a domain or "
                    "specific repo for tighter results."
                ),
            }
        # Prior turns — excludes the current user message (which is N)
        # so we feed the assistant the conversation context without
        # repeating the question it's about to answer.
        prior_messages = await repo.list_messages(conversation_id)
        # Drop the final user message (the current question) from context
        if prior_messages and getattr(prior_messages[-1], "role", None) == "user":
            prior_messages = prior_messages[:-1]
        prior_turns_text = _format_prior_turns(prior_messages, max_turns=4)
        # Reuse the prior_bundle we loaded earlier (before the
        # keyword/squad filters so the inherit-scope path could kick
        # in). Don't re-load — the bundle hasn't changed since.
        prior_bundle = prior_bundle_early

    # ── 1c. Follow-up planner ─────────────────────────────────────────
    # Embed the new question and classify against the prior bundle so
    # the UI can show "this builds on your last turn" continuity. The
    # plan is saved into the bundle we'll persist at the end so the
    # next turn benefits from this classification's lineage.
    question_embedding = await _embed_question_async(question)
    # When we inherited scope from the prior turn (1b'), the planner
    # should still be able to detect mentions of repos OUTSIDE the
    # narrowed pool — that's how the user widens scope mid-conversation.
    # Build the planner's repo dictionary from the full, un-narrowed
    # repo list (a single DB query; fast in-memory map).
    if inherited_from_prior:
        async with deps.session_factory() as session:
            from sqlalchemy import select as _sel_all

            full_rows = (await session.execute(_sel_all(Repo.id, Repo.name))).all()
        available_repo_id_by_name = {n: str(rid) for rid, n in full_rows}
    else:
        available_repo_id_by_name = {r.name: str(r.id) for r in candidates}
    research_plan = await classify_followup(
        question=question,
        prior_bundle=prior_bundle,
        question_embedding=question_embedding,
        available_repo_names=list(available_repo_id_by_name.keys()),
        available_repo_id_by_name=available_repo_id_by_name,
        llm_classifier=llm_classify_followup if prior_bundle is not None else None,
    )

    # If the planner spotted new repo names in the question, fold them
    # back into ``candidates`` so the router can see them. Without this,
    # the inherit-narrow above would silently drop the user's scope-
    # widening.
    if inherited_from_prior and research_plan.extension_repos:
        already = {str(r.id) for r in candidates}
        new_ids = [rid for rid in research_plan.extension_repos if rid not in already]
        if new_ids:
            async with deps.session_factory() as session:
                from sqlalchemy import select as _sel_ext

                added = (
                    (
                        await session.execute(
                            _sel_ext(Repo).where(
                                Repo.id.in_([_uuid.UUID(rid) for rid in new_ids]),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            candidates = list(candidates) + list(added)
            yield {
                "type": "scope_widened",
                "added_repos": [r.name for r in added],
                "message": (
                    f"Widened scope by {len(added)} repo(s) you mentioned: "
                    + ", ".join(r.name for r in added)
                ),
            }
    yield {"type": "research_plan", **research_plan.to_dict()}

    will_run_clarify_shortcut = _should_run_clarify_shortcut(
        research_plan,
        prior_bundle,
    )
    if research_plan.mode == MODE_CLARIFY and prior_bundle is not None:
        clarify_message = (
            "Skipping fresh research — answering from the prior bundle "
            "directly via a quick Haiku call."
            if will_run_clarify_shortcut
            else (
                "Builds on prior research, but similarity is below the "
                "shortcut threshold — running the full pipeline for "
                "safety."
            )
        )
        yield {
            "type": "bundle_reused",
            "conversation_id": str(conversation_id),
            "prior_repos": list(prior_bundle.repos_used_names),
            "shortcut": "clarify_from_bundle" if will_run_clarify_shortcut else None,
            "message": clarify_message,
        }
    elif research_plan.mode == MODE_EXTEND_BUNDLE and prior_bundle is not None:
        yield {
            "type": "bundle_reused",
            "conversation_id": str(conversation_id),
            "prior_repos": list(prior_bundle.repos_used_names),
            "extension_repos": list(research_plan.extension_repo_names),
            "message": (
                "Extending prior research; new agents will only fan out "
                "to the additional repos mentioned this turn."
                if research_plan.extension_repo_names
                else "Extending prior research; reusing the same repo set."
            ),
        }

    # ── 1d. CLARIFY shortcut ──────────────────────────────────────────
    # When the planner is confident the new turn is a clarification of
    # the prior one, skip the full pipeline and stream a Haiku answer
    # built from the prior bundle. ``_should_run_clarify_shortcut``
    # gates on classifier verdict + similarity floor + substantive
    # prior answer; below any bar we fall through to full research.
    if will_run_clarify_shortcut:
        assert prior_bundle is not None  # narrowed by guard
        fell_through = False
        async for evt in _run_clarify_path(
            question=question,
            conversation_id=conversation_id,
            prior_bundle=prior_bundle,
            question_embedding=question_embedding,
            prior_turns_text=prior_turns_text,
            settings=settings,
            session_factory=deps.session_factory,
        ):
            if evt.get("type") == "_clarify_fallback":
                fell_through = True
                continue
            yield evt
        if not fell_through:
            return  # shortcut succeeded

    # ── 1e. Kick off prior-subgraph hydration in the background ──────
    # Whenever we're about to run the full pipeline AND there's a
    # prior bundle with walked URNs, hydrate the prior entities + 1-hop
    # edges concurrently with the freshness sync + agent fan-out.
    # Synthesis (line ~2050) awaits the result and renders a "## Prior
    # research evidence" block alongside the live fan-out so cross-turn
    # flow reasoning has structured evidence on both sides of the comparison.
    hydration_task: asyncio.Task[HydratedSubgraph] | None = None
    if prior_bundle is not None and prior_bundle.subgraph_entity_urns:
        hydration_task = asyncio.create_task(
            hydrate_prior_subgraph(
                urns=list(prior_bundle.subgraph_entity_urns),
                session_factory=deps.session_factory,
            ),
            name="bifrost.coordinator.hydrate_prior",
        )

    yield {
        "type": "scope",
        "candidates": [{"id": str(r.id), "name": r.name} for r in candidates],
    }

    # ── 1b. Catalog expansion (domain-scoped only) ───────────────────
    # When a user asks with domain scope, they may be asking about repos
    # the org owns but we haven't formally registered yet. Expand the
    # candidate pool from the org-wide catalog + auto-register any picks.
    # We track which ids were auto-registered this turn so the
    # status classifier can emit a distinct "auto_registered" chip
    # (manifest_only AND fresh discovery).
    auto_registered_ids: set[str] = set()
    catalog_candidates: list[OrgRepo] = []
    if conv.scope_domain_id and not explicit_scope_repo_ids and not conv.scope_repo_ids:
        async with deps.session_factory() as session:
            from sqlalchemy import select as _sel

            domain = (
                await session.execute(
                    _sel(Domain).where(Domain.id == conv.scope_domain_id),
                )
            ).scalar_one_or_none()
            registered_names = {r.name for r in candidates}
            catalog_candidates = await _catalog_candidates_for_domain(
                session,
                domain,
                registered_names,
            )

        if catalog_candidates:
            yield {
                "type": "catalog_expansion",
                "message": (
                    f"Found {len(catalog_candidates)} additional "
                    f"{domain.name if domain else '?'} repos in the org "
                    f"catalog not yet registered. Considering these too."
                ),
                "repos": [
                    {"owner": c.owner, "name": c.name, "url": c.url}
                    for c in catalog_candidates[:20]
                ],
            }

            # Auto-register each catalog candidate so they join the
            # research fan-out. Each auto-register is fast (DB insert
            # only); the expensive part is the clone that happens during
            # freshness check just below.
            for cat in catalog_candidates:
                try:
                    reg = await _auto_register_from_catalog(
                        deps.session_factory,
                        cat,
                        conv.scope_domain_id,
                    )
                    candidates.append(reg)
                    auto_registered_ids.add(str(reg.id))
                    yield {
                        "type": "auto_registered",
                        "repo_name": reg.name,
                        "url": reg.url,
                        "reason": (
                            "Catalog match for domain "
                            f"{domain.name if domain else ''} — "
                            "will shallow-clone + research on-the-fly."
                        ),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "auto-register of %s failed: %s",
                        cat.name,
                        exc,
                    )

    # Post-expansion empty check. If a domain-scoped turn started with
    # zero registered repos AND catalog expansion couldn't find any
    # OrgRepo rows tagged with this squad, we have nothing to research.
    # Tell the user explicitly so they know to fix the squad name.
    if not candidates:
        if conv.scope_domain_id is not None:
            yield {
                "type": "error",
                "error": (
                    "This domain has no registered repos AND no matching "
                    "repos in the org catalog. The team's ownership manifest "
                    "likely uses a different squad name than the domain "
                    "row — check the domain's name vs the squad field "
                    "in the catalog. You can also pick repos manually."
                ),
            }
        else:
            yield {"type": "error", "error": "No repositories in scope."}
        return

    # ── 2. Router FIRST: pick top-K relevant from candidates (NO clones yet) ─
    # Router only needs metadata (name + component_count + flow_count +
    # sample_component_names), all in the DB. Moving it before the
    # filesystem step means we avoid ls-remote'ing + cloning hundreds
    # of repos we're about to discard.
    yield {
        "type": "status",
        "message": f"Routing {len(candidates)} candidate repo(s) to find the most relevant…",
    }

    async with deps.session_factory() as session:
        freshness_rows = await _load_freshness_rows(
            session,
            [r.id for r in candidates],
        )
        router_candidates: list[RepoCandidate] = []
        from sqlalchemy import select as _sel

        for r in candidates:
            fr_row = freshness_rows.get(r.id)
            ent_stmt = (
                _sel(Entity.name)
                .where(
                    Entity.scope_repo_id == r.id,
                    Entity.entity_type == "component",
                )
                .limit(6)
            )
            samples = [row[0] for row in (await session.execute(ent_stmt)).all()]
            router_candidates.append(
                RepoCandidate(
                    id=str(r.id),
                    name=r.name,
                    description=(r.description or (r.adapter_config or {}).get("description")),
                    component_count=(fr_row.component_count if fr_row else 0) or 0,
                    flow_count=(fr_row.flow_count if fr_row else 0) or 0,
                    sample_component_names=samples,
                    is_primary=bool(getattr(r, "is_primary", False)),
                )
            )

    # top_k is no longer a hard 5 — the router ranks up to
    # ``walk_breadth_soft_cap`` repos so wide-scope questions actually
    # get wide-scope coverage. Status classification + auto-extract run
    # on the full ranked set below; only the cross-repo agent-fanout
    # is throttled by ``_FANOUT_CONCURRENCY``.
    router_top_k = max(1, int(resolved_walk.walk_breadth_soft_cap or 200))
    rankings: list[RepoRanking] = await rank_repos(
        question=question,
        candidates=router_candidates,
        top_k=router_top_k,
        settings=settings,
    )
    if not rankings:
        yield {"type": "error", "error": "Router returned no candidates."}
        return

    # ── Narrow fan-out for EXTEND_BUNDLE follow-ups ─────────────────
    # When the planner identified specific NEW repos the user is
    # bringing into scope this turn, only fan out agents on those
    # extension repos. Prior repos contribute via the prior_bundle's
    # subgraph URNs + sources block in the synthesis prompt — we
    # already paid the research cost for them last turn. Fewer agents
    # = faster + cheaper follow-ups.
    narrowed_rankings, narrow_event = _maybe_narrow_for_extend(
        rankings,
        research_plan,
        prior_bundle,
    )
    if narrow_event is not None:
        yield narrow_event
        rankings = narrowed_rankings

    # Keep only candidates the router picked.
    selected_ids = {rk.repo_id for rk in rankings}
    selected_candidates = [r for r in candidates if str(r.id) in selected_ids]

    # ── 3. Freshness — ONLY on router's top-K ───────────────────────
    yield {
        "type": "status",
        "message": f"Syncing {len(selected_candidates)} selected repo clone(s)…",
    }
    fresh_input = [
        {
            "id": str(r.id),
            "name": r.name,
            "url": r.url,
            "last_extracted_sha": (
                freshness_rows.get(r.id).last_commit_indexed if freshness_rows.get(r.id) else None
            ),
        }
        for r in selected_candidates
    ]
    fresh_results: list[RepoFreshState] = await ensure_fresh_clones(
        fresh_input,
        settings=settings,
        concurrency=5,
    )

    yield {
        "type": "freshness",
        "repos": [
            {
                "id": f.repo_id,
                "name": f.repo_name,
                "stale": f.metadata_stale,
                "cloned_now": f.ensured_cloned,
                "last_extracted_sha": (f.last_extracted_sha or "")[:8],
                "remote_head": (f.remote_head or "")[:8],
                "error": f.error,
            }
            for f in fresh_results
        ],
    }

    stale_repos = [f for f in fresh_results if f.metadata_stale and not f.error]
    if stale_repos:
        yield {
            "type": "staleness_warning",
            "stale_count": len(stale_repos),
            "total": len(fresh_results),
            "repos": [
                {
                    "id": f.repo_id,
                    "name": f.repo_name,
                    "last_extracted_sha": (f.last_extracted_sha or "")[:8],
                    "remote_head": (f.remote_head or "")[:8],
                    "refresh_endpoint": f"/api/v1/repos/{f.repo_id}/extract",
                }
                for f in stale_repos
            ],
        }

    # Drop repos we couldn't sync. Router's `rankings` list stays —
    # downstream code filters via fresh_by_id.
    fresh_by_id: dict[str, RepoFreshState] = {f.repo_id: f for f in fresh_results}
    usable = [
        r
        for r in selected_candidates
        if fresh_by_id.get(str(r.id)) and not fresh_by_id[str(r.id)].error
    ]
    if not usable:
        # When every repo failed to clone the cause is usually one systemic
        # issue (missing git credentials); surface an actionable message
        # instead of the generic "Could not clone any selected repo."
        errs = [(f.error or "") for f in fresh_results if f.error]
        auth_pattern = (
            "could not read Username",
            "Authentication failed",
            "Permission denied",
            "fatal: Authentication",
        )
        is_auth = bool(errs) and all(
            any(p.lower() in e.lower() for p in auth_pattern) for e in errs
        )
        # Persist a partial bundle with the routed scope before the early
        # return, so the next turn's inherit-from-prior block has one.
        try:
            partial_bundle = ResearchBundle(
                conversation_id=str(conversation_id),
                last_question=question,
                last_answer="",
                confidence="low",
                repos_used=[str(r.id) for r in selected_candidates],
                repos_used_names=[r.name for r in selected_candidates],
                scope_rationale=(
                    f"Partial bundle — clone failed for all "
                    f"{len(errs)} routed repos"
                    + (" (auth issue — see error)." if is_auth else ".")
                    + " Scope captured for follow-up continuity."
                ),
                subgraph_entity_urns=[],
                sources=[],
                last_question_embedding=question_embedding,
            )
            await _persist_research_bundle(
                session_factory=deps.session_factory,
                conversation_id=conversation_id,
                bundle=partial_bundle,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "partial bundle persist failed on clone-error path (non-fatal)",
            )
        if is_auth:
            yield {
                "type": "error",
                "error": (
                    "GitHub authentication is missing — every "
                    "`git ls-remote` failed with an auth error.\n"
                    "  • Run on the Bifrost host: `gh auth setup-git`\n"
                    "  • Or set up a credential helper for git\n"
                    "After that, retry this question — the freshness "
                    "step will succeed."
                ),
            }
        else:
            yield {
                "type": "error",
                "error": (
                    "Could not clone any of the selected repos. "
                    f"({len(errs)} repos errored.) "
                    "Sample error: " + (errs[0][:200] if errs else "(no error message)")
                ),
            }
        return
    # Build a quick id→is_primary map so the UI can show the primary
    # badge without re-querying (router_candidates already carry the
    # flag from the Repo row).
    is_primary_by_id = {c.id: c.is_primary for c in router_candidates}
    yield {
        "type": "route",
        "selected": [
            {
                "id": rk.repo_id,
                "name": rk.repo_name,
                "relevance": rk.relevance,
                "reason": rk.reason,
                "is_primary": bool(is_primary_by_id.get(rk.repo_id, False)),
            }
            for rk in rankings
        ],
    }
    # Human-readable status reinforces the route for the UI's status line.
    repo_name_list = ", ".join(rk.repo_name for rk in rankings[:5])
    yield {
        "type": "status",
        "message": (
            f"Investigating {len(rankings)} repo(s): {repo_name_list}"
            + ("…" if len(rankings) > 5 else "")
        ),
    }

    # ── 3b. Classify picks as deep-indexed vs manifest-only ──────────────
    # Repos with zero extracted components/flows/dependencies have
    # only manifest metadata. For the current query we'll still
    # reason about them via their service entity + community summary,
    # but we also kick off background extraction so follow-up queries
    # get deep answers. The UI shows a chip per auto-extraction so the
    # user knows they're getting a better answer next time.
    #
    # Classification is factored into
    # ``bifrost.research.status.classify_repo_status``. We run it on
    # the FULL ``usable`` set (router top-K is no longer hard-capped at
    # 5), and emit a ``status`` SSE event per repo so the UI can show a
    # live status chip per discovered repo before agents fan out.
    from bifrost.research.status import (
        classify_repo_status,
        status_summary,
    )
    from bifrost.research.status import (
        manifest_only_ids as _manifest_only_ids,
    )

    async with deps.session_factory() as session:
        code_entity_counts: dict[str, int] = {}
        from sqlalchemy import func as _f
        from sqlalchemy import select as _s

        # Count entities for the FULL selected set (not just usable) so
        # not_cloned chips can still report any prior extraction depth
        # even when the current clone failed.
        cnt_stmt = (
            _s(Entity.scope_repo_id, _f.count(Entity.id))
            .where(Entity.scope_repo_id.in_([r.id for r in selected_candidates]))
            .where(Entity.entity_type.in_(["component", "flow", "dependency"]))
            .group_by(Entity.scope_repo_id)
        )
        for rid, c in (await session.execute(cnt_stmt)).all():
            code_entity_counts[str(rid)] = int(c)

    # Classify selected_candidates (not just usable) so the UI chip-row
    # surfaces ``not_cloned`` red chips for repos that failed freshness
    # — informative even though we won't fan out agents on them this
    # turn.
    status_by_id = classify_repo_status(
        selected_candidates,
        code_entity_counts=code_entity_counts,
        fresh_by_id=fresh_by_id,
        auto_registered_ids=auto_registered_ids,
    )

    # Per-repo status SSE event. The UI consumes these to render a
    # chip-row under "Repos discovered" before any agent has spoken.
    # ORDER MATTERS: this event must fire BEFORE ``auto_extracting``
    # below so the UI can paint the full chip-row first, then update
    # individual chips as wait-and-integrate extractions complete.
    # Reordering would cause the UI to flash the auto-extract banner
    # for repos that hadn't yet appeared as chips.
    yield {
        "type": "status_classification",
        "summary": status_summary(status_by_id),
        "repos": [
            {
                "repo_id": info.repo_id,
                "repo_name": info.repo_name,
                "status": info.status,
                "code_entity_count": info.code_entity_count,
                "auto_registered": info.auto_registered,
                "clone_error": info.clone_error,
            }
            for info in status_by_id.values()
        ],
    }

    manifest_only_repo_ids = set(_manifest_only_ids(status_by_id))
    manifest_only_repos: list[Repo] = [r for r in usable if str(r.id) in manifest_only_repo_ids]
    deep_indexed_repos: list[Repo] = [
        r for r in usable if status_by_id[str(r.id)].status == "deep_indexed"
    ]

    if manifest_only_repos:
        # Wait-and-integrate auto-extraction for manifest-only picks. We
        # submit a deep extraction per manifest-only repo, then block the
        # chat for up to ``auto_extract_wait_s`` seconds. Whatever
        # finishes inside the budget gets promoted to deep-indexed and
        # included in this answer; the rest keep running in the
        # background and surface in follow-up turns.
        from bifrost.extraction.worker import get_worker

        _worker = get_worker()
        async with deps.session_factory() as session:
            # EntityRepository imported at module level; the inline
            # re-import here caused an UnboundLocalError at line 666
            # because Python treats any assigned name as local-to-function
            # throughout the function.
            _repo_api = EntityRepository(session)
            queued: list[dict[str, Any]] = []
            queued_repos: list[tuple[str, str, Repo]] = []  # (task_id, repo_id, repo)
            for r in manifest_only_repos:
                try:
                    task = await _repo_api.create_task(
                        task_type="extraction",
                        query=f"Auto-extract (first-touch) for {r.name}",
                        scope_json={
                            "repo_ids": [str(r.id)],
                            "force_full": False,
                            "triggered_by": "query_auto_extract",
                        },
                        budget_json={},
                    )
                    # Do NOT submit to the worker here: it reads from a different
                    # session and raises "Task not found" if it runs before this
                    # transaction commits. Submission moves below, after commit.
                    entry = {
                        "repo_id": str(r.id),
                        "repo_name": r.name,
                        "task_id": str(task.id),
                    }
                    queued.append(entry)
                    queued_repos.append((str(task.id), str(r.id), r))
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "auto-extraction submit failed for %s",
                        r.name,
                    )
            await session.commit()

        # Now that the task rows are committed and visible to other
        # connections, hand them off to the worker. The worker's
        # _load_task_and_repo will find the rows; no more racy
        # "Task not found" failures.
        for task_id, _repo_id, _r in queued_repos:
            _worker.submit(task_id)

        if queued:
            wait_s = get_settings().auto_extract_wait_s
            yield {
                "type": "auto_extracting",
                "message": (
                    f"Started deep extraction on {len(queued)} repo(s) "
                    f"— waiting up to {int(wait_s)}s before answering "
                    "(extractions that don't finish in time will surface "
                    "in your next follow-up)."
                ),
                "wait_seconds": wait_s,
                "repos": queued,
            }

            promoted_repos: list[Repo] = []
            promoted_ids: set[str] = set()
            async for evt in _wait_and_integrate_extractions(
                worker=_worker,
                queued_repos=queued_repos,
                wait_s=wait_s,
                session_factory=deps.session_factory,
                promoted_out=promoted_repos,
                promoted_ids_out=promoted_ids,
            ):
                yield evt

            if promoted_repos:
                deep_indexed_repos = list(deep_indexed_repos) + promoted_repos
                manifest_only_repos = [
                    r for r in manifest_only_repos if str(r.id) not in promoted_ids
                ]
                yield {
                    "type": "research_upgraded",
                    "promoted": [
                        {"repo_id": str(r.id), "repo_name": r.name} for r in promoted_repos
                    ],
                    "message": (
                        f"Promoted {len(promoted_repos)} repo(s) to deep-"
                        "indexed for this answer: " + ", ".join(r.name for r in promoted_repos)
                    ),
                }
            else:
                yield {
                    "type": "status",
                    "message": (
                        "No deep extraction finished within the budget; "
                        "this answer relies on manifest metadata + community "
                        "summaries."
                    ),
                }

    # For the current query we can only fan-out code agents to the
    # deep-indexed ones — manifest-only repos contribute via their
    # service entity + community summary already embedded in the
    # master subgraph. Narrow rankings + usable to deep-indexed.
    deep_indexed_ids = {str(r.id) for r in deep_indexed_repos}

    # ── Critical-repo extraction gate ─────────────────────────────────
    # Answering about a high-relevance repo that is not indexed produces
    # T4-only results that look authoritative but may contradict code.
    # Surface a hard warning before proceeding — not a soft caveat.
    unindexed_critical = [
        rk
        for rk in rankings
        if rk.repo_id not in deep_indexed_ids and rk.relevance in ("high", "medium")
    ]
    if unindexed_critical:
        names = ", ".join(rk.repo_name for rk in unindexed_critical[:4])
        yield {
            "type": "extraction_required",
            "repos": [
                {"name": rk.repo_name, "relevance": rk.relevance} for rk in unindexed_critical
            ],
            "message": (
                f"\u26a0 Critical repos not yet code-indexed: {names}. "
                f"These were ranked {unindexed_critical[0].relevance}-relevance "
                f"for this question but extraction has not finished. "
                f"Proceeding with documentation-only (T4) evidence — "
                f"file paths, prop names, and ownership come from Confluence/Jira, "
                f"NOT verified against source code. "
                f"Re-ask once extraction finishes for disk-grounded analysis."
            ),
        }

    rankings = [rk for rk in rankings if rk.repo_id in deep_indexed_ids]
    usable = deep_indexed_repos
    if not rankings:
        yield {
            "type": "status",
            "message": (
                "No deep-indexed repos in scope — answer will draw "
                "on manifest metadata + community summaries + "
                "Confluence/Jira only."
            ),
        }

    # ── 4. Fan-out: per-repo × per-agent-type (2D) ────────────────────
    repo_by_id = {str(r.id): r for r in usable}
    keywords = _extract_keywords(question)

    # Pre-load each repo's ownership metadata (manifest-derived) so
    # Jira/Confluence agents know which project/space to scope to.
    async with deps.session_factory() as session:
        from sqlalchemy import select as _select

        ownership_stmt = _select(RepoOwnership).where(
            RepoOwnership.repo_id.in_([r.id for r in usable]),
        )
        ownership_rows = (await session.execute(ownership_stmt)).scalars().all()
        ownership_by_id = {str(o.repo_id): o for o in ownership_rows}

    # ── 4a. Master query subgraph (Stage 3) ──────────────────────────
    # Build ONE cross-repo subgraph spanning every candidate repo
    # before fan-out. Each code agent then receives its *slice* of this
    # subgraph (its in-repo nodes + boundary nodes in other repos +
    # touching edges). This is what gives agents cross-repo context —
    # without it they're blind to edges leaving their repo.
    from bifrost.research.communities import search_communities
    from bifrost.research.graph_retrieval import (
        FlowPath,
        build_query_subgraph,
        extract_repo_slice,
        find_flow_paths,
    )

    master_subgraph = None
    try:
        async with deps.session_factory() as session:
            # Walk parameters come from the orchestrator's
            # plan merged with the user's settings. ``resolved_walk``
            # is the final shape — see ``merge_plan_with_settings``.
            master_subgraph = await build_query_subgraph(
                session,
                question,
                repo_ids=[str(r.id) for r in usable],
                depth=resolved_walk.walk_depth,
                soft_cap=resolved_walk.walk_breadth_soft_cap,
                seeds=min(10, max(4, len(usable))),
                confidence_floor=resolved_walk.confidence_floor,
                direction=resolved_walk.edge_directions,
                rel_types=resolved_walk.edge_types,
            )
        # Surface to the user when their walk exceeded the soft
        # cap so they can tighten the breadth knob if they want sharper
        # results. Doesn't truncate — every node still made it back.
        if master_subgraph is not None and master_subgraph.soft_cap_exceeded:
            yield {
                "type": "status",
                "message": (
                    f"Blast-radius walk exceeded the soft cap "
                    f"({resolved_walk.walk_breadth_soft_cap} nodes). "
                    f"All {master_subgraph.stats.get('total_nodes', 0)} "
                    f"nodes still surfaced — tighten 'Walk breadth' in "
                    f"settings to narrow."
                ),
            }
    except Exception:
        logger.exception("master subgraph build failed; falling back per-repo")

    # ── 4b. Community retrieval (Stage 6) ─────────────────────────
    # Fetch top-K community summaries by centroid-cosine to the
    # question. Communities = Louvain-clustered entity groups with
    # LLM-generated "what this cluster DOES" summaries. They are the
    # retrieval unit for architectural/global questions where
    # vector-top-K over individual entities under-retrieves.
    community_hits: list[dict[str, object]] = []
    try:
        async with deps.session_factory() as session:
            community_hits = await search_communities(
                session,
                question,
                repo_ids=[str(r.id) for r in usable],
                k=5,
            )
        if community_hits:
            yield {
                "type": "communities",
                "count": len(community_hits),
                "names": [c["name"] for c in community_hits],
            }
            yield {
                "type": "status",
                "message": (
                    f"Matched {len(community_hits)} architectural "
                    f"communit{'ies' if len(community_hits) != 1 else 'y'}: "
                    + ", ".join(str(c["name"])[:40] for c in community_hits[:3])
                    + ("…" if len(community_hits) > 3 else "")
                ),
            }
            logger.info(
                "retrieved %d communities for synthesis",
                len(community_hits),
            )
    except Exception:
        logger.exception("community retrieval failed; synthesis will skip")

    flow_paths: list[FlowPath] = []
    if master_subgraph and master_subgraph.nodes:
        # Surface structural stats to the UI so users can see graph
        # coverage (and eventually select which edges are in play).
        yield {
            "type": "subgraph",
            "stats": master_subgraph.stats,
            "cross_repo_edge_count": master_subgraph.stats.get("cross_repo_edges", 0),
            "repo_footprint": sorted({n.repo_id for n in master_subgraph.nodes if n.repo_id}),
        }
        yield {
            "type": "status",
            "message": (
                f"Built query subgraph: "
                f"{master_subgraph.stats.get('total_nodes', 0)} entities, "
                f"{master_subgraph.stats.get('total_edges', 0)} edges "
                f"({master_subgraph.stats.get('cross_repo_edges', 0)} cross-repo)"
            ),
        }
        logger.info(
            "master subgraph: %d nodes, %d edges (%d cross-repo), footprint across %d repos",
            master_subgraph.stats.get("total_nodes", 0),
            master_subgraph.stats.get("total_edges", 0),
            master_subgraph.stats.get("cross_repo_edges", 0),
            len({n.repo_id for n in master_subgraph.nodes if n.repo_id}),
        )

        # Stage 5: pre-compute flow paths (DFS from seeds, ranked by
        # cross-repo-hop count). Synthesis uses these as the literal
        # skeleton of its "End-to-end flow" section.
        try:
            flow_paths = find_flow_paths(
                master_subgraph,
                max_paths=5,
                max_depth=4,
            )
            if flow_paths:
                yield {
                    "type": "flow_paths",
                    "count": len(flow_paths),
                    "cross_repo_paths": sum(1 for p in flow_paths if p.cross_repo_hops > 0),
                }
                logger.info(
                    "pre-computed %d flow paths (%d cross-repo)",
                    len(flow_paths),
                    sum(1 for p in flow_paths if p.cross_repo_hops > 0),
                )
        except Exception:
            logger.exception("flow-path computation failed; synthesis will improvise")

    # Which per-repo agent types are enabled for this turn. MAX_AGENTS_PER_REPO
    # caps the width. "code" is always run; the external-source agents only
    # run if we have the metadata to scope them.
    #
    # Per-conversation shard semaphore — enforced inside the
    # sharded code-agent dispatch path below. Inert when no repo
    # triggers sharding (agents_per_repo_max=1 across the board), so
    # the common case pays no overhead.
    shard_sem = asyncio.Semaphore(_SHARD_CONCURRENCY)

    # Build per-source "already-found evidence" hints once per turn —
    # they're identical across every repo bundle. Each per-source agent
    # receives prior_turns_text + its kind's hint, so e.g. the Jira
    # agent sees the Jira tickets prior turns already cited and is told
    # to find NEW evidence beyond them. This is the smoothness win the
    # user asked for: follow-ups gather more focused data instead of
    # re-finding the same files turn after turn.
    def _agent_prior_context(kind: str) -> str:
        base = prior_turns_text or ""
        extra = _format_prior_evidence(prior_bundle, kind_filter=kind) if prior_bundle else ""
        if not extra:
            return base
        return (base + "\n\n" + extra) if base else extra

    prior_turns_jira = _agent_prior_context("jira")
    prior_turns_confluence = _agent_prior_context("confluence")
    prior_turns_github = _agent_prior_context("github")
    prior_turns_slack = _agent_prior_context("slack")
    prior_turns_gdocs = _agent_prior_context("gdocs")
    # The repo (code) agent gets the full evidence picture across all
    # kinds — its synthesis spans sources.
    prior_turns_repo = _agent_prior_context("code") if prior_bundle else (prior_turns_text or "")

    async def _build_bundle(rk: RepoRanking) -> RepoBundle:
        r = repo_by_id.get(rk.repo_id)
        if r is None:
            return RepoBundle(repo_id=rk.repo_id, repo_name=rk.repo_name)
        fr = fresh_by_id[rk.repo_id]
        own = ownership_by_id.get(rk.repo_id)

        # GitHub and Slack are opt-in (default off) — low citation rate
        # and high added latency per query. Enable via
        # research_include_github / research_include_slack in chat settings.
        #
        # Exception: when the question was enriched with a Jira ticket chain
        # (had_jira_chain=True), automatically enable GitHub for high-relevance
        # repos so the research agent can find reference PRs (e.g. prior
        # tenant-plan implementations) without the user having to toggle a setting.
        github_enabled = "github.com" in (r.url or "").lower() and (
            getattr(chat_settings, "research_include_github", False)
            or (had_jira_chain and rk.relevance == "high")  # auto-enable for Jira research
        )
        slack_enabled = bool(own and own.slack_channel) and getattr(
            chat_settings, "research_include_slack", False
        )

        # Emit per-agent start events so the UI renders the 2D grid upfront.
        await event_hook(
            {"type": "agent_start", "repo_name": r.name, "agent": "code"},
        )
        if own and own.jira_project_key:
            await event_hook(
                {"type": "agent_start", "repo_name": r.name, "agent": "jira"},
            )
        if (
            own
            and own.confluence_space_key
            and getattr(chat_settings, "research_include_confluence", False)
        ):
            await event_hook(
                {"type": "agent_start", "repo_name": r.name, "agent": "confluence"},
            )
        if github_enabled:
            await event_hook(
                {"type": "agent_start", "repo_name": r.name, "agent": "github"},
            )
        if slack_enabled:
            await event_hook(
                {"type": "agent_start", "repo_name": r.name, "agent": "slack"},
            )
        # gdocs always fires — keyword-fallback works without a folder URL
        await event_hook(
            {"type": "agent_start", "repo_name": r.name, "agent": "gdocs"},
        )

        # Entity snippets + cached external content fetched in one session
        cached_jira: list[dict] = []
        cached_conf: list[dict] = []
        repo_slice = None
        async with deps.session_factory() as s:
            # Prefer the master subgraph slice (Stage 3): the agent sees
            # its own in-repo nodes PLUS boundary nodes (entities in
            # other repos that connect to this one) — real cross-repo
            # context, not just a repo-local view.
            if master_subgraph and master_subgraph.nodes:
                repo_slice = extract_repo_slice(master_subgraph, rk.repo_id)
                if repo_slice.nodes or repo_slice.boundary:
                    entities = repo_slice.to_snippets()
                else:
                    # Repo has no subgraph footprint — fall back to
                    # local retrieval so the agent still has something
                    # to work with.
                    entities = await _fetch_entity_snippets_for_repo(
                        s,
                        r.id,
                        question=question,
                        question_keywords=keywords,
                    )
            else:
                entities = await _fetch_entity_snippets_for_repo(
                    s,
                    r.id,
                    question=question,
                    question_keywords=keywords,
                )
            # Pre-indexed Jira issues — chat feeds these to the agent so
            # it doesn't have to round-trip to Atlassian every turn.
            jira_rows = (
                (
                    await s.execute(
                        __import__("sqlalchemy")
                        .select(JiraIssue)
                        .where(JiraIssue.repo_id == r.id)
                        .order_by(JiraIssue.updated_at.desc().nulls_last())
                        .limit(200),
                    )
                )
                .scalars()
                .all()
            )
            cached_jira = [
                {
                    "issue_key": j.issue_key,
                    "title": j.title,
                    "url": j.url,
                    "status": j.status,
                    "issue_type": j.issue_type,
                    "summary": j.summary or "",
                    "description_excerpt": j.description_excerpt or "",
                    "labels": j.labels or [],
                    "components": j.components or [],
                }
                for j in jira_rows
            ]
            conf_rows = (
                (
                    await s.execute(
                        __import__("sqlalchemy")
                        .select(ConfluencePage)
                        .where(ConfluencePage.repo_id == r.id)
                        .order_by(ConfluencePage.last_modified.desc().nulls_last())
                        .limit(200),
                    )
                )
                .scalars()
                .all()
            )
            cached_conf = [
                {
                    "title": p.title,
                    "url": p.url,
                    "summary": p.summary or "",
                    "body_excerpt": p.body_excerpt or "",
                    "labels": p.labels or [],
                }
                for p in conf_rows
            ]

        # All per-repo agents fire in parallel — they share the CLI session
        # but do fundamentally different things (filesystem vs MCP).
        # Split the snippet list back into in-repo vs boundary so the
        # agent's prompt can frame boundary entities as "other repos —
        # don't try to Grep them here".
        in_repo_snippets = entities
        boundary_snippets: list[EntitySnippet] = []
        if repo_slice is not None:
            in_repo_snippets = repo_slice.to_snippets()[: len(repo_slice.nodes)]
            # Everything beyond the in-repo count is boundary.
            all_snips = repo_slice.to_snippets()
            boundary_snippets = all_snips[len(repo_slice.nodes) :]

        # ── Chat-research learning loop (read path) ─────────────────────
        # Build a per-kind "Known facts about <repo>" block from the
        # entity_relationships graph (the knowledge store).
        # Each per-source agent's prior_turns gets prepended with the
        # block tuned to its predicate scope so it verifies-or-extends
        # instead of re-discovering edges already in the graph.
        from bifrost.research.graph_facts import render_repo_facts_block_for_kind

        repo_facts_blocks: dict[str, str] = {}
        for _kind in ("code", "jira", "confluence", "github", "slack", "gdocs"):
            try:
                repo_facts_blocks[_kind] = await render_repo_facts_block_for_kind(
                    session_factory=deps.session_factory,
                    repo_id=r.id,
                    repo_name=r.name,
                    kind=_kind,
                    limit=20,
                )
            except Exception:  # pragma: no cover
                logger.exception(
                    "graph_facts load failed for repo=%s kind=%s; falling back to no-facts block",
                    r.name,
                    _kind,
                )
                repo_facts_blocks[_kind] = ""

        def _splice_facts(base: str, kind: str) -> str:
            """Prepend the kind's facts block (if any) to ``base``."""
            block = repo_facts_blocks.get(kind, "")
            if not block:
                return base or ""
            return (block + "\n\n" + base) if base else block

        prior_turns_jira_full = _splice_facts(prior_turns_jira, "jira")
        prior_turns_confluence_full = _splice_facts(prior_turns_confluence, "confluence")
        prior_turns_github_full = _splice_facts(prior_turns_github, "github")
        prior_turns_slack_full = _splice_facts(prior_turns_slack, "slack")
        prior_turns_gdocs_full = _splice_facts(prior_turns_gdocs, "gdocs")
        prior_turns_repo_full = _splice_facts(prior_turns_repo, "code")

        # ── multi-code-agent sharding ───────────────────────────────
        # When the user dialed ``agents_per_repo_max > 1`` AND this
        # repo has enough entities to make sharding worthwhile, split
        # the in-repo entity slice into N shards and spawn one code
        # agent per shard. Each shard's prompt names its scope so it
        # focuses there and doesn't redo work other shards are doing.
        # Shard reports merge into a single RepoReport before
        # synthesis sees them — the rest of the pipeline is shard-
        # agnostic.
        community_label_by_urn: dict[str, str] = {}
        if chat_settings.agents_per_repo_max > 1 and len(in_repo_snippets) > 0:
            try:
                async with deps.session_factory() as s2:
                    community_label_by_urn = await _load_community_labels_for_repo(
                        s2,
                        rk.repo_id,
                        in_repo_snippets,
                    )
            except Exception:
                logger.exception(
                    "community-label load failed for %s — falling back "
                    "to entity-count balanced sharding",
                    r.name,
                )

        shards = shard_repo_for_code_agents(
            entities=in_repo_snippets,
            agents_per_repo_max=chat_settings.agents_per_repo_max,
            community_label_by_urn=community_label_by_urn or None,
        )

        if shards:
            # Pre-emit per-shard ``agent_start`` so the UI can render
            # one chip per shard under the 2D grid's "code" cell.
            for sh in shards:
                await event_hook(
                    {
                        "type": "agent_start",
                        "repo_name": r.name,
                        "agent": "code",
                        "shard_index": sh.shard_index,
                        "total_shards": sh.total_shards,
                        "community_label": sh.community_label,
                    },
                )
            urn_to_snippet = {e.urn: e for e in in_repo_snippets}

            # Bound shard subprocess concurrency at the
            # conversation level. ``shard_sem`` is shared across
            # every repo bundle in this turn, so even with N repos
            # × M shards the total in-flight `claude -p` processes
            # never exceed ``_SHARD_CONCURRENCY``.
            async def _shard_task(sh: CodeShard) -> RepoReport:
                shard_entities = [urn_to_snippet[u] for u in sh.entity_urns if u in urn_to_snippet]

                async def _fresh_shard() -> RepoReport:
                    async with shard_sem:
                        return await research_repo(
                            question=question,
                            repo_id=rk.repo_id,
                            repo_name=r.name,
                            clone_path=fr.clone_path,
                            entities=shard_entities,
                            boundary_entities=boundary_snippets or None,
                            metadata_stale=fr.metadata_stale,
                            prior_turns=prior_turns_repo_full,
                            settings=settings,
                            shard=sh,
                        )

                if research_run_id is None or turn_id is None:
                    return await _fresh_shard()

                shard_scope_hash = compute_scope_hash(
                    [
                        "repo_shard",
                        question,
                        rk.repo_id,
                        sh.shard_index,
                        sh.total_shards,
                        sorted(sh.entity_urns),
                    ]
                )
                report, _hit = await cached_agent(
                    session_factory=deps.session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind="repo",
                    target_id=rk.repo_id,
                    shard_index=sh.shard_index,
                    scope_hash=shard_scope_hash,
                    runner=_fresh_shard,
                    serialize=lambda r: r.to_dict(),
                    deserialize=RepoReport.from_dict,
                    extract_cost=cost_from_usage,
                    question_embedding=question_embedding,
                    repo_commit_sha=fr.local_head or None,
                )
                return report

            shard_tasks: list[asyncio.Task[RepoReport]] = [
                asyncio.create_task(_shard_task(sh)) for sh in shards
            ]

            async def _gather_and_merge(
                tasks: list[asyncio.Task[RepoReport]],
                shard_list: list[CodeShard],
            ) -> RepoReport:
                shard_reports = await asyncio.gather(
                    *tasks,
                    return_exceptions=False,
                )
                # Per-shard agent_done so the UI's chip-row can update
                # progressively. The legacy single ``agent_done: code``
                # below will fire after this whole task resolves with
                # the merged report.
                for sh, sr in zip(shard_list, shard_reports, strict=False):
                    await event_hook(
                        {
                            "type": "agent_done",
                            "repo_name": r.name,
                            "agent": "code",
                            "shard_index": sh.shard_index,
                            "total_shards": sh.total_shards,
                            "community_label": sh.community_label,
                            "relevance": sr.relevance,
                            "files": len(sr.key_files),
                            "error": sr.error,
                        },
                    )
                return merge_repo_reports(shard_reports)

            code_task = asyncio.create_task(
                _gather_and_merge(shard_tasks, shards),
            )
        else:

            async def _run_code_with_cache(
                _repo_id: str = rk.repo_id,
                _repo_name: str = r.name,
                _clone_path: str = fr.clone_path,
                _entities: list[Any] = in_repo_snippets,
                _boundary: list[Any] | None = boundary_snippets or None,
                _stale: bool = fr.metadata_stale,
                _commit: str = fr.local_head or "",
            ) -> RepoReport:
                async def _fresh() -> RepoReport:
                    return await research_repo(
                        question=question,
                        repo_id=_repo_id,
                        repo_name=_repo_name,
                        clone_path=_clone_path,
                        entities=_entities,
                        boundary_entities=_boundary,
                        metadata_stale=_stale,
                        prior_turns=prior_turns_repo_full,
                        settings=settings,
                    )

                if research_run_id is None or turn_id is None:
                    return await _fresh()
                code_scope_hash = compute_scope_hash(
                    [
                        "repo_full",
                        question,
                        _repo_id,
                    ]
                )
                report, _hit = await cached_agent(
                    session_factory=deps.session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind="repo",
                    target_id=_repo_id,
                    shard_index=0,
                    scope_hash=code_scope_hash,
                    runner=_fresh,
                    serialize=lambda r: r.to_dict(),
                    deserialize=RepoReport.from_dict,
                    extract_cost=cost_from_usage,
                    question_embedding=question_embedding,
                    repo_commit_sha=_commit or None,
                )
                return report

            code_task = asyncio.create_task(_run_code_with_cache())

        # Each external agent runs under ``_tracked_external_agent``
        # so the per-agent cancel endpoint can stop one Jira /
        # Confluence / GitHub / Slack agent without touching the
        # others. On cancel, the wrapper returns a synthetic report
        # marked ``error="cancelled by user"``; the merge + synthesis
        # path continues with whatever the still-running agents
        # produced.
        conv_id_str = str(conversation_id)
        jira_task: asyncio.Task[JiraReport] | None = None
        if own and own.jira_project_key:
            jira_scope_hash = compute_scope_hash(
                [
                    "jira",
                    question,
                    rk.repo_id,
                    own.jira_project_key,
                    own.squad,
                ]
            )

            async def _run_jira_with_cache(
                _jpk: str = own.jira_project_key,
                _squad: Any = own.squad,
                _scope_hash: str = jira_scope_hash,
                _repo_id: str = rk.repo_id,
                _repo_name: str = r.name,
            ) -> JiraReport:
                async def _fresh() -> JiraReport:
                    return await research_jira_for_repo(
                        question=question,
                        repo_id=_repo_id,
                        repo_name=_repo_name,
                        jira_project_key=_jpk,
                        squad=_squad,
                        prior_turns=prior_turns_jira_full,
                        settings=settings,
                        cached_issues=cached_jira,
                    )

                if research_run_id is None or turn_id is None:
                    return await _fresh()
                report, _hit = await cached_agent(
                    session_factory=deps.session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind="jira",
                    target_id=_repo_id,
                    shard_index=0,
                    scope_hash=_scope_hash,
                    runner=_fresh,
                    serialize=lambda r: r.to_dict(),
                    deserialize=JiraReport.from_dict,
                    extract_cost=cost_from_usage,
                    question_embedding=question_embedding,
                )
                return report

            jira_task = asyncio.create_task(
                _tracked_external_agent(
                    conv_id=conv_id_str,
                    repo_id=rk.repo_id,
                    source="jira",
                    coro=_run_jira_with_cache(),
                    cancelled_report=JiraReport(
                        repo_id=rk.repo_id,
                        repo_name=r.name,
                        jira_project_key=own.jira_project_key,
                        relevance="none",
                        summary="(cancelled by user)",
                        error="cancelled by user",
                    ),
                ),
            )

        confluence_task: asyncio.Task[ConfluenceReport] | None = None
        # Per-repo Confluence is opt-in (default off) — the org-wide
        # ExternalAgent already covers Confluence search, so per-repo adds
        # redundant overlap. Enable via research_include_confluence.
        if (
            own
            and own.confluence_space_key
            and getattr(chat_settings, "research_include_confluence", False)
        ):
            conf_scope_hash = compute_scope_hash(
                [
                    "confluence",
                    question,
                    rk.repo_id,
                    own.confluence_space_key,
                    own.squad,
                ]
            )

            async def _run_conf_with_cache(
                _csk: str = own.confluence_space_key,
                _squad: Any = own.squad,
                _scope_hash: str = conf_scope_hash,
                _repo_id: str = rk.repo_id,
                _repo_name: str = r.name,
            ) -> ConfluenceReport:
                async def _fresh() -> ConfluenceReport:
                    return await research_confluence_for_repo(
                        question=question,
                        repo_id=_repo_id,
                        repo_name=_repo_name,
                        confluence_space_key=_csk,
                        squad=_squad,
                        prior_turns=prior_turns_confluence_full,
                        settings=settings,
                        cached_pages=cached_conf,
                    )

                if research_run_id is None or turn_id is None:
                    return await _fresh()
                report, _hit = await cached_agent(
                    session_factory=deps.session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind="confluence",
                    target_id=_repo_id,
                    shard_index=0,
                    scope_hash=_scope_hash,
                    runner=_fresh,
                    serialize=lambda r: r.to_dict(),
                    deserialize=ConfluenceReport.from_dict,
                    extract_cost=cost_from_usage,
                    question_embedding=question_embedding,
                )
                return report

            confluence_task = asyncio.create_task(
                _tracked_external_agent(
                    conv_id=conv_id_str,
                    repo_id=rk.repo_id,
                    source="confluence",
                    coro=_run_conf_with_cache(),
                    cancelled_report=ConfluenceReport(
                        repo_id=rk.repo_id,
                        repo_name=r.name,
                        confluence_space_key=own.confluence_space_key,
                        relevance="none",
                        summary="(cancelled by user)",
                        error="cancelled by user",
                    ),
                ),
            )

        github_task: asyncio.Task[GitHubReport] | None = None
        if github_enabled:
            github_scope_hash = compute_scope_hash(
                [
                    "github",
                    question,
                    rk.repo_id,
                    r.url,
                ]
            )

            async def _run_github_with_cache(
                _scope_hash: str = github_scope_hash,
                _repo_id: str = rk.repo_id,
                _repo_name: str = r.name,
                _repo_url: str | None = r.url,
            ) -> GitHubReport:
                async def _fresh() -> GitHubReport:
                    return await research_github_for_repo(
                        question=question,
                        repo_id=_repo_id,
                        repo_name=_repo_name,
                        repo_url=_repo_url,
                        prior_turns=prior_turns_github_full,
                        settings=settings,
                    )

                if research_run_id is None or turn_id is None:
                    return await _fresh()
                report, _hit = await cached_agent(
                    session_factory=deps.session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind="github",
                    target_id=_repo_id,
                    shard_index=0,
                    scope_hash=_scope_hash,
                    runner=_fresh,
                    serialize=lambda r: r.to_dict(),
                    deserialize=GitHubReport.from_dict,
                    extract_cost=cost_from_usage,
                    question_embedding=question_embedding,
                )
                return report

            github_task = asyncio.create_task(
                _tracked_external_agent(
                    conv_id=conv_id_str,
                    repo_id=rk.repo_id,
                    source="github",
                    coro=_run_github_with_cache(),
                    cancelled_report=GitHubReport(
                        repo_id=rk.repo_id,
                        repo_name=r.name,
                        github_owner=None,
                        github_name=None,
                        relevance="none",
                        summary="(cancelled by user)",
                        error="cancelled by user",
                    ),
                ),
            )

        slack_task: asyncio.Task[SlackReport] | None = None
        if slack_enabled:
            slack_scope_hash = compute_scope_hash(
                [
                    "slack",
                    question,
                    rk.repo_id,
                    own.slack_channel if own else None,
                    own.squad if own else None,
                ]
            )

            async def _run_slack_with_cache(
                _scope_hash: str = slack_scope_hash,
                _repo_id: str = rk.repo_id,
                _repo_name: str = r.name,
                _channel: str | None = own.slack_channel if own else None,
                _squad: Any = own.squad if own else None,
            ) -> SlackReport:
                async def _fresh() -> SlackReport:
                    return await research_slack_for_repo(
                        question=question,
                        repo_id=_repo_id,
                        repo_name=_repo_name,
                        slack_channel=_channel,
                        squad=_squad,
                        prior_turns=prior_turns_slack_full,
                        settings=settings,
                    )

                if research_run_id is None or turn_id is None:
                    return await _fresh()
                report, _hit = await cached_agent(
                    session_factory=deps.session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind="slack",
                    target_id=_repo_id,
                    shard_index=0,
                    scope_hash=_scope_hash,
                    runner=_fresh,
                    serialize=lambda r: r.to_dict(),
                    deserialize=SlackReport.from_dict,
                    extract_cost=cost_from_usage,
                    question_embedding=question_embedding,
                )
                return report

            slack_task = asyncio.create_task(
                _tracked_external_agent(
                    conv_id=conv_id_str,
                    repo_id=rk.repo_id,
                    source="slack",
                    coro=_run_slack_with_cache(),
                    cancelled_report=SlackReport(
                        repo_id=rk.repo_id,
                        repo_name=r.name,
                        slack_channel=own.slack_channel if own else None,
                        relevance="none",
                        summary="(cancelled by user)",
                        error="cancelled by user",
                    ),
                ),
            )

        # ── Google Drive / Docs (6th per-source agent) ──────────────────
        # Mirrors confluence: scoped via RepoOwnership.gdoc_folder_url
        # when set, falls back to repo-name keyword scope otherwise.
        # The agent is ALWAYS attempted (no enable flag), because the
        # absence of a folder_url isn't disqualifying — older orgs have
        # design docs scattered across My Drive / shared drives that
        # the keyword fallback can still find.
        gdocs_task: asyncio.Task[GDocsReport] | None = None
        gdoc_folder_url = getattr(own, "gdoc_folder_url", None) if own else None
        gdocs_scope_hash = compute_scope_hash(
            [
                "gdocs",
                question,
                rk.repo_id,
                gdoc_folder_url,
                r.name,
            ]
        )

        async def _run_gdocs_with_cache(
            _scope_hash: str = gdocs_scope_hash,
            _repo_id: str = rk.repo_id,
            _repo_name: str = r.name,
            _folder: str | None = gdoc_folder_url,
            _squad: Any = own.squad if own else None,
        ) -> GDocsReport:
            async def _fresh() -> GDocsReport:
                return await research_gdocs_for_repo(
                    question=question,
                    repo_id=_repo_id,
                    repo_name=_repo_name,
                    gdoc_folder_url=_folder,
                    squad=_squad,
                    prior_turns=prior_turns_gdocs_full,
                    settings=settings,
                )

            if research_run_id is None or turn_id is None:
                return await _fresh()
            report, _hit = await cached_agent(
                session_factory=deps.session_factory,
                research_run_id=research_run_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                agent_kind="gdocs",
                target_id=_repo_id,
                shard_index=0,
                scope_hash=_scope_hash,
                runner=_fresh,
                serialize=lambda r: r.to_dict(),
                deserialize=GDocsReport.from_dict,
                extract_cost=cost_from_usage,
                question_embedding=question_embedding,
            )
            return report

        gdocs_task = asyncio.create_task(
            _tracked_external_agent(
                conv_id=conv_id_str,
                repo_id=rk.repo_id,
                source="gdocs",
                coro=_run_gdocs_with_cache(),
                cancelled_report=GDocsReport(
                    repo_id=rk.repo_id,
                    repo_name=r.name,
                    gdoc_folder_url=gdoc_folder_url,
                    relevance="none",
                    summary="(cancelled by user)",
                    error="cancelled by user",
                ),
            ),
        )

        code_report = await code_task
        await event_hook(
            {
                "type": "agent_done",
                "repo_name": r.name,
                "agent": "code",
                "relevance": code_report.relevance,
                "files": len(code_report.key_files),
                "error": code_report.error,
            },
        )

        # Soft deadline for external agents: 180s after code finished.
        # Without this, a single hung MCP call (rate-limited Slack
        # search, paginating Confluence query) could keep the bundle
        # blocked indefinitely. Past the deadline we cancel whatever's
        # still running, mark it skipped, and move synthesis along
        # with the evidence we have. The per-subprocess timeouts
        # (120-240s in the agents) are the lower bound; this is the
        # bundle-level upper bound.
        _ext_pairs: list[tuple[str, asyncio.Task[Any] | None, Any]] = [
            (
                "jira",
                jira_task,
                JiraReport(
                    repo_id=rk.repo_id,
                    repo_name=r.name,
                    jira_project_key=own.jira_project_key if own else None,
                    relevance="none",
                    summary="(skipped — slow agent timed out)",
                    error="bundle deadline",
                )
                if own
                else None,
            ),
            (
                "confluence",
                confluence_task,
                ConfluenceReport(
                    repo_id=rk.repo_id,
                    repo_name=r.name,
                    confluence_space_key=own.confluence_space_key if own else None,
                    relevance="none",
                    summary="(skipped — slow agent timed out)",
                    error="bundle deadline",
                )
                if own
                else None,
            ),
            (
                "github",
                github_task,
                GitHubReport(
                    repo_id=rk.repo_id,
                    repo_name=r.name,
                    github_owner=None,
                    github_name=None,
                    relevance="none",
                    summary="(skipped — slow agent timed out)",
                    error="bundle deadline",
                ),
            ),
            (
                "slack",
                slack_task,
                SlackReport(
                    repo_id=rk.repo_id,
                    repo_name=r.name,
                    slack_channel=own.slack_channel if own else None,
                    relevance="none",
                    summary="(skipped — slow agent timed out)",
                    error="bundle deadline",
                ),
            ),
            (
                "gdocs",
                gdocs_task,
                GDocsReport(
                    repo_id=rk.repo_id,
                    repo_name=r.name,
                    gdoc_folder_url=gdoc_folder_url,
                    relevance="none",
                    summary="(skipped — slow agent timed out)",
                    error="bundle deadline",
                ),
            ),
        ]
        live_ext = [(k, t) for k, t, _ in _ext_pairs if t is not None]
        live_tasks = [t for _, t in live_ext]
        if live_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*live_tasks, return_exceptions=True),
                    timeout=180.0,
                )
            except TimeoutError:
                # Cancel whatever's still pending so the subprocess
                # gets killed and doesn't keep streaming tokens we'll
                # never consume.
                for _, t in live_ext:
                    if not t.done():
                        t.cancel()
                # Brief grace period for cancellation to settle.
                await asyncio.gather(*live_tasks, return_exceptions=True)

        def _harvest(task: asyncio.Task[Any] | None, fallback: Any) -> Any:
            if task is None:
                return None
            if not task.done():
                return fallback
            try:
                return task.result()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                return fallback

        jira_report = _harvest(jira_task, _ext_pairs[0][2])
        confluence_report = _harvest(confluence_task, _ext_pairs[1][2])
        github_report = _harvest(github_task, _ext_pairs[2][2])
        slack_report = _harvest(slack_task, _ext_pairs[3][2])
        gdocs_report = _harvest(gdocs_task, _ext_pairs[4][2])

        if jira_report is not None:
            await event_hook(
                {
                    "type": "agent_done",
                    "repo_name": r.name,
                    "agent": "jira",
                    "relevance": jira_report.relevance,
                    "hits": len(jira_report.issues),
                    "error": jira_report.error,
                }
            )
        if confluence_report is not None:
            await event_hook(
                {
                    "type": "agent_done",
                    "repo_name": r.name,
                    "agent": "confluence",
                    "relevance": confluence_report.relevance,
                    "hits": len(confluence_report.pages),
                    "error": confluence_report.error,
                }
            )
        if github_report is not None:
            await event_hook(
                {
                    "type": "agent_done",
                    "repo_name": r.name,
                    "agent": "github",
                    "relevance": github_report.relevance,
                    "hits": len(github_report.recent_prs) + len(github_report.relevant_issues),
                    "error": github_report.error,
                }
            )
        if slack_report is not None:
            await event_hook(
                {
                    "type": "agent_done",
                    "repo_name": r.name,
                    "agent": "slack",
                    "relevance": slack_report.relevance,
                    "hits": len(slack_report.hits),
                    "error": slack_report.error,
                }
            )
        if gdocs_report is not None:
            await event_hook(
                {
                    "type": "agent_done",
                    "repo_name": r.name,
                    "agent": "gdocs",
                    "relevance": gdocs_report.relevance,
                    "hits": len(gdocs_report.docs),
                    "error": gdocs_report.error,
                }
            )

        return RepoBundle(
            repo_id=rk.repo_id,
            repo_name=r.name,
            code=code_report,
            jira=jira_report,
            confluence=confluence_report,
            github=github_report,
            slack=slack_report,
            gdocs=gdocs_report,
            entities=entities,
        )

    # Emit initial roster so UI shows the 2D grid immediately.
    for rk in rankings:
        r = repo_by_id.get(rk.repo_id)
        if r is None:
            continue
        own = ownership_by_id.get(rk.repo_id)
        has_jira = bool(own and own.jira_project_key)
        has_conf = bool(own and own.confluence_space_key) and getattr(
            chat_settings, "research_include_confluence", False
        )
        has_github = "github.com" in (r.url or "").lower() and getattr(
            chat_settings, "research_include_github", False
        )
        has_slack = bool(own and own.slack_channel) and getattr(
            chat_settings, "research_include_slack", False
        )
        yield {
            "type": "repo_roster",
            "repo_id": rk.repo_id,
            "repo_name": r.name,
            "squad": own.squad if own else None,
            "relevance": rk.relevance,
            "is_primary": bool(getattr(r, "is_primary", False)),
            "agents": {
                "code": True,
                "jira": has_jira,
                "confluence": has_conf,
                "github": has_github,
                "slack": has_slack,
            },
        }

    # Event-hook lets `_build_bundle` emit events back through this generator.
    # Because Python generators can't easily accept callbacks, we use an
    # asyncio.Queue and drain it interleaved with awaiting the bundle tasks.
    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_hook(evt: dict[str, Any]) -> None:
        await event_queue.put(evt)

    async def _run_all_bundles() -> list[RepoBundle]:
        # Cross-repo parallelism is bounded by ``_FANOUT_CONCURRENCY``
        # so we don't spawn hundreds of `claude -p` subprocesses at
        # once. The router's top-K (now from ``walk_breadth_soft_cap``)
        # may discover more repos than this — they queue and run in
        # waves under the semaphore.
        sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)

        async def _bounded(rk: RepoRanking) -> RepoBundle:
            async with sem:
                return await _build_bundle(rk)

        try:
            return await asyncio.gather(*(_bounded(rk) for rk in rankings))
        finally:
            await event_queue.put(None)  # signal end of events

    # Report how many agents we're about to fan out so the UI can
    # render "3 code agents + 2 Jira agents + … working in parallel".
    _include_confluence = getattr(chat_settings, "research_include_confluence", False)
    _include_github = getattr(chat_settings, "research_include_github", False)
    _include_slack = getattr(chat_settings, "research_include_slack", False)
    total_agents = sum(
        1  # code is always on
        + (
            1
            if ownership_by_id.get(rk.repo_id) and ownership_by_id[rk.repo_id].jira_project_key
            else 0
        )
        + (
            1
            if _include_confluence
            and ownership_by_id.get(rk.repo_id)
            and ownership_by_id[rk.repo_id].confluence_space_key
            else 0
        )
        + (
            1
            if _include_github
            and "github.com"
            in (
                (repo_by_id.get(rk.repo_id).url or "").lower() if repo_by_id.get(rk.repo_id) else ""
            )
            else 0
        )
        + (
            1
            if _include_slack
            and ownership_by_id.get(rk.repo_id)
            and ownership_by_id[rk.repo_id].slack_channel
            else 0
        )
        for rk in rankings
    )
    if rankings:
        yield {
            "type": "status",
            "message": (
                f"Fanning out {total_agents} agent(s) across "
                f"{len(rankings)} repo(s) (code + Jira + Confluence + GitHub + Slack)…"
            ),
        }
    else:
        # Common case when routed repos haven't been deep-indexed yet —
        # don't render "0 agents across 0 repos", explain the fallback.
        yield {
            "type": "status",
            "message": (
                "Researching via org-wide Confluence + Jira only "
                "(selected repos have no deep-index yet — extract them "
                "first to include per-repo code/Jira/Slack agents)."
            ),
        }

    bundles_task = asyncio.create_task(_run_all_bundles())

    # Fire the external (global) agent in parallel with the bundles.
    # Its purpose is cross-cutting docs that aren't tied to one repo
    # (e.g., an org-wide "How we integrate carriers" page). Per-repo
    # Jira/Confluence agents cover the repo-specific cases.
    yield {"type": "external_start", "label": "Org-wide Confluence/Jira"}
    external_task = asyncio.create_task(
        research_external(question=question, settings=settings),
    )

    # Drain event_queue → yield to UI until bundles finish.
    while True:
        evt = await event_queue.get()
        if evt is None:
            break
        yield evt

    bundles: list[RepoBundle] = await bundles_task
    external: ExternalFindings = await external_task

    # Aggregate usage across all bundles + external + (already-added router)
    for b in bundles:
        if b.code and b.code.usage:
            agg.add(b.code.usage)
        if b.jira and b.jira.usage:
            agg.add(b.jira.usage)
        if b.confluence and b.confluence.usage:
            agg.add(b.confluence.usage)
        if b.github and b.github.usage:
            agg.add(b.github.usage)
        if b.slack and b.slack.usage:
            agg.add(b.slack.usage)

    # Back-compat: emit the old subagent_done events so the existing UI
    # summary box still works while we finish the 2D grid rendering.
    for b in bundles:
        if b.code:
            yield {
                "type": "subagent_done",
                "repo_name": b.repo_name,
                "relevance": b.code.relevance,
                "key_files_count": len(b.code.key_files),
                "error": b.code.error,
            }

    agg.add(external.usage)
    yield {
        "type": "external_done",
        "findings_count": len(external.findings),
        "searched_sources": external.searched_sources,
        "error": external.error,
    }

    # Keep a bundle if ANY of its agents produced positive signal
    # (relevance high/medium/low). Previously we required code to
    # succeed, but Jira/Confluence/GitHub signal alone is often
    # enough (e.g., "recent PRs for carrier-gateway" doesn't need a
    # deep code walk).
    _POS_RELEVANCE = ("high", "medium", "low")

    def _bundle_has_positive_signal(b: RepoBundle) -> bool:
        if b.code and not b.code.error and b.code.relevance in _POS_RELEVANCE:
            return True
        if b.jira and not b.jira.error and b.jira.relevance in _POS_RELEVANCE:
            return True
        if b.confluence and not b.confluence.error and b.confluence.relevance in _POS_RELEVANCE:
            return True
        if b.github and not b.github.error and b.github.relevance in _POS_RELEVANCE:
            return True
        if b.slack and not b.slack.error and b.slack.relevance in _POS_RELEVANCE:
            return True
        return bool(b.gdocs and not b.gdocs.error and b.gdocs.relevance in _POS_RELEVANCE)

    def _bundle_ran_successfully(b: RepoBundle) -> bool:
        """At least one agent ran without erroring out — even if it
        returned ``relevance="none"``. A clean ``none`` is a valid
        finding ("I looked, nothing matched") and the synthesizer
        can still describe the repo's manifest metadata."""
        if b.code and not b.code.error:
            return True
        if b.jira and not b.jira.error:
            return True
        if b.confluence and not b.confluence.error:
            return True
        if b.github and not b.github.error:
            return True
        if b.slack and not b.slack.error:
            return True
        return bool(b.gdocs and not b.gdocs.error)

    useful_bundles = [b for b in bundles if _bundle_has_positive_signal(b)]

    # With no positive signal but some clean runs, fall back to the
    # "ran successfully" set so synthesis can still give a partial
    # answer; the entity graph is often sparse for newly extracted teams.
    soft_signal = False
    if not useful_bundles:
        useful_bundles = [b for b in bundles if _bundle_ran_successfully(b)]
        soft_signal = bool(useful_bundles)

    if not useful_bundles:
        # Persist a minimal bundle even though synthesis can't run, so the
        # next turn can inherit this scope instead of padding with
        # unrelated repos.
        try:
            partial_bundle = ResearchBundle(
                conversation_id=str(conversation_id),
                last_question=question,
                last_answer="",  # synthesis didn't run
                confidence="low",
                repos_used=[str(r.id) for r in usable],
                repos_used_names=[r.name for r in usable],
                scope_rationale=(
                    "Partial bundle — all per-repo agents returned "
                    "irrelevant or failed. Scope captured for "
                    "follow-up continuity."
                ),
                subgraph_entity_urns=[],
                sources=[],
                last_question_embedding=question_embedding,
            )
            await _persist_research_bundle(
                session_factory=deps.session_factory,
                conversation_id=conversation_id,
                bundle=partial_bundle,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "partial bundle persist failed (non-fatal)",
            )
        yield {
            "type": "error",
            "error": "All per-repo agents failed or returned irrelevant results.",
        }
        return

    if soft_signal:
        # Tell the user up front that the answer's going to be thin.
        yield {
            "type": "status",
            "message": (
                f"All {len(useful_bundles)} per-repo agents ran but "
                "none returned strong signal — answering from manifest "
                "metadata + community summaries. Re-extract these "
                "repos for a deeper answer."
            ),
        }

    # ── 5. Synthesis ────────────────────────────────────────────────
    yield {"type": "status", "message": "Synthesising final answer…"}

    # Await the prior-subgraph hydration (started in §1e, has been
    # running concurrently with freshness + fan-out). Failure here is
    # non-fatal: synthesis still has the live fan-out + the prior
    # bundle metadata block, just without the structured prior
    # evidence section.
    prior_subgraph, hydration_event = await _await_prior_hydration(hydration_task)
    if hydration_event is not None:
        yield hydration_event

    synth_user = _build_synthesis_user_message(
        question,
        useful_bundles,
        external,
        prior_turns=prior_turns_text,
        flow_paths=flow_paths,
        community_hits=community_hits,
        prior_bundle=prior_bundle,
        prior_subgraph=prior_subgraph,
    )
    if had_jira_chain:
        synth_user += (
            "\n\n## Devil's advocate (required when Jira tickets are in scope)\n"
            "After your main answer, add a ## Devil's advocate section with:\n"
            "1. Is there a redundant in-flight ticket already solving this? Name it.\n"
            "2. Is the primary blocker technical or a dependency outside the code (a decision, a review, another team)? Be direct.\n"
            "3. Realistic business impact range \u2014 not just the optimistic estimate.\n"
            "4. If you had to argue AGAINST building this, what is the strongest case?\n"
            "Keep this section to 4 bullets max."
        )
    client = ClaudeLlmClient(settings)

    final_text_parts: list[str] = []
    try:
        synthesis_stream = client.stream_text(
            system_prompt=_SYNTHESIS_SYSTEM_PROMPT,
            user_message=synth_user,
            model=settings.reasoning_model,  # premium — produces the final answer
            max_turns=1,
        )
        async for evt in _stream_with_heartbeat(synthesis_stream, interval_s=10.0):
            etype = evt.get("type")
            if etype == "_heartbeat":
                # Keep the frontend watchdog rearmed across long token
                # gaps. No state change, just a liveness ping.
                yield {"type": "synthesis_heartbeat"}
            elif etype == "delta":
                text = str(evt.get("text", ""))
                if text:
                    final_text_parts.append(text)
                    yield {"type": "synthesis_delta", "text": text}
            elif etype == "done":
                agg.add(evt.get("usage"))
                if not final_text_parts and evt.get("final_text"):
                    final_text_parts.append(str(evt["final_text"]))
    except ClaudeCliError as exc:
        # Synthesis blew up — but we still have useful_bundles + scope.
        # Save a partial bundle so the follow-up turn can inherit
        # this scope and the user can rephrase or refine instead of
        # starting from a new candidate pool.
        try:
            partial_bundle = ResearchBundle(
                conversation_id=str(conversation_id),
                last_question=question,
                last_answer="".join(final_text_parts),  # whatever streamed
                confidence="low",
                # Same reasoning as the success path: save rankings so the
                # follow-up inherits the router's scope, not the signal-
                # filtered subset. Synthesis failed here so useful_bundles
                # is an incomplete picture anyway.
                repos_used=[rk.repo_id for rk in rankings],
                repos_used_names=[rk.repo_name for rk in rankings],
                scope_rationale=(
                    f"Partial bundle — synthesis failed: "
                    f"{str(exc)[:140]}. Scope captured for follow-up "
                    "continuity."
                ),
                subgraph_entity_urns=[],
                sources=[],
                last_question_embedding=question_embedding,
            )
            await _persist_research_bundle(
                session_factory=deps.session_factory,
                conversation_id=conversation_id,
                bundle=partial_bundle,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "partial bundle persist failed on synthesis error (non-fatal)",
            )
        yield {"type": "error", "error": f"Synthesis failed: {exc}"}
        return

    final_text = "".join(final_text_parts).strip()

    adversarial_note = _adversarial_check(final_text, useful_bundles)
    if adversarial_note:
        final_text = final_text + adversarial_note

    # Sources: code files + jira issues + confluence pages across all
    # useful bundles, plus org-wide external findings.
    sources: list[dict[str, Any]] = []
    for b in useful_bundles:
        if b.code:
            sources.append(
                {
                    "urn": f"urn:bifrost:repo:{b.repo_name}",
                    "name": b.repo_name,
                    "entity_type": "repo",
                    "relevance": b.code.relevance,
                    "role": b.code.role_in_answer,
                    "files": b.code.key_files[:5],
                    "stale": b.code.metadata_stale,
                }
            )
        if b.jira and b.jira.relevance in ("high", "medium", "low"):
            for iss in b.jira.issues:
                sources.append(
                    {
                        "urn": iss.url,
                        "name": f"{iss.key}: {iss.title}",
                        "entity_type": "jira",
                        "relevance": b.jira.relevance,
                        "role": iss.summary,
                        "repo": b.repo_name,
                    }
                )
        if b.confluence and b.confluence.relevance in ("high", "medium", "low"):
            for pg in b.confluence.pages:
                sources.append(
                    {
                        "urn": pg.url,
                        "name": pg.title,
                        "entity_type": "confluence",
                        "relevance": b.confluence.relevance,
                        "role": pg.summary,
                        "repo": b.repo_name,
                    }
                )
        if b.github and b.github.relevance in ("high", "medium", "low"):
            for pr in b.github.recent_prs:
                sources.append(
                    {
                        "urn": pr.url,
                        "name": f"PR #{pr.number}: {pr.title}",
                        "entity_type": "github_pr",
                        "relevance": b.github.relevance,
                        "role": pr.summary,
                        "repo": b.repo_name,
                    }
                )
            for iss in b.github.relevant_issues:
                sources.append(
                    {
                        "urn": iss.url,
                        "name": f"Issue #{iss.number}: {iss.title}",
                        "entity_type": "github_issue",
                        "relevance": b.github.relevance,
                        "role": iss.summary,
                        "repo": b.repo_name,
                    }
                )
        if b.slack and b.slack.relevance in ("high", "medium", "low"):
            for hit in b.slack.hits:
                sources.append(
                    {
                        "urn": hit.url,
                        "name": f"Slack #{hit.channel}: {hit.author}",
                        "entity_type": "slack",
                        "relevance": b.slack.relevance,
                        "role": hit.summary,
                        "repo": b.repo_name,
                    }
                )
        if b.gdocs and b.gdocs.relevance in ("high", "medium", "low"):
            for d in b.gdocs.docs:
                sources.append(
                    {
                        "urn": d.url,
                        "name": d.title,
                        "entity_type": "gdoc",
                        "relevance": b.gdocs.relevance,
                        "role": d.summary,
                        "repo": b.repo_name,
                    }
                )
    for f in external.findings:
        sources.append(
            {
                "urn": f.url,
                "name": f.title,
                "entity_type": f.source,  # "confluence" | "jira" | "other"
                "relevance": "high",
                "role": f.summary,
            }
        )

    confidence = _confidence_from_bundles(useful_bundles)
    # Evidence-consistency guard.
    # Downgrades ``confirmed`` → ``inferred`` when per-source coverage
    # admits a gap that the LLM may have ignored in its own confidence
    # call. The synthesizer's HARD RULE #2 is the first line of defence;
    # this is the deterministic safety net.
    confidence, _conf_debug = apply_evidence_consistency_guard(
        base_confidence=confidence,
        bundles=useful_bundles,
        answer_text=final_text,
    )
    if _conf_debug.get("downgraded"):
        logger.info(
            "evidence-consistency guard downgraded confidence: bundle_gap=%s answer_gap=%s",
            _conf_debug.get("bundle_gap"),
            _conf_debug.get("answer_gap"),
        )

    # Persist the research bundle for follow-up continuity. The bundle
    # holds metadata pointers only (URNs / source IDs / answer text)
    # so this is bounded regardless of how big the synthesis was.
    walked_entity_urns: list[str] = []
    try:
        # subgraph_entity_urns must carry ACTUAL entity URNs (the ones
        # the agents walked) so the subgraph hydrator can re-fetch their
        # rows + relationships next turn. ``sources`` is a separate
        # citation list (Jira tickets, Confluence pages, repo URNs)
        # used by the UI's footnotes — it lives on the ``sources``
        # field below, not here. Conflating the two left hydration
        # with nothing to fetch (every URL was a Jira ticket, not an
        # entity URN).
        seen_urns: set[str] = set()
        for b in useful_bundles:
            for e in getattr(b, "entities", []) or []:
                urn = getattr(e, "urn", "")
                if not urn or urn in seen_urns:
                    continue
                # Only retain Bifrost entity URNs — skip repo / domain /
                # service URNs (those don't have entity rows that
                # ``hydrate_prior_subgraph`` can match).
                if "/component/" not in urn and "/flow/" not in urn and "/dependency/" not in urn:
                    continue
                seen_urns.add(urn)
                walked_entity_urns.append(urn)
                if len(walked_entity_urns) >= 200:
                    break
            if len(walked_entity_urns) >= 200:
                break
        bundle_to_save = ResearchBundle(
            conversation_id=str(conversation_id),
            last_question=question,
            last_answer=final_text,
            confidence=confidence,
            # rankings = what the route SSE event showed the user.
            # useful_bundles is filtered to repos that returned extraction
            # signal; unindexed repos the router correctly selected score
            # no signal even when they ARE the right answer. Saving
            # useful_bundles here causes the follow-up turn to inherit the
            # wrong scope (the "wrong repos scanned again" bug): the
            # scope-inheritance block at the start of run_research narrows
            # the candidate pool to prior_bundle.repos_used, so whatever
            # we save here becomes the entire candidate universe for turn 2.
            # Using rankings keeps scope inheritance consistent with the
            # route event and the UI's ChatScopeLine display.
            repos_used=[rk.repo_id for rk in rankings],
            repos_used_names=[rk.repo_name for rk in rankings],
            scope_rationale=", ".join(f"{rk.repo_name}: {rk.reason}" for rk in rankings[:3]),
            subgraph_entity_urns=walked_entity_urns,
            sources=_bundle_sources_from_payload(sources),
            last_question_embedding=question_embedding,
        )
        await _persist_research_bundle(
            session_factory=deps.session_factory,
            conversation_id=conversation_id,
            bundle=bundle_to_save,
        )
    except Exception:  # noqa: BLE001
        logger.exception("research bundle build/persist failed")

    # ── Learning loop: record citation signals ──────────────────────
    # Every URN the synthesizer cited is weak positive evidence that
    # the entity exists, was useful, and surfaced for a relevant
    # question. We write these as noise-floor confidence_deltas (the
    # delta amount is 0 → diff < NOISE_FLOOR → recorded for audit but
    # doesn't shift the entity's confidence on its own). This makes
    # the Learning dashboard non-empty and lets the operator see
    # "URN X was cited 47 times across 31 conversations" — real
    # signal even when no automated upgrade fires.
    #
    # Why no auto-upgrade from chat: governance requires confirmation
    # from distinct *tasks* (action-layer briefs), not chat turns.
    # ``confidence_deltas.task_id`` FK targets ``tasks.id``; chat
    # turns don't create task rows. When the brief/verify path runs
    # against the same URN with a real task_id, governance counts
    # those toward the upgrade threshold; chat citations sit alongside
    # as audit trail. Future work: extend governance to dedupe by
    # conversation_id when task_id is null.
    #
    # All-non-blocking: failure here NEVER bubbles up to the chat
    # response. Worst case the dashboard misses a turn's citations.
    try:
        await _record_chat_citations(
            session_factory=deps.session_factory,
            conversation_id=conversation_id,
            walked_entity_urns=walked_entity_urns,
        )
    except Exception:  # noqa: BLE001
        logger.exception("learning citation record failed (non-fatal)")

    # ── Chat-research learning loop (write path) ────────────────────────
    # Walk per-repo edge_verifications + missed_edges from the code
    # agent and feed them into ``confidence_deltas`` (governed via
    # ``learning.record_delta``) plus new ``entity_relationships`` rows.
    audit_summary: dict[str, int] = {}
    try:
        from bifrost.research.chat_audit import chat_research_audit

        audit_stats = await chat_research_audit(
            session_factory=deps.session_factory,
            bundles=useful_bundles,
            conversation_id=str(conversation_id) if conversation_id else None,
            research_run_id=str(research_run_id) if research_run_id else None,
        )
        audit_summary = {
            "edges_confirmed": audit_stats.edges_confirmed,
            "edges_contradicted": audit_stats.edges_contradicted,
            "new_edges_inserted": audit_stats.new_edges_inserted,
            "deltas_recorded": audit_stats.deltas_recorded,
        }
    except Exception:  # noqa: BLE001
        logger.exception("chat_research_audit failed (non-fatal)")

    # ── Adversarial code-claim verification ──────────────────────────────
    # Cross-checks specific code claims in the answer against T1 evidence.
    # Runs concurrently with follow-up generation, appends a corrections
    # block if any claims are contradicted by disk-verified code findings.
    adversarial_note, adversarial_count, adversarial_error = await _adversarial_verify_claims(
        answer=final_text,
        useful_bundles=useful_bundles,
        settings=settings,
    )
    if adversarial_note:
        final_text = final_text + adversarial_note
    for _adv_evt in _adversarial_events(adversarial_note, adversarial_count, adversarial_error):
        yield _adv_evt

    # ── Intelligent follow-up suggestions ────────────────────────────────
    followup_suggestions = _generate_followups(
        answer=final_text,
        question=question,
        useful_bundles=useful_bundles,
        rankings=rankings,
    )
    if followup_suggestions:
        yield {"type": "suggested_followups", "suggestions": followup_suggestions}

    yield {
        "type": "done",
        "answer": final_text,
        "sources": sources,
        "suggested_followups": followup_suggestions,
        "usage": {
            "total_cost_usd": round(agg.total_cost_usd, 4),
            "input_tokens": agg.input_tokens,
            "output_tokens": agg.output_tokens,
            "cache_read_tokens": agg.cache_read_tokens,
            "calls": agg.n_calls,
        },
        "confidence": confidence,
        "reports": [
            {
                "repo_name": b.repo_name,
                "relevance": b.code.relevance if b.code else "none",
                "role": b.code.role_in_answer if b.code else "",
                "files": b.code.key_files if b.code else [],
                "gaps": b.code.gaps if b.code else [],
                "stale": b.code.metadata_stale if b.code else False,
                "jira_hits": len(b.jira.issues) if b.jira else 0,
                "confluence_hits": len(b.confluence.pages) if b.confluence else 0,
                "github_prs": len(b.github.recent_prs) if b.github else 0,
                "github_issues": len(b.github.relevant_issues) if b.github else 0,
                "slack_hits": len(b.slack.hits) if b.slack else 0,
                "gdocs_hits": len(b.gdocs.docs) if b.gdocs else 0,
            }
            for b in useful_bundles
        ],
        "audit_summary": audit_summary,
        "adversarial_error": adversarial_error,
    }


def _render_prior_research_block(prior_bundle: ResearchBundle) -> str:
    """Render the prior research bundle as an XML-tagged context section.

    Used by the synthesizer to know what the previous turn already
    walked, without re-running agents. Stays metadata-only — URN
    strings + source references + a short prior answer snippet — so
    the prompt token count grows by ~200-1500 tokens rather than
    re-injecting the full evidence subgraph.

    The synthesis system prompt instructs the model to treat content
    inside <prior_research> as continuity context, NOT as authoritative
    coverage of the new question.
    """
    repos = ", ".join(prior_bundle.repos_used_names[:8]) or "(none)"
    urn_lines = (
        "\n".join(f"  - {urn}" for urn in prior_bundle.subgraph_entity_urns[:30])
        or "  (none recorded)"
    )
    if len(prior_bundle.subgraph_entity_urns) > 30:
        urn_lines += f"\n  (+ {len(prior_bundle.subgraph_entity_urns) - 30} more)"
    src_lines: list[str] = []
    for s in prior_bundle.sources[:20]:
        title = (s.title or "").strip()[:120]
        ref = (s.ref or "").strip()
        kind = (s.kind or "source").strip()
        if title and ref:
            src_lines.append(f"  - [{kind}] {ref}: {title}")
        elif ref:
            src_lines.append(f"  - [{kind}] {ref}")
    sources_block = "\n".join(src_lines) or "  (none recorded)"
    if len(prior_bundle.sources) > 20:
        sources_block += f"\n  (+ {len(prior_bundle.sources) - 20} more)"
    prior_q = (prior_bundle.last_question or "").strip()[:600]
    prior_a = (prior_bundle.last_answer or "").strip()[:1500]
    return (
        "<prior_research>\n"
        "This is a follow-up turn — the previous turn already produced "
        "research that's worth referencing for continuity. Use it to "
        "avoid re-deriving findings, but DO NOT assume it covers the "
        "current question; the user's new question may need fresh "
        "evidence from the per-repo blocks below.\n\n"
        f"Prior question: {prior_q}\n\n"
        f"Prior repos walked: {repos}\n\n"
        f"Prior subgraph URNs:\n{urn_lines}\n\n"
        f"Prior sources:\n{sources_block}\n\n"
        "Prior answer snippet:\n"
        f"{prior_a}\n"
        "</prior_research>\n"
    )


def _render_communities_block(community_hits: list[dict[str, object]]) -> str:
    """Render top-K communities as a big-picture context section.

    Community summaries are the GraphRAG retrieval unit for global
    queries. Even on specific queries they provide architectural
    framing that individual entities don't.
    """
    if not community_hits:
        return ""
    lines = [
        "## Architectural context — relevant communities",
        "",
        "These are LLM-generated summaries of entity clusters (Louvain "
        "communities over the cross-repo graph) that the retriever "
        "matched to the question. Use them to frame the architectural "
        "context in your TL;DR and How it works sections — they are "
        "the broadest correct frame for the answer.",
        "",
    ]
    for i, c in enumerate(community_hits, 1):
        rep_urns = cast("list[str]", c.get("representative_urns") or [])
        reps_str = ", ".join(f"`{u}`" for u in rep_urns[:4])
        lines.append(f"### Community {i}: {c['name']}")
        lines.append(f"*{c['entity_count']} entities. Representative: {reps_str}*")
        lines.append("")
        lines.append(str(c.get("summary", "")))
        lines.append("")
    return "\n".join(lines)


def _render_flow_paths_block(flow_paths: list) -> str:
    """Render pre-computed DFS flow paths for the synthesizer.

    The model is instructed to use these as the skeleton of the
    End-to-end flow section: each step = one node in a path, cited by
    URN. Cross-repo hops are explicitly flagged so the narrative can
    call out "then the flow leaves this service and enters <other>".
    """
    if not flow_paths:
        return ""
    lines = [
        "## Pre-computed flow paths (graph DFS from seeds)",
        "",
        "Each path below is a DFS chain through the query subgraph "
        "starting from a seed node. They are ranked by "
        "architectural interest (cross-repo hops first, then span, "
        "then length). **Use the first path as the primary skeleton "
        "of your ### End-to-end flow section**: one numbered step per "
        "node, cite its URN, narrate what happens at the edge leading "
        "into it. Additional paths are alternatives worth acknowledging.",
        "",
    ]
    for i, p in enumerate(flow_paths, 1):
        header = (
            f"### Path {i} — "
            f"{len(p.nodes)} nodes, {p.cross_repo_hops} cross-repo hop(s), "
            f"spans {p.span_repos} repo(s)"
        )
        lines.append(header)
        # Emit node chain with arrows, each showing edge rel_type.
        parts: list[str] = []
        for idx, n in enumerate(p.nodes):
            if idx > 0:
                e = p.edges[idx - 1]
                arrow = f" --**{e.rel_type}**" + ("⇒ " if e.is_cross_repo else "--> ")
                parts.append(arrow)
            marker = "⊙ " if n.is_seed else ""
            parts.append(f"{marker}`{n.urn}`")
        lines.append("".join(parts))
        # Per-hop detail so the synthesizer can narrate concretely.
        for idx, e in enumerate(p.edges):
            src = p.nodes[idx]
            tgt = p.nodes[idx + 1]
            cross = " **[cross-repo: flow leaves repo]**" if e.is_cross_repo else ""
            lines.append(f"  - Step {idx + 1}: `{src.urn}` --{e.rel_type}--> `{tgt.urn}`{cross}")
        lines.append("")
    return "\n".join(lines)


def _render_audit_block(code_report: RepoReport) -> str:
    """Emit the Stage 4 audit output (edge verifications + missed edges +
    contradictions) as a compact evidence section for synthesis."""
    if not (
        code_report.edge_verifications or code_report.missed_edges or code_report.contradictions
    ):
        return ""
    lines = [
        "### Code audit (what the agent actually verified on disk)",
        "",
        "The agent below ran Grep/Read to verify specific graph claims. "
        "Trust the verified claims highly and be skeptical of "
        "contradictions — the graph may be stale there.",
    ]

    verified_t = [v for v in code_report.edge_verifications if v.verified == "true"]
    verified_f = [v for v in code_report.edge_verifications if v.verified == "false"]
    verified_u = [v for v in code_report.edge_verifications if v.verified == "unknown"]

    if verified_t:
        lines.append("")
        lines.append("**Verified edges (confirmed in source):**")
        for v in verified_t[:10]:
            evidence = f" — {v.evidence}" if v.evidence else ""
            lines.append(f"- ✓ {v.edge_desc}{evidence}")
    if verified_f:
        lines.append("")
        lines.append("**Contradicted edges (graph claim NOT found in source):**")
        for v in verified_f[:5]:
            evidence = f" — {v.evidence}" if v.evidence else ""
            lines.append(f"- ✗ {v.edge_desc}{evidence}")
    if verified_u:
        lines.append("")
        lines.append("**Unverified edges (agent could not confirm):**")
        for v in verified_u[:5]:
            lines.append(f"- ? {v.edge_desc}")

    if code_report.missed_edges:
        lines.append("")
        lines.append("**Edges the agent found live (not yet in the pre-extracted graph):**")
        for e in code_report.missed_edges[:8]:
            evidence = f" — {e.evidence}" if e.evidence else ""
            lines.append(f"- {e.source_name} --{e.rel_type}--> {e.target_name}{evidence}")

    if code_report.contradictions:
        lines.append("")
        lines.append("**Contradictions between the graph and observed code:**")
        for c in code_report.contradictions[:5]:
            lines.append(f"- ⚠ {c}")

    return "\n".join(lines)


def _render_evidence_block(
    entities: list[EntitySnippet],
    *,
    max_entities: int = 25,
    max_edges_per_entity: int = 5,
    max_total_edges: int = 60,
) -> str:
    """Render the query subgraph as a compact markdown evidence block.

    Synthesis cites FROM this block — every URN here is a valid citation
    target. Entities are already sorted by build_query_subgraph
    (seeds first, then by degree desc). We preserve that order so the
    "Key components" table in the answer can be a top-slice of this list.

    Bounded to protect the synthesis context window.
    """
    if not entities:
        return ""
    ents = entities[:max_entities]
    lines = [
        "### Evidence (multi-hop query subgraph — cite URNs from here)",
        "",
        "Entities are ordered by graph centrality within the subgraph "
        "(seeds marked ⊙; higher degree = more connected to the answer). "
        "Each edge carries a tier badge — `[T1 code]` = directly "
        "observed in source, `[T2 manifest]` = declared in an ownership manifest, "
        "`[T3 meta]` = metadata-inferred, `[T4 conf/jira/slack]` = "
        "documented, `[T5 fuzzy]` = name-match only. Relationships are "
        "split into three sections below — High-confidence (T1/T2), "
        "Medium (T3), Low (T4/T5) — so you can see at a glance which "
        "tier supports each claim. Hedging tone is REQUIRED to track "
        "the tier (see HARD RULE 1).",
        "",
        "| URN | Name | Type | File | Purpose |",
        "|---|---|---|---|---|",
    ]
    for e in ents:
        purpose = (e.purpose or "").replace("|", "/").replace("\n", " ")[:140]
        fp = (e.source_file or "").replace("|", "/")[:80]
        # Seed entities are the ones whose name carries no inbound edges
        # (or which match the question directly) — we approximate by
        # "has zero inbound AND has outbound" or first-in-list. The
        # caller's ordering already puts seeds first, but we also surface
        # them explicitly via a leading marker.
        marker = _seed_marker(e)
        lines.append(f"| {marker}`{e.urn}` | {e.name} | {e.entity_type} | `{fp}` | {purpose} |")

    # Relationships — bucketed by tier so the synthesizer can SEE the
    # confidence floor for each claim and adjust hedging tone.
    # Within each bucket, cross-repo edges sort first because they
    # carry product-narrative weight. Tiers map to confidence buckets:
    #   * T1 (code_verified) + T2 (manifest/gradle/package_json) → high
    #   * T3 (metadata_match)                                     → medium
    #   * T4 (confluence/jira/github_issue/slack)
    #     + T5 (fuzzy_match/llm_guess)                            → low
    high_edges: list[str] = []
    med_edges: list[str] = []
    low_edges: list[str] = []
    high_cross: list[str] = []
    med_cross: list[str] = []
    low_cross: list[str] = []
    total_edges = 0
    seen: set[tuple[str, str, str]] = set()

    def _tier_badge(edge) -> str:
        """`[T1 code]`, `[T2 manifest]`, `[T3 meta]`, `[T4 docs]`, `[T5 fuzzy]`
        — short, visible, forces the synthesizer to weigh claims."""
        short = {
            "code_verified": "code",
            "manifest": "manifest",
            "gradle": "gradle",
            "package_json": "pkg",
            "metadata_match": "meta",
            "confluence": "conf",
            "jira": "jira",
            "github_issue": "gh",
            "slack": "slack",
            "fuzzy_match": "fuzzy",
            "llm_guess": "guess",
        }.get(getattr(edge, "source_type", "unknown"), "?")
        return f"[T{getattr(edge, 'tier', 5)} {short}]"

    def _bucket_for_tier(tier: int) -> str:
        """Fold the 5-tier scale into the 3 hedging buckets the
        synthesis prompt's HARD RULE 1.5 reasons about."""
        if tier <= 2:
            return "high"
        if tier == 3:
            return "medium"
        return "low"

    def _route(line: str, tier: int, is_cross: bool) -> None:
        bucket = _bucket_for_tier(tier)
        if bucket == "high":
            (high_cross if is_cross else high_edges).append(line)
        elif bucket == "medium":
            (med_cross if is_cross else med_edges).append(line)
        else:
            (low_cross if is_cross else low_edges).append(line)

    for e in ents:
        for edge in e.outbound[:max_edges_per_entity]:
            key = (e.urn, edge.other_urn, edge.rel_type)
            if key in seen:
                continue
            seen.add(key)
            is_cross = _is_cross_repo_edge(e.urn, edge.other_urn)
            line = (
                f"- `{e.urn}` --{edge.rel_type}--> `{edge.other_urn}` "
                f"({edge.other_name}, {edge.other_type}) {_tier_badge(edge)}"
                + ("  **[cross-repo]**" if is_cross else "")
            )
            _route(line, getattr(edge, "tier", 5), is_cross)
            total_edges += 1
            if total_edges >= max_total_edges:
                break
        if total_edges >= max_total_edges:
            break
        for edge in e.inbound[:max_edges_per_entity]:
            key = (edge.other_urn, e.urn, edge.rel_type)
            if key in seen:
                continue
            seen.add(key)
            is_cross = _is_cross_repo_edge(edge.other_urn, e.urn)
            line = (
                f"- `{edge.other_urn}` --{edge.rel_type}--> `{e.urn}` "
                f"({edge.other_name}, {edge.other_type}) {_tier_badge(edge)}"
                + ("  **[cross-repo]**" if is_cross else "")
            )
            _route(line, getattr(edge, "tier", 5), is_cross)
            total_edges += 1
            if total_edges >= max_total_edges:
                break

    def _emit_bucket(
        title: str,
        regular: list[str],
        cross: list[str],
        note: str,
    ) -> None:
        if not (regular or cross):
            return
        lines.append("")
        lines.append(title)
        lines.append(f"_{note}_")
        if cross:
            lines.append("")
            lines.append("Cross-repo:")
            lines.extend(cross)
        if regular:
            if cross:
                lines.append("")
                lines.append("In-repo:")
            lines.extend(regular)

    _emit_bucket(
        "### High-confidence edges (T1/T2 — code-verified or config-declared)",
        high_edges,
        high_cross,
        'Cite these with definite language ("X calls Y").',
    )
    _emit_bucket(
        "### Medium-confidence edges (T3 — metadata-inferred)",
        med_edges,
        med_cross,
        'Hedge: "appears to" / "likely" — metadata signal without direct code observation.',
    )
    _emit_bucket(
        "### Low-confidence edges (T4/T5 — docs-only / fuzzy)",
        low_edges,
        low_cross,
        'Mark claims supported only here as "(documented but not '
        'code-verified)". Drop entirely if a higher-tier edge '
        "contradicts.",
    )

    return "\n".join(lines)


def _seed_marker(e: EntitySnippet) -> str:
    """Very light heuristic: inbound==0 AND outbound>0 → probably an entry.
    Real seed marking will come from Subgraph directly once we stop
    round-tripping through EntitySnippet (Stage 3)."""
    if not e.inbound and e.outbound:
        return "⊙ "
    return ""


def _is_cross_repo_edge(src_urn: str, tgt_urn: str) -> bool:
    """Classify whether an edge crosses repository boundaries.

    Compares the repo segment of both URNs. Returns False when either
    URN doesn't carry a recognizable repo segment (e.g. domain or
    service URNs) — those are conservatively classified as same-repo.
    """
    s = _repo_from_urn(src_urn)
    t = _repo_from_urn(tgt_urn)
    return bool(s and t and s != t)


def _repo_from_urn(urn: str) -> str:
    """Extract the repo name from a Bifrost URN.

    Bifrost emits two URN shapes today; this parser handles both so
    the cross-repo classifier works regardless of which extraction
    path produced the URN:

      * Production extraction (the format dominating the entities
        table): ``urn:bifrost:repo/<repo>/<type>/<qualified>``.
        Pipeline.py and the Phase D-G writers use this slash-keyed
        form so a single ``urn:`` prefix scan can find every entity
        for a repo.

      * Fixture / seed URNs: ``urn:bifrost:<type>:<repo>::<qualified>``.
        Older fixtures and a few coordinator-internal URNs (e.g. the
        ``urn:bifrost:repo:<name>`` repo URN itself) use colon-keyed
        form. We accept this so legacy data and tests don't silently
        misclassify edges.

    Returns "" when neither shape matches (e.g. domain or service
    URNs without a repo segment).
    """
    if not urn.startswith("urn:bifrost:"):
        return ""
    body = urn[len("urn:bifrost:") :]
    # Production slash-form: "repo/<repo>/<type>/<name>"
    if body.startswith("repo/"):
        rest = body[len("repo/") :]
        if not rest:
            return ""
        first_slash = rest.find("/")
        return rest if first_slash == -1 else rest[:first_slash]
    # Legacy colon-form: "<type>:<repo>::<qualified>"
    parts = body.split(":")
    if len(parts) < 2:
        return ""
    repo_segment = parts[1]
    return repo_segment.split("::", 1)[0]


def _render_source_coverage_block(
    bundles: list[RepoBundle],
    external: ExternalFindings | None,
) -> str:
    """Aggregate which sources succeeded vs failed across all repos.

    Output is a small Markdown table the synthesiser MUST surface in
    its answer (per the system prompt). Without this, failures in
    Jira/Confluence/GitHub/Slack disappear into the prose as "we
    found nothing" — false-negative attribution that makes the
    answer feel more complete than it is.
    """
    code_ok = code_fail = code_skip = 0
    jira_ok = jira_fail = jira_skip = 0
    conf_ok = conf_fail = conf_skip = 0
    gh_ok = gh_fail = gh_skip = 0
    slack_ok = slack_fail = slack_skip = 0
    fail_messages: list[str] = []

    for b in bundles:
        # Code agent — every bundle should have one.
        if b.code:
            if b.code.error:
                code_fail += 1
                fail_messages.append(f"- code · {b.repo_name}: {b.code.error[:120]}")
            elif b.code.relevance == "skipped":
                code_skip += 1
            else:
                code_ok += 1
        # External agents — may be None (skipped before run if no
        # ownership metadata to scope them).
        if b.jira is None or b.jira.relevance == "skipped":
            jira_skip += 1
        elif b.jira.error:
            jira_fail += 1
            fail_messages.append(f"- jira · {b.repo_name}: {b.jira.error[:120]}")
        else:
            jira_ok += 1
        if b.confluence is None or b.confluence.relevance == "skipped":
            conf_skip += 1
        elif b.confluence.error:
            conf_fail += 1
            fail_messages.append(f"- confluence · {b.repo_name}: {b.confluence.error[:120]}")
        else:
            conf_ok += 1
        if b.github is None or b.github.relevance == "skipped":
            gh_skip += 1
        elif b.github.error:
            gh_fail += 1
            fail_messages.append(f"- github · {b.repo_name}: {b.github.error[:120]}")
        else:
            gh_ok += 1
        if b.slack is None or b.slack.relevance == "skipped":
            slack_skip += 1
        elif b.slack.error:
            slack_fail += 1
            fail_messages.append(f"- slack · {b.repo_name}: {b.slack.error[:120]}")
        else:
            slack_ok += 1

    org_status = "skipped"
    if external is not None:
        if external.error:
            org_status = f"FAILED ({external.error[:120]})"
            fail_messages.append(f"- org-wide-external: {external.error[:120]}")
        elif external.findings:
            org_status = f"OK ({len(external.findings)} hits)"
        else:
            org_status = "OK (no relevant findings)"

    lines: list[str] = [
        "## Source coverage (audit trail)",
        "",
        "Per-source aggregate across all routed repos. The synthesiser",
        "MUST surface this in the answer's `### Source coverage`",
        "section so the user knows which sources contributed and which",
        "failed.",
        "",
        "| Source | Contributed | Failed | Skipped |",
        "|---|---|---|---|",
        f"| **Code (T1 ground truth)** | {code_ok} | {code_fail} | {code_skip} |",
        f"| Jira (T4) | {jira_ok} | {jira_fail} | {jira_skip} |",
        f"| Confluence (T4) | {conf_ok} | {conf_fail} | {conf_skip} |",
        f"| GitHub (T4) | {gh_ok} | {gh_fail} | {gh_skip} |",
        f"| Slack (T4) | {slack_ok} | {slack_fail} | {slack_skip} |",
        f"| Org-wide external | {org_status} | | |",
    ]
    if fail_messages:
        lines += [
            "",
            "### Source failures (verbatim — surface ALL of these in answer)",
        ]
        lines.extend(fail_messages)
    return "\n".join(lines) + "\n"


# ── Format-intent detection ───────────────────────────────────────────────────
#
# When the user asks for "one-page" / "TLDR" / "executive summary" /
# "no fluff" / "in one sentence" etc., the synthesizer must produce a
# concise answer rather than the default deep dossier. Compiled regexes
# are case-insensitive substring matches — be liberal because users
# phrase the same intent many ways.

import re as _re

_FORMAT_INTENT_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    (
        _re.compile(r"\b(one[- ]page|1[- ]page|single[- ]page|single page)\b", _re.IGNORECASE),
        "concise",
    ),
    (
        _re.compile(
            r"\b(no fluff|no preamble|no waffle|skip the preamble|cut the fluff)\b", _re.IGNORECASE
        ),
        "concise",
    ),
    (_re.compile(r"\b(tl;?dr|tldr)\b", _re.IGNORECASE), "concise"),
    (
        _re.compile(r"\b(quick summary|short answer|brief answer|in short)\b", _re.IGNORECASE),
        "concise",
    ),
    (
        _re.compile(r"\b(executive (summary|brief|version)|leadership update)\b", _re.IGNORECASE),
        "executive",
    ),
    (
        _re.compile(
            r"\b(in (one|1) (sentence|line)|single sentence|one[- ]liner)\b", _re.IGNORECASE
        ),
        "single_decision",
    ),
    (
        _re.compile(r"\b(just (the )?(headline|decision)|bottom line)\b", _re.IGNORECASE),
        "single_decision",
    ),
]


def detect_format_intent(question: str) -> str | None:
    """Return ``concise``/``executive``/``single_decision`` when the
    user asks for a non-dossier shape, ``None`` otherwise.

    A request for "one-page, no fluff" must change the answer shape
    (shorter, executive-operational) instead of keeping the long dossier.

    Detection happens at synthesis time so the heavy intent_router can
    stay focused on ask-vs-clarify routing.
    """
    if not question:
        return None
    # Earlier patterns win — single_decision is most specific but also
    # the rarest; check the full list and take the FIRST match in
    # priority order: single_decision > executive > concise.
    out: dict[str, bool] = {}
    for pat, label in _FORMAT_INTENT_PATTERNS:
        if pat.search(question):
            out[label] = True
    if out.get("single_decision"):
        return "single_decision"
    if out.get("executive"):
        return "executive"
    if out.get("concise"):
        return "concise"
    return None


_FORMAT_DIRECTIVE_BLOCKS: dict[str, str] = {
    "concise": (
        "## ⚠️ FORMAT OVERRIDE — CONCISE MODE (overrides system-prompt structure rules)\n\n"
        'The user asked for a short, action-oriented answer ("one-page",'
        ' "TL;DR", "no fluff", or similar). **THIS DIRECTIVE OVERRIDES**'
        ' the system prompt\'s "### Follow this EXACT structure" rules'
        " for this turn. Ignore those structural rules; obey these:\n\n"
        "* **HARD CAP: ≤300 words total.** Count your words. If you go over,"
        " your answer is wrong. The format_adherence scorer penalizes"
        " verbose answers severely.\n"
        "* **ALLOWED HEADINGS (use only these):** `### TL;DR` (2-3"
        " sentences), `### Key points` (5-8 tight bullets), `### Sources`"
        " (footnote definitions, ≤6 entries).\n"
        "* **DO NOT EMIT these sections — they are forbidden in concise"
        " mode:** `### How it works`, `### Key components`,"
        " `### End-to-end flow`, `### Cross-source alignment`, `### Source"
        " coverage`, `### Caveats & staleness`, `### Gaps & unknowns`.\n"
        "* **NO mermaid diagram** unless the question explicitly contains"
        ' the word "diagram" or "draw".\n'
        "* **DEPTH rules from the system prompt do NOT apply** in this"
        ' mode. Do not aim for "3-6 substantive paragraphs per section"'
        " — aim for tight bullets, total answer under one screen.\n"
        "* If a confidence caveat needs to be conveyed, fold it into the"
        " TL;DR's last sentence (e.g., \"Confidence: inferred — see"
        ' /admin/learning for the evidence chain.") rather than a'
        " standalone block.\n\n"
        "The full evidence is still attached below for your reference,"
        " but your answer MUST be tight. Bullet-first, action-oriented,"
        " no preamble. Brevity > depth here — that is the user's explicit"
        " ask.\n"
    ),
    "executive": (
        "## FORMAT DIRECTIVE — EXECUTIVE MODE\n\n"
        "The user asked for a leadership-style brief. Cut everything"
        " that isn't a decision, an action, or an owner.\n\n"
        "* **Target length: ≤200 words.**\n"
        "* **Structure (5-line max):**\n"
        "    1. **Headline:** one-sentence what's-going-on.\n"
        "    2. **What's working:** 2-3 bullets.\n"
        "    3. **What's at risk:** 2-3 bullets with owners named.\n"
        "    4. **The decision:** one explicit choice the leader needs to make.\n"
        "    5. **Confidence + provenance:** one line.\n"
        "* **OMIT all dossier sections** (Source coverage, Caveats,"
        " Cross-source alignment, etc.).\n"
        "* **NO mermaid diagram.**\n"
    ),
    "single_decision": (
        "## FORMAT DIRECTIVE — SINGLE-DECISION MODE\n\n"
        "The user asked for a one-sentence / one-decision answer. Output"
        " a single decision plus its reasoning + named owner. Nothing"
        " else.\n\n"
        "* **Target length: ≤100 words.** Hard cap.\n"
        "* **Structure:** one sentence answering the question, then ONE"
        " short paragraph (≤60 words) covering rationale + owner +"
        " confidence.\n"
        "* **NO sections, NO bullets, NO diagram, NO sources block.**\n"
        "* If you cannot produce a concrete decision from the evidence,"
        " say so in one sentence — do NOT pad with context.\n"
    ),
}


def _build_synthesis_user_message(
    question: str,
    bundles: list[RepoBundle],
    external: ExternalFindings | None = None,
    *,
    prior_turns: str = "",
    flow_paths: list | None = None,
    community_hits: list[dict[str, object]] | None = None,
    prior_bundle: ResearchBundle | None = None,
    prior_subgraph: HydratedSubgraph | None = None,
) -> str:
    """Build the synthesis agent's user message.

    Per repo emits:
      (A) Agent summaries (code / jira / confluence / github / slack) —
          short, opinionated reconnaissance.
      (B) An Evidence block — the raw entity subgraph: URN table + 1-hop
          relationships. This is the source of truth the synthesizer
          cites. Without it the answer ends up as paraphrased summaries.

    When Stage 5 `flow_paths` are provided, a top-level **Pre-computed
    flow paths** section is emitted BEFORE the per-repo blocks. The
    synthesizer is instructed to use these paths as the literal skeleton
    of the End-to-end flow section.

    When ``prior_bundle`` is provided (chat follow-up turn), a
    ``<prior_research>`` XML-tagged block is emitted near the top so
    the synthesizer can reference prior findings for continuity rather
    than re-deriving them. The block carries metadata only (URNs,
    sources, prior question + answer snippet) — never raw entity
    bodies, which would balloon the prompt.

    When ``prior_subgraph`` is *also* provided (EXTEND_BUNDLE narrow
    fan-out path), a "## Prior research evidence" block is emitted
    AFTER the live per-repo bundles, with the same Evidence-table
    shape. The synthesizer can now do real cross-turn flow reasoning
    (e.g. compare a freshly-walked repo's flow against a prior repo's
    by URN) without the prior-repo entity bodies being absent.
    """
    sections: list[str] = []
    # Format-intent directive comes FIRST so the synthesizer sees the
    # length/section rules before anything else. The block — when
    # injected — overrides the default dossier structure rules from
    # the system prompt for this turn only.
    fmt_intent = detect_format_intent(question)
    if fmt_intent and fmt_intent in _FORMAT_DIRECTIVE_BLOCKS:
        sections.append(_FORMAT_DIRECTIVE_BLOCKS[fmt_intent])

    if prior_turns:
        sections.append(
            "## Prior conversation (for context — do NOT restate)\n" + prior_turns + "\n",
        )
    if prior_bundle is not None:
        sections.append(_render_prior_research_block(prior_bundle))
    sections.append(f"## Current user question\n{question}\n")

    # Top-level source-coverage table — aggregates per-source success/
    # failure across all repos so the synthesiser can ATTRIBUTE the
    # answer to working sources and CALL OUT failures explicitly. This
    # is the "user wants to know which external sources were used and
    # which failed" requirement: the data is right here, the prompt
    # below requires the synthesiser to surface it.
    sections.append(_render_source_coverage_block(bundles, external))

    # Stage 6: community summaries — the "what does this area of the
    # codebase do" context, for broad/architectural questions.
    if community_hits:
        sections.append(_render_communities_block(community_hits))

    # Stage 5: pre-computed flow paths.
    if flow_paths:
        sections.append(_render_flow_paths_block(flow_paths))

    sections.append(
        f"Research across {len(bundles)} repo(s). Each repo ships an "
        f"agent summary AND a structured Evidence block (URN table + "
        f"relationships) — cite URNs from the Evidence block in your "
        f"answer."
    )
    for b in bundles:
        sections.append(f"\n## Repo: {b.repo_name}")
        if b.code:
            stale_flag = " [STALE: remote has new commits]" if b.code.metadata_stale else ""
            comps = (
                ", ".join(
                    f"{c.get('name', '?')} ({c.get('why', '')})" for c in b.code.key_components[:6]
                )
                or "(none)"
            )
            flows = (
                ", ".join(
                    f"{f.get('name', '?')}: {f.get('summary', '')}" for f in b.code.key_flows[:6]
                )
                or "(none)"
            )
            files = ", ".join(b.code.key_files[:6]) or "(none)"
            gaps = "; ".join(b.code.gaps) or "(none)"
            sections.append(
                f"### Code agent summary (relevance={b.code.relevance}){stale_flag}\n"
                f"Role: {b.code.role_in_answer}\n"
                f"Key components: {comps}\n"
                f"Key flows: {flows}\n"
                f"Key files: {files}\n"
                f"Evidence: {b.code.evidence}\n"
                f"Gaps: {gaps}"
            )
            # Stage 4 audit-mode block — what the agent literally
            # verified on disk. This is higher-trust than key_components
            # / key_flows (which are inferred summaries).
            audit = _render_audit_block(b.code)
            if audit:
                sections.append(audit)

        # Evidence block — the structured facts. This is what the
        # synthesizer should cite from in its footnotes.
        if b.entities:
            sections.append(_render_evidence_block(b.entities))
        # External-source agents: render with EXPLICIT failure markers
        # so the synthesizer can correctly call out which sources
        # contributed vs which failed. Previously, an errored agent
        # came back with relevance="none"+error=<msg>, and the render
        # code dumped "(no issues found)" — indistinguishable from a
        # successful "found nothing" lookup. The synthesizer
        # confidently summarised "Jira had no relevant issues" when
        # the truth was "Jira call failed (auth missing)".
        if b.jira and b.jira.relevance != "skipped":
            if b.jira.error:
                sections.append(
                    f"### Jira agent (project={b.jira.jira_project_key}, "
                    f"status=FAILED)\n"
                    f"⚠️ Jira lookup FAILED: {b.jira.error}\n"
                    f"This source contributed NO evidence. Do NOT claim "
                    f"Jira was silent — it errored before returning."
                )
            else:
                sections.append(
                    f"### Jira agent (project={b.jira.jira_project_key}, "
                    f"relevance={b.jira.relevance})\n"
                    f"{b.jira.summary or '(no summary)'}\n"
                    + (
                        "\n".join(
                            f"- {iss.key} [{iss.status}/{iss.issue_type}]: "
                            f"{iss.title} — {iss.summary} ({iss.url})"
                            for iss in b.jira.issues[:8]
                        )
                        if b.jira.issues
                        else "(no issues found)"
                    )
                )
        if b.confluence and b.confluence.relevance != "skipped":
            if b.confluence.error:
                sections.append(
                    f"### Confluence agent (space={b.confluence.confluence_space_key}, "
                    f"status=FAILED)\n"
                    f"⚠️ Confluence lookup FAILED: {b.confluence.error}\n"
                    f"This source contributed NO evidence. Do NOT claim "
                    f"Confluence was silent — it errored before returning."
                )
            else:
                sections.append(
                    f"### Confluence agent (space={b.confluence.confluence_space_key}, "
                    f"relevance={b.confluence.relevance})\n"
                    f"{b.confluence.summary or '(no summary)'}\n"
                    + (
                        "\n".join(
                            f"- {pg.title} — {pg.summary} ({pg.url})"
                            for pg in b.confluence.pages[:8]
                        )
                        if b.confluence.pages
                        else "(no pages found)"
                    )
                )
        if b.github and b.github.relevance != "skipped":
            if b.github.error:
                sections.append(
                    f"### GitHub activity agent (owner={b.github.github_owner}/"
                    f"{b.github.github_name}, status=FAILED)\n"
                    f"⚠️ GitHub lookup FAILED: {b.github.error}\n"
                    f"This source contributed NO evidence. Do NOT claim "
                    f"GitHub was silent — it errored before returning."
                )
            else:
                pr_list = (
                    "\n".join(
                        f"- PR #{pr.number} [{pr.state}]: {pr.title} — {pr.summary} ({pr.url})"
                        for pr in b.github.recent_prs[:6]
                    )
                    or "(no relevant PRs)"
                )
                issue_list = (
                    "\n".join(
                        f"- Issue #{iss.number} [{iss.state}]: {iss.title} — {iss.summary} ({iss.url})"
                        for iss in b.github.relevant_issues[:6]
                    )
                    or "(no relevant issues)"
                )
                sections.append(
                    f"### GitHub activity agent (owner={b.github.github_owner}/"
                    f"{b.github.github_name}, relevance={b.github.relevance})\n"
                    f"{b.github.summary or '(no summary)'}\n"
                    f"Recent PRs:\n{pr_list}\n"
                    f"Relevant issues:\n{issue_list}"
                )
        if b.slack and b.slack.relevance != "skipped":
            if b.slack.error:
                sections.append(
                    f"### Slack channel agent (channel={b.slack.slack_channel}, "
                    f"status=FAILED)\n"
                    f"⚠️ Slack lookup FAILED: {b.slack.error}\n"
                    f"This source contributed NO evidence. Do NOT claim "
                    f"Slack was silent — it errored before returning."
                )
            else:
                hit_list = (
                    "\n".join(
                        f"- {hit.author} in {hit.channel}: "
                        f"{hit.excerpt[:150]} — {hit.summary} ({hit.url})"
                        for hit in b.slack.hits[:6]
                    )
                    or "(no relevant threads)"
                )
                sections.append(
                    f"### Slack channel agent (channel={b.slack.slack_channel}, "
                    f"relevance={b.slack.relevance})\n"
                    f"{b.slack.summary or '(no summary)'}\n"
                    f"{hit_list}"
                )
        if b.gdocs and b.gdocs.relevance != "skipped":
            if b.gdocs.error:
                sections.append(
                    f"### Google Drive / Docs agent "
                    f"(folder={b.gdocs.gdoc_folder_url or 'keyword fallback'}, "
                    f"status=FAILED)\n"
                    f"⚠️ Drive lookup FAILED: {b.gdocs.error}\n"
                    f"This source contributed NO evidence. Do NOT claim "
                    f"Drive was silent — it errored before returning."
                )
            else:
                doc_list = (
                    "\n".join(
                        f"- {d.title} (owner={d.owner_email or '?'}, "
                        f"modified={d.last_modified or '?'}): {d.summary} ({d.url})"
                        for d in b.gdocs.docs[:6]
                    )
                    or "(no relevant Drive docs)"
                )
                sections.append(
                    f"### Google Drive / Docs agent "
                    f"(folder={b.gdocs.gdoc_folder_url or 'keyword fallback'}, "
                    f"relevance={b.gdocs.relevance})\n"
                    f"{b.gdocs.summary or '(no summary)'}\n"
                    f"{doc_list}"
                )

    # Org-wide external findings (the coordinator-level agent)
    if external and external.findings:
        sections.append(
            f"\n## Org-wide findings "
            f"({len(external.findings)} docs in "
            f"{', '.join(external.searched_sources) or 'Confluence + Jira'})"
        )
        for f in external.findings:
            sections.append(f"- [{f.source}] {f.title} — {f.summary}\n  {f.url}")
    elif external and external.error:
        sections.append(f"\n## Org-wide findings\n(skipped — {external.error})")

    if prior_subgraph is not None and not prior_subgraph.is_empty():
        sections.append(_render_prior_evidence_block(prior_subgraph))

    sections.append(
        "\n\n---\n"
        "**Now write the answer using the structure mandated in the "
        "system prompt** (### TL;DR → ### How it works → ### Key "
        "components → ### End-to-end flow → ### Cross-source alignment "
        "→ ### Caveats & staleness → ### Gaps & unknowns → ### Sources).\n"
        "Cite URNs from the Evidence blocks above using GFM footnotes "
        "[^1], [^2], …. When code and docs conflict, prefer code-truth "
        "and flag the disagreement in the Cross-source section."
    )
    return "\n".join(sections)


def _render_prior_evidence_block(prior_subgraph: HydratedSubgraph) -> str:
    """Render hydrated prior-repo entities as Evidence blocks.

    Same shape as the per-live-repo Evidence block — just labelled
    "## Prior research evidence" so the synthesizer treats it as
    cross-turn continuity context rather than a duplicate of fresh
    research. Each prior repo gets its own subsection.
    """
    if prior_subgraph.is_empty():
        return ""
    out: list[str] = [
        "\n## Prior research evidence (from earlier turn — re-fetched "
        "for cross-turn flow reasoning)",
        "",
        "These entities were walked in a previous turn. Treat them as "
        "established context: cite them when comparing against the "
        "freshly-walked repos above (e.g. cross-repo flow alignment), "
        "but do NOT re-derive findings the prior turn already produced.",
    ]
    for sub in prior_subgraph.repos:
        out.append(f"\n### Prior repo: {sub.repo_name}")
        block = _render_evidence_block(sub.entities)
        if block:
            out.append(block)
    if prior_subgraph.missing_urns:
        out.append(
            f"\n_(prior bundle referenced "
            f"{len(prior_subgraph.missing_urns)} URN(s) that no longer "
            "exist — these may have been deleted by a re-extraction)_"
        )
    return "\n".join(out)


# ── Precision adversarial verifier ───────────────────────────────────────────
# Extracts specific falsifiable code claims from the answer and checks each
# against T1 evidence. NOT generic AI questions — each probe is a targeted
# lookup: "design claims prop X exists at component Y, does it?"

_ADVERSARIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},  # exact claim from the answer
                    "probe": {"type": "string"},  # specific thing checked
                    "reality": {"type": "string"},  # what code actually shows
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "action": {"type": "string"},  # what the engineer should do differently
                },
                "required": ["claim", "probe", "reality", "severity"],
            },
        },
        "verified_claims": {
            "type": "array",
            "items": {"type": "string"},  # claims that checked out (optional, builds trust)
        },
    },
    "required": ["corrections"],
}

_ADVERSARIAL_SYSTEM = """You are an adversarial code reviewer. You receive:
1. A synthesized answer about a codebase
2. T1 code evidence (what code agents actually found on disk)
3. T4 doc evidence (Confluence/Jira design docs)

Your job: find every claim in the answer that is contradicted by or unsupported by T1 code evidence.

RULES — these are non-negotiable:
- Only raise corrections where T1 code evidence (disk-verified) contradicts the answer.
  Never raise a "correction" based on T4 docs vs T4 docs — that is speculation.
- Each correction must name:
  (a) the EXACT claim from the answer (quote it)
  (b) the specific probe you ran (e.g. "searched for data-testid at OrderSummary.tsx root")
  (c) what code actually shows (e.g. "attribute only exists at :130 on the submit button, not the root element")
  (d) severity: critical (would cause a compile error or broken behavior), high (wrong assumption
      that changes the implementation), medium (minor inaccuracy), low (cosmetic)
  (e) what the engineer should do differently

DO NOT raise corrections for:
- Claims the answer itself marked as [documented, not code-verified]
- Claims about repos that were not code-scanned this turn
- Differences in phrasing or paraphrasing that don't affect behavior
- Speculative "maybe this is wrong" without a concrete T1 counter-example

The goal is precision, not quantity. 2 surgical corrections beat 8 vague ones."""


async def _adversarial_verify_claims(
    answer: str,
    useful_bundles: list[RepoBundle],
    settings: BifrostSettings,
) -> tuple[str, int, str | None]:
    """Extract specific falsifiable code claims from ``answer`` and
    cross-check each against T1 code evidence from ``useful_bundles``.

    Returns (formatted_block, correction_count, error). The block is empty
    when no corrections were found. ``error`` is None unless the verifier
    LLM call failed; the caller must surface it so a failed check is not
    mistaken for a clean one.
    """
    # Build a compact evidence summary from T1 code findings
    code_evidence: list[str] = []
    for b in useful_bundles:
        if not b.code or b.code.error:
            continue
        ev = f"Repo: {b.repo_name}\n"
        if b.code.role_in_answer:
            ev += f"Role: {b.code.role_in_answer[:600]}\n"
        if b.code.evidence:
            ev += f"Evidence: {b.code.evidence[:600]}\n"
        if b.code.key_files:
            ev += f"Key files found: {', '.join(b.code.key_files[:8])}\n"
        if b.code.gaps:
            ev += f"Code agent noted gaps: {'; '.join(b.code.gaps[:3])}\n"
        code_evidence.append(ev)

    if not code_evidence:
        return "", 0, None  # no T1 evidence to check against

    from bifrost.extraction.cli_client import ClaudeLlmClient

    client = ClaudeLlmClient(settings)
    user_msg = (
        "## Answer to verify\n\n"
        + answer[:6000]
        + "\n\n## T1 Code evidence (disk-verified)\n\n"
        + "\n---\n".join(code_evidence[:5])
        + "\n\nIdentify every claim in the answer that is contradicted by the T1 code evidence above. "
        "Be surgical — only raise corrections with specific file:line counter-evidence."
    )

    try:
        result = await client.call_with_json_schema(
            system_prompt=_ADVERSARIAL_SYSTEM,
            user_message=user_msg,
            model=settings.triage_model,
            json_schema=_ADVERSARIAL_SCHEMA,
            max_turns=4,
            timeout_override=60,
            allowed_tools=[],  # no tools — reason from the evidence given
            inherit_user_mcp=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("adversarial_verify LLM call failed")
        return "", 0, f"{type(exc).__name__}: {exc}"

    data = result.parsed or {}
    corrections = [c for c in (data.get("corrections") or []) if c.get("claim")]
    verified = data.get("verified_claims") or []

    if not corrections and not verified:
        return "", 0, None

    lines = ["\n---\n## ⚔ Adversarial code check"]
    if corrections:
        lines.append(f"\n**{len(corrections)} claim(s) contradicted by code evidence:**\n")
        for c in corrections:
            sev = c.get("severity", "medium")
            sev_badge = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}.get(sev, "🟡")
            lines.append(f"{sev_badge} **{sev.upper()}** — {c.get('claim', '')}")
            lines.append(f"   Probe: {c.get('probe', '')}")
            lines.append(f"   Reality: {c.get('reality', '')}")
            if c.get("action"):
                lines.append(f"   Action: {c['action']}")
            lines.append("")
    if verified:
        lines.append(f"\n**{len(verified)} claim(s) code-verified correct:**")
        for v in verified[:3]:
            lines.append(f"  ✓ {v}")
    return "\n".join(lines), len(corrections), None


def _adversarial_events(note: str, count: int, error: str | None) -> list[dict[str, Any]]:
    """Stream events for one adversarial-check outcome."""
    if error:
        return [
            {
                "type": "status",
                "message": f"Adversarial code check failed, answer was not code-verified: {error}",
            }
        ]
    if note:
        return [
            {
                "type": "adversarial_corrections",
                "count": count,
                "message": "Code check found discrepancies between docs and source",
            }
        ]
    return []


def _generate_followups(
    *,
    answer: str,
    question: str,
    useful_bundles: list[RepoBundle],
    rankings: list[Any],
) -> list[dict[str, Any]]:
    """Generate context-aware follow-up chip suggestions from the answer.

    Pulls from 4 signal sources in priority order:
    1. Gaps & unknowns section of the answer (most actionable)
    2. Jira ticket keys mentioned in the answer
    3. Repos touched (drill-down per service)
    4. Content patterns (implementation asks, architecture, etc.)

    Returns up to 4 suggestions as {prompt, rationale} dicts.
    """
    import re as _re2

    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(prompt: str, rationale: str) -> None:
        if prompt not in seen and len(suggestions) < 4:
            seen.add(prompt)
            suggestions.append({"prompt": prompt, "rationale": rationale})

    # 1. Extract items from "Gaps & unknowns" section
    gaps_match = _re2.search(
        r"###\s+Gaps\s+[&＆]\s+unknowns?\s*\n(.*?)(?=\n###|\Z)",
        answer,
        _re2.IGNORECASE | _re2.DOTALL,
    )
    if gaps_match:
        gap_text = gaps_match.group(1).strip()
        # Pull first 2 specific gaps as prompts
        bullets = _re2.findall(r"[-*]\s+\*{0,2}([^\n]{15,120})\*{0,2}", gap_text)
        for bullet in bullets[:2]:
            clean = _re2.sub(r"\*{1,2}", "", bullet).strip().rstrip(".")
            # Turn gap description into a question
            if "?" not in clean:
                prompt = f"Can you find {clean}?"
            else:
                prompt = clean
            _add(prompt, "Gap flagged in the answer")

    # 2. Jira keys mentioned in the answer — offer to drill into linked work
    jira_keys = list(dict.fromkeys(_re2.findall(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b", answer)))
    for key in jira_keys[:2]:
        if key not in question:  # skip the seed ticket(s) already asked about
            _add(
                f"What does {key} require and which repos does it touch?",
                f"Linked Jira ticket {key} referenced in the answer",
            )

    # 3. Repos that had high-relevance code signal — offer per-service drill-down
    high_repos = [
        b.repo_name
        for b in useful_bundles
        if b.code and b.code.relevance in ("high", "medium") and b.repo_name
    ]
    for repo in high_repos[:2]:
        if len(suggestions) >= 4:
            break
        _add(
            f"Show me the exact files to change in {repo}",
            f"{repo} was identified as a high-relevance repo",
        )

    # 4. Content-pattern fallbacks — only if the above didn't fill all slots
    content_lower = answer.lower()
    question_lower = question.lower()

    # Feature/build questions → implementation guidance
    if any(w in question_lower for w in ("build", "implement", "add", "create", "jira", "ticket")):
        if len(suggestions) < 4:
            touched = [rk.repo_name for rk in rankings[:2]] if rankings else []
            if touched:
                _add(
                    f"What tests do I need to write for {touched[0]}?",
                    "Testing guidance for the implementation",
                )
    # Architecture/flow questions
    if any(w in content_lower for w in ("flow", "architecture", "end-to-end", "e2e")):
        _add(
            "Show me a sequence diagram of the full flow",
            "Architecture context in the answer",
        )
    # Stale/incomplete signal
    if "not yet fully indexed" in content_lower or "stale" in content_lower:
        _add(
            "Re-run this after the repos finish indexing for a deeper answer",
            "Some repos were not yet fully indexed this turn",
        )

    return suggestions


def _adversarial_check(
    answer: str,
    useful_bundles: list,
) -> str:
    """Scan answer for code claims (prop names, file paths, selector names, method calls)
    that can be cross-referenced against T1 code evidence in the bundles.
    Returns a corrections block if mismatches found, empty string otherwise.
    """
    import re

    # Collect all T1 code facts: component names, file paths, method names
    # that agents actually found in code (not docs)
    code_facts = set()
    for b in useful_bundles:
        if b.code and not b.code.error:
            for f in b.code.key_files or []:
                code_facts.add(f.lower())
    if not code_facts:
        return ""
    # Find claims in answer about specific files/props/methods (backtick-quoted identifiers)
    claims = re.findall(r"`([^`]{3,80})`", answer)
    # Flag any file path claim that mentions a path not seen in T1 evidence
    suspicious = []
    for c in claims:
        if ("." in c or "/" in c) and not any(fact in c.lower() for fact in code_facts):
            # Only flag if it looks like a specific path/identifier, not a concept
            if re.search(r"\.(ts|tsx|kt|java|py|swift|js|jsx)$", c):
                suspicious.append(c)
    if not suspicious:
        return ""
    lines = [
        "\n\n> **Note:** The following file paths were cited but were not directly "
        "code-verified this turn (T4 documentation only). Treat with caution until "
        "the repo is fully indexed:"
    ]
    for s in suspicious[:5]:
        lines.append(f"> - `{s}`")
    return "\n".join(lines)


def _confidence_from_bundles(bundles: list[RepoBundle]) -> str:
    """Aggregate a single confidence label across all bundles.

    Rules (in order, first match wins):
      confirmed: at least one bundle has a 'high' relevance from any source
                 (code / jira / confluence), OR we gathered >=10 real
                 cross-source hits across bundles.
      inferred:  at least one bundle has 'medium' from any source, OR we
                 gathered 3-9 cross-source hits.
      uncertain: everything else — thin evidence, fallbacks, errors.
    """
    if not bundles:
        return "uncertain"

    all_relevances: list[str] = []
    total_hits = 0
    for b in bundles:
        if b.code:
            all_relevances.append(b.code.relevance)
        if b.jira and b.jira.relevance != "skipped":
            all_relevances.append(b.jira.relevance)
            total_hits += len(b.jira.issues)
        if b.confluence and b.confluence.relevance != "skipped":
            all_relevances.append(b.confluence.relevance)
            total_hits += len(b.confluence.pages)
        if b.github and b.github.relevance != "skipped":
            all_relevances.append(b.github.relevance)
            total_hits += len(b.github.recent_prs) + len(b.github.relevant_issues)
        if b.slack and b.slack.relevance != "skipped":
            all_relevances.append(b.slack.relevance)
            total_hits += len(b.slack.hits)
        if b.gdocs and b.gdocs.relevance != "skipped":
            all_relevances.append(b.gdocs.relevance)
            total_hits += len(b.gdocs.docs)

    if any(r == "high" for r in all_relevances) or total_hits >= 10:
        return "confirmed"
    if any(r == "medium" for r in all_relevances) or total_hits >= 3:
        return "inferred"
    return "uncertain"


# Phrases the synthesizer or per-source agents emit when their evidence
# coverage admits a gap; a body saying "not routed" under a "confirmed"
# headline is the trust failure this guards against.
#
# Maintained as case-insensitive substring needles. Conservative — we
# downgrade rather than over-claim. Never the other way around.
_EVIDENCE_GAP_NEEDLES: tuple[str, ...] = (
    "search failed",
    "lookup failed",
    "request failed",
    "unavailable this turn",
    "timed out",
    "not routed",
    "not in scope",
    "not walked",
    "couldn't determine",
    "could not determine",
    "exact caller unknown",
    "exact upstream caller unknown",
    "documented but not code-verified",
    "treat as speculative",
    "first commit/pr not actually located",
    "no code agent ran",
    "no signal from",
)


def _downgrade_rank(level: str) -> int:
    """Sortable rank: confirmed > inferred > uncertain > unknown."""
    return {"confirmed": 3, "inferred": 2, "uncertain": 1}.get(level, 0)


def _has_evidence_gap_in_bundles(bundles: list[RepoBundle]) -> str | None:
    """Detect coverage gaps across per-source agents.

    Returns a short reason string when ANY per-source report carries an
    error / skipped status / "search failed"-style signal. ``None``
    means coverage was clean.
    """
    for b in bundles:
        for kind in ("code", "jira", "confluence", "github", "slack", "gdocs"):
            r = getattr(b, kind, None)
            if r is None:
                continue
            err = getattr(r, "error", None)
            if err:
                return f"{b.repo_name}.{kind}: {str(err)[:80]}"
            relevance = getattr(r, "relevance", None)
            if relevance == "skipped":
                # 'skipped' is structural (no slack channel configured,
                # etc.) — that's not a gap, it's a fact about the
                # ownership row. Don't downgrade for skipped.
                continue
        # Code agent's own self-reported gaps array — when populated
        # with substantive entries, the agent itself is telling us its
        # walk was incomplete.
        if b.code is not None:
            for gap in getattr(b.code, "gaps", None) or []:
                gap_l = (gap or "").strip().lower()
                if gap_l and any(n in gap_l for n in _EVIDENCE_GAP_NEEDLES):
                    return f"{b.repo_name}.code reported gap: {gap[:80]}"
    return None


def _has_evidence_gap_in_answer(answer_text: str) -> str | None:
    """Scan the synthesized answer for gap-marker phrases.

    The synthesizer is supposed to surface failures in its "Source
    coverage" + "Caveats" sections. When it does, the answer body
    contains the needle — and the headline confidence MUST track that.
    Returns the matched needle, or ``None`` when none found.
    """
    body = (answer_text or "").lower()
    for needle in _EVIDENCE_GAP_NEEDLES:
        if needle in body:
            return needle
    return None


def apply_evidence_consistency_guard(
    *,
    base_confidence: str,
    bundles: list[RepoBundle],
    answer_text: str,
) -> tuple[str, dict[str, Any]]:
    """Deterministic post-processor: downgrade ``confirmed`` to
    ``inferred`` when coverage admits gaps.

    The synthesizer prompt has a HARD RULE telling the LLM to do this
    itself, but LLMs drift. This guard is the deterministic safety net
    against stated certainty that the evidence only partly supports.

    Rules:
      * If ``base_confidence`` is ``confirmed`` AND a gap was detected
        anywhere (per-source error, code-agent gap entry, or gap-marker
        phrase in the answer body) → downgrade to ``inferred``.
      * ``inferred`` and ``uncertain`` are NEVER upgraded by this
        guard — only downgrades are allowed.

    Returns ``(adjusted_confidence, debug_info)``.
    """
    debug: dict[str, Any] = {
        "base": base_confidence,
        "downgraded": False,
        "bundle_gap": None,
        "answer_gap": None,
    }

    if base_confidence != "confirmed":
        return base_confidence, debug

    bundle_gap = _has_evidence_gap_in_bundles(bundles)
    answer_gap = _has_evidence_gap_in_answer(answer_text)
    if bundle_gap or answer_gap:
        debug["downgraded"] = True
        debug["bundle_gap"] = bundle_gap
        debug["answer_gap"] = answer_gap
        return "inferred", debug

    return base_confidence, debug
