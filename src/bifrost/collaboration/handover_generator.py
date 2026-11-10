"""Handover-doc generator (repo + domain + team + region).

Repo scope is the base case. Multi-repo scopes:
  * ``generate_domain_handover`` — pools entities across all repos
    linked to a Domain via ``repos.domain_id``.
  * ``generate_team_handover`` — pools entities across all
    repos owned by a team (squad), joined via
    ``OrgRepo.registered_repo_id``.

  * ``generate_region_handover`` — pools entities across all repos
    in a geographic region (e.g. "us", "eu"),
    joined via ``OrgRepo.region``. Structurally identical to the
    team variant; the join column is the only difference.

All reuse the same single-Sonnet-call shape as the repo generator
with prompts that emphasize the multi-repo nature (call out which
repo contributes which capability). The doc is written to disk under
``docs/handovers/{repos|domains|teams|regions}/<slug>/index.md`` and
indexed in the ``handover_docs`` table.

Still out of scope (lands later):
  * Tree decomposition (synthesis from child docs vs flat re-pull)
  * Pre-distillation cache + parallel section authoring
  * Cross-doc linking + diagram emission
  * Loop-back from collaboration session doubts
  * Auto-flag-stale on extraction completion (the schema field
    is in place; the trigger isn't yet wired)

The generator is a single Sonnet call with a sectioned JSON
schema. Sonnet is fed:
  - Repo metadata (name, url, domain)
  - A signal-density-bounded summary of entities (top-N by tier
    + count distribution per type)

…and returns the canonical 7 sections (executive summary,
architecture, critical flows, ownership, dependencies, KTLO
surface, glossary). Each section is rendered into the index.md
in the canonical section order.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.collaboration.events import publish_handover_event
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient
from bifrost.storage.models import (
    ConfluencePage,
    Domain,
    Entity,
    EntityRelationship,
    HandoverDoc,
    OrgRepo,
    Repo,
    RepoOwnership,
)
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


# Top-N entities passed to the model per type. We bias toward
# tier-1 (canonical) entities and cap inputs to keep the prompt
# tight — Sonnet does NOT need every entity to write a useful
# summary; it needs a representative slice.
_PER_TYPE_CAP = 30
_TOTAL_ENTITY_CAP = 200

# Tree decomposition: when a multi-repo scope has more than
# this many repos, switch from pooling raw entities into one Sonnet
# call to (a) generating per-repo child handovers in parallel and
# (b) composing the parent handover from their body_mds. This is
# strictly better at scale (each child uses its repo's full signal)
# but costs ~2-3x as many Sonnet calls, so we keep the simpler
# pooled path for small scopes where signal density isn't a problem.
_TREE_THRESHOLD = 5

# Cap on parallel child handover generations. Keeps total Anthropic
# CLI subprocess load bounded even when a domain has 100+ repos.
# Children run under an asyncio.Semaphore so we don't fork the box.
_CONCURRENT_CHILD_CAP = 8

# Per-child body truncation when composing the parent prompt. Each
# repo handover is ~10-20KB; with up to ~50 children we'd blow the
# context budget. 3000 chars (~750 tokens) is enough for the model
# to see each child's headline structure without re-reading every
# detail. Full bodies remain available on disk.
_CHILD_BODY_CHAR_CAP = 3000

# Confluence-page injection caps. We pull at most this many
# pages per scope and truncate each body to keep the prompt bounded
# even when a scope has hundreds of indexed pages.
_CONFLUENCE_PAGE_CAP = 5
_CONFLUENCE_BODY_CHAR_CAP = 2000

# Cap for the auto-derived cross-repo flow diagram. We
# aggregate cross-repo edges by (source_repo, target_repo, rel_type)
# and keep the top-N by count. 15 is enough to surface the dominant
# integration shape without rendering an unreadable hairball when a
# scope spans many repos with dense interconnections.
_CROSS_REPO_DIAGRAM_EDGE_CAP = 15


@dataclass
class HandoverGenerationResult:
    doc_id: uuid.UUID
    fs_path: str
    title: str
    used_fallback: bool = False


_HANDOVER_SYSTEM_PROMPT = f"""\
You are writing a handover document that a STAFF ENGINEER will use at
3am during an incident, on their first day joining a team, and when
deciding whether a change is safe to ship.

You are given:
  - Repository metadata and URL
  - Extracted knowledge graph (component names, flow names, dependencies)
  - ACTUAL SOURCE CODE from key files (README, entry point, business logic)
  - Ownership metadata from the repo manifest (squad, Slack, Jira, contacts)
  - Cross-repo blast radius (which services call INTO this one)
  - Confluence wiki pages

HARD RULES — violating any of these produces a useless document:
  1. NEVER say "check CODEOWNERS", "verify in the on-call pager", "look up in
     AWS console", or "consult Confluence". If you don't know a fact,
     write "**UNKNOWN** — needs manual verification" once and move on.
  2. NEVER say "step ordering is approximate" or "inferred from entity
     names" — you have actual source code; use it.
  3. NEVER produce a section that is just a list of class names without
     explaining what they DO and WHY.
  4. ALWAYS cite the actual file path and function/method name when
     describing code behaviour. E.g.: `app/mainloop.go:CycleOrchestrator`
  5. Every dependency MUST answer: "what happens to THIS service if THAT
     dependency goes down?" Don't just list it.
  6. The incident_response section MUST be a step-by-step triage guide,
     not a list of tools. Start with "1. Check …", "2. If X then Y".

Produce these sections as separate JSON fields:

  title              — 1 line. "<Repo name> — handover".

  executive_summary  — 3 paragraphs.
                       Para 1: What problem does this service solve?
                         Who are its callers? What happens to the
                         business if it goes down for 1 hour?
                       Para 2: How does it work at a high level?
                         What is the core data flow? What persistence
                         does it own?
                       Para 3: What is non-obvious or surprising about
                         this service that a new engineer must know?

  architecture       — Markdown. For each top-level component:
                       - What it does (1-2 sentences)
                       - The key file(s) and entry function
                       - How it connects to the others
                       Include a Mermaid flowchart showing data flow
                       AND the key data stores (MySQL, DynamoDB,
                       SQS, SNS etc.) as labeled nodes.

  critical_flows     — Markdown. Top 5-7 flows. For each:
                       - Happy path: numbered steps citing
                         file:FunctionName at each step
                       - Error path: what can go wrong + what happens
                       - SLA / timeout: if known from code/config

  ownership          — Markdown. USE the <ownership_metadata> block.
                       Write a table with: Squad, Slack channel,
                       Jira project, Confluence space, compliance
                       tags declared in the service manifest, on-call contact. For any missing
                       field write "UNKNOWN". Then: escalation path
                       in plain English — who to Slack first, who to
                       page if they don't respond.

  blast_radius       — Markdown. "If this service has an outage,
                       these services break:" — use the
                       <blast_radius_callers> block. For each caller:
                       relationship type, estimated impact on that
                       caller's users. Then: what is the GRACEFUL
                       DEGRADATION story — do callers have fallbacks
                       or timeouts? Do they retry? Does the caller or
                       end user feel the impact immediately or is it
                       delayed?

  incident_response  — Markdown. Step-by-step triage guide for
                       the three most likely failure scenarios:
                       (a) service is completely down / not responding
                       (b) service is responding but producing wrong
                           results / data corruption
                       (c) service is slow (high latency)
                       For each: CHECK → DIAGNOSE → FIX. Cite
                       actual metric names, log patterns, or alert
                       names from the code/config. Be specific:
                       "look for ERROR in the error-tracker project named X"
                       is better than "check error logs".

  key_configuration  — Markdown. Table of critical runtime config:
                       | Variable | Default | Effect | Where set |
                       List env vars, feature flags (any flag service),
                       and any hardcoded thresholds that an engineer
                       MUST know about. If a wrong value will cause
                       an outage, mark it ⚠️ CRITICAL.

  dependencies       — Markdown. Two subsections:
                       "Upstream (we call these)" — table with:
                         service name | what we call it for |
                         timeout/retry policy if known |
                         degradation if it goes down
                       "Downstream (these call us)" — from
                         blast_radius_callers context.

  ktlo_surface       — Markdown. What is actually running in
                       production that keeps this alive:
                       - Alert rules: cite names from config files
                       - Dashboards: APM/Grafana if mentioned
                         in code or config
                       - Background jobs / workers / scheduled tasks
                       - DLQ / dead-letter queues and their DLQ depth
                         alarm threshold if known
                       - Known toil or manual runbooks

  glossary           — Markdown. 5-15 terms specific to this service
                       that an outsider would not know. Include
                       business terms AND internal code names.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object per the schema. No markdown wrapper.
"""


_HANDOVER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "title",
        "executive_summary",
        "architecture",
        "critical_flows",
        "ownership",
        "blast_radius",
        "incident_response",
        "key_configuration",
        "dependencies",
        "ktlo_surface",
        "glossary",
    ],
    "properties": {
        "title": {"type": "string", "maxLength": 200},
        "executive_summary": {"type": "string"},
        "architecture": {"type": "string"},
        "critical_flows": {"type": "string"},
        "ownership": {"type": "string"},
        "blast_radius": {"type": "string"},
        "incident_response": {"type": "string"},
        "key_configuration": {"type": "string"},
        "dependencies": {"type": "string"},
        "ktlo_surface": {"type": "string"},
        "glossary": {"type": "string"},
    },
}


def _handovers_dir() -> Path:
    return Path(os.environ.get("BIFROST_HANDOVERS_DIR", "docs/handovers"))


def _slugify(value: str) -> str:
    """Filesystem-safe directory slug from a repo name."""
    s = value.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "repo"


def _summarize_entities(entities: list[Entity]) -> tuple[str, dict[str, int]]:
    """Group entities by type, take top-N per type, render as wrapped
    text + return the per-type counts (used in the prompt's preface)."""
    by_type: dict[str, list[Entity]] = {}
    for e in entities:
        by_type.setdefault(e.entity_type, []).append(e)

    # Sort each bucket: tier 1 first (canonical), then by name.
    for k in by_type:
        by_type[k].sort(key=lambda e: (e.knowledge_tier, e.name.lower()))

    counts = {k: len(v) for k, v in by_type.items()}

    # Build a per-type sample, capped at _PER_TYPE_CAP, with metadata
    # only for entities that have something useful in it. Keeps the
    # prompt readable.
    parts: list[str] = []
    total = 0
    for kind in sorted(by_type, key=lambda k: counts[k], reverse=True):
        sample = by_type[kind][:_PER_TYPE_CAP]
        if not sample:
            continue
        if total + len(sample) > _TOTAL_ENTITY_CAP:
            sample = sample[: max(0, _TOTAL_ENTITY_CAP - total)]
            if not sample:
                break
        total += len(sample)
        lines = [f"{kind} ({counts[kind]} total, showing {len(sample)}):"]
        for e in sample:
            qn = f" [{e.qualified_name}]" if e.qualified_name else ""
            lines.append(f"  - {e.name}{qn}  (tier={e.knowledge_tier}, conf={e.confidence_level})")
        parts.append("\n".join(lines))
    return "\n\n".join(parts), counts


# Mermaid validation + table fallback. Sonnet occasionally
# emits a malformed mermaid block (unclosed brackets, wrong header,
# stray characters). The UI's <Mermaid> component already catches
# parse errors and shows a collapsible source viewer, but that hides
# the *intent* of the diagram from the reader. So before writing the
# handover to disk, we shape-validate every mermaid fence and — if
# invalid — swap it for a Markdown component table extracted from the
# architecture section. The on-disk doc stays useful even when the
# diagram is broken.

# Mermaid diagram-type prefixes a valid block must start with.
_MERMAID_DIAGRAM_PREFIXES: tuple[str, ...] = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
)

# Regex: a fenced ```mermaid ... ``` block. Captures the inner code.
# DOTALL so the body can span lines; non-greedy so consecutive blocks
# don't get eaten as one. Anchors require the fences to be on their
# own line (the standard Markdown shape).
_MERMAID_FENCE_RE = re.compile(
    r"^```mermaid[ \t]*\n(.*?)\n```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

_MERMAID_FALLBACK_NOTE = "> _Architecture diagram not rendered — see component table below._"


def _validate_mermaid_block(code: str) -> bool:
    """Heuristic shape-validator for a mermaid code block.

    True ⇒ the block looks well-formed enough to render.
    False ⇒ obvious red flag found; caller should swap for a table.

    Checks (any failure ⇒ False):
      * First non-comment, non-blank line starts with a known
        diagram-type keyword (flowchart, graph, sequenceDiagram, …).
      * Every non-comment, non-blank line has balanced ``"``, ``(`` /
        ``)``, ``[`` / ``]``, and ``{`` / ``}``.

    No real Mermaid parser exists in Python and we won't shell out to
    the Node CLI — when in doubt, return False so the caller can swap
    in the table fallback (still useful) rather than ship a bomb-SVG.
    """
    if not code or not code.strip():
        return False

    lines = code.split("\n")

    # Find first significant line (non-blank, non-comment).
    first: str | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        first = stripped
        break

    if first is None:
        return False
    if not any(first.startswith(p) for p in _MERMAID_DIAGRAM_PREFIXES):
        return False

    # Bracket balance. Strings inside double-quotes are skipped for
    # bracket counting because mermaid node labels like
    # ``A["foo (bar)"]`` legitimately contain unbalanced inner chars.
    #
    # ``[``/``(`` must balance *per line* — that's how node-shape
    # syntax works in flowcharts, and unclosed-on-the-line is the
    # most common LLM truncation mode. ``{``/``}`` is allowed to
    # span lines because classDiagram / stateDiagram bodies do exactly
    # that (``class Foo {\n  +bar()\n}``), so we balance them across
    # the whole block instead.
    block_brace_depth = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("%%"):
            continue

        if stripped.count('"') % 2 != 0:
            return False

        in_quote = False
        depth_paren = depth_square = 0
        for ch in stripped:
            if ch == '"':
                in_quote = not in_quote
                continue
            if in_quote:
                continue
            if ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
            elif ch == "[":
                depth_square += 1
            elif ch == "]":
                depth_square -= 1
            elif ch == "{":
                block_brace_depth += 1
            elif ch == "}":
                block_brace_depth -= 1
            if depth_paren < 0 or depth_square < 0 or block_brace_depth < 0:
                return False
        if depth_paren or depth_square:
            return False

    return block_brace_depth == 0


def _extract_table_from_architecture(parsed: dict[str, Any]) -> str | None:
    """Try to surface a 2-column component table from the parsed
    architecture section.

    Returns the table Markdown, or ``None`` if no shape we recognize
    is present.

    Strategy:
      1. If the architecture text already contains a Markdown table
         (a header row + a ``| --- |`` separator), return the first
         such table verbatim.
      2. Otherwise, scan for bullet lines of the shape
         ``- **Name**: description`` (em-dash variant accepted) and
         render them as ``| Component | Description |``.
      3. Else return ``None``.
    """
    arch = str(parsed.get("architecture") or "").strip()
    if not arch:
        return None

    lines = arch.split("\n")

    # (1) Existing table — header + separator pair.
    for i in range(len(lines) - 1):
        head = lines[i].strip()
        sep = lines[i + 1].strip()
        if (
            head.startswith("|")
            and sep.startswith("|")
            and re.fullmatch(r"\|[\s:|-]+\|", sep) is not None
        ):
            # Greedy collect contiguous table rows.
            table_lines = [lines[i], lines[i + 1]]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                table_lines.append(lines[j])
                j += 1
            return "\n".join(table_lines)

    # (2) Bullet-list components.
    bullet_re = re.compile(r"^\s*[-*]\s+\*\*(?P<name>[^*]+?)\*\*\s*[:\-—–]\s*(?P<desc>.+?)\s*$")
    rows: list[tuple[str, str]] = []
    for ln in lines:
        m = bullet_re.match(ln)
        if m is None:
            continue
        name = m.group("name").strip()
        desc = m.group("desc").strip()
        if name:
            rows.append((name, desc))

    if not rows:
        return None

    out = ["| Component | Description |", "| --- | --- |"]
    for name, desc in rows:
        # Escape pipes inside cells so the table doesn't break.
        out.append(f"| {name.replace('|', r'\|')} | {desc.replace('|', r'\|')} |")
    return "\n".join(out)


def _sanitize_mermaid_blocks(md: str, parsed: dict[str, Any]) -> str:
    """Replace invalid mermaid fences in ``md`` with a Markdown table
    fallback when one can be extracted from the architecture section.

    For each ```mermaid ... ``` fence:
      * If ``_validate_mermaid_block`` returns True ⇒ leave it.
      * Else, try ``_extract_table_from_architecture``:
          - If a table is extracted ⇒ replace fence with a callout +
            the table.
          - If no table ⇒ leave the fence alone (the UI's <Mermaid>
            component still has its source-viewer fallback).
    """
    if "```mermaid" not in md:
        return md

    # Cache the table extraction — it's idempotent and may be reused
    # by multiple bad blocks. ``cache`` is a 1-element list so the
    # closure can mutate it without a ``nonlocal`` re-binding dance.
    cache: list[str | None] = []

    def _replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if _validate_mermaid_block(body):
            return match.group(0)

        if not cache:
            cache.append(_extract_table_from_architecture(parsed))
        table_md = cache[0]

        if not table_md:
            # No table available — leave block alone, UI viewer kicks in.
            return match.group(0)

        return f"{_MERMAID_FALLBACK_NOTE}\n\n{table_md}"

    return _MERMAID_FENCE_RE.sub(_replace, md)


def _handover_frontmatter(
    *,
    scope_kind: str,
    scope_value: str,
    title: str,
) -> str:
    """Build the YAML frontmatter S13 mandates on every generated doc.

    The on-disk Markdown becomes the source of truth for trustworthiness
    metadata: anyone reading docs/handovers/<kind>/<slug>/index.md should
    be able to tell when it was generated, what state its sources were
    in, and how confident the platform is in the claims.

    Values that aren't yet tracked (review status, version diffs, per-
    section gaps) are stamped with safe defaults today and will firm
    up as the lifecycle / gap-extraction features land. Even the stub
    values are useful: they tell a reviewer "this doc is generated
    output, not human-curated, and it has not been reviewed yet."
    """
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    # Quote string values to keep YAML parsers happy even when titles
    # contain colons, dashes, or other special chars.
    safe_title = title.replace('"', '\\"')
    safe_scope = scope_value.replace('"', '\\"')
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f"scope_kind: {scope_kind}\n"
        f'scope_value: "{safe_scope}"\n'
        f"generated_at: {now}\n"
        "generator: bifrost-handover-generator\n"
        "version: 1\n"
        "status: draft\n"
        "source_state: extracted\n"
        "confidence_level: inferred\n"
        "confidence: 0.7\n"
        "known_gaps: []\n"
        "---\n\n"
    )


def _render_handover_md(repo: Repo, parsed: dict[str, Any]) -> str:
    """Compose the Markdown handover from the parsed sections."""
    title = str(parsed.get("title") or f"{repo.name} — handover").strip()
    fm = _handover_frontmatter(
        scope_kind="repo",
        scope_value=repo.name,
        title=title,
    )
    md = fm + "\n".join(
        [
            f"# {title}",
            "",
            f"_Repo:_ `{repo.name}`{f'  ·  {repo.url}' if repo.url else ''}",
            "",
            "## Executive summary",
            str(parsed.get("executive_summary", "")).strip(),
            "",
            "## Architecture",
            str(parsed.get("architecture", "")).strip(),
            "",
            "## Critical flows",
            str(parsed.get("critical_flows", "")).strip(),
            "",
            "## Ownership",
            str(parsed.get("ownership", "")).strip(),
            "",
            "## Dependencies",
            str(parsed.get("dependencies", "")).strip(),
            "",
            "## KTLO surface",
            str(parsed.get("ktlo_surface", "")).strip(),
            "",
            "## Glossary",
            str(parsed.get("glossary", "")).strip(),
            "",
            "## Blast radius",
            str(parsed.get("blast_radius", "")).strip(),
            "",
            "## Incident response",
            str(parsed.get("incident_response", "")).strip(),
            "",
            "## Key configuration",
            str(parsed.get("key_configuration", "")).strip(),
            "",
        ]
    )
    return _sanitize_mermaid_blocks(md, parsed)


def _parse_github_owner_name(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL."""
    m = re.search(r"github\.com[/:]([^/]+)/([^/.]+)", url or "")
    if not m:
        return None
    return m.group(1), m.group(2).removesuffix(".git")


async def _fetch_github_source_context(repo_url: str) -> str:
    """Fetch key source files from GitHub via gh CLI.

    Reads README, the repo manifest, and top entry-point files.
    Returns a formatted string ready to inject into the prompt.
    Returns empty string if gh is not authenticated or repo not accessible.
    """
    import asyncio
    import base64
    import json

    parsed_owner_name = _parse_github_owner_name(repo_url)
    if not parsed_owner_name:
        return ""
    owner, name = parsed_owner_name

    async def _gh_read_file(path: str, max_chars: int = 4000) -> str | None:
        """Read a single file from GitHub via gh api. Returns None if not found."""
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "api",
            f"repos/{owner}/{name}/contents/{path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None
        if proc.returncode != 0:
            return None
        try:
            blob = json.loads(stdout.decode("utf-8", errors="replace"))
            content = base64.b64decode(blob.get("content", "")).decode("utf-8", errors="replace")
            # Truncate to max_chars, cutting on a line boundary
            if len(content) > max_chars:
                cut = content[:max_chars]
                last_nl = cut.rfind("\n")
                content = (cut[:last_nl] if last_nl > max_chars // 2 else cut) + "\n... (truncated)"
            return content
        except Exception:
            return None

    # Candidate entry-point files to try in order
    entry_point_candidates = [
        "app/app.go",
        "cmd/main.go",
        "main.go",
        "app/main.go",
        "src/main.py",
        "app.py",
        "server.go",
        "cmd/server/main.go",
        "src/index.ts",
        "src/app.ts",
        "src/server.ts",
        "app/api.go",
        "internal/app/app.go",
    ]

    parts: list[str] = []

    # README (highest priority — often has the best overview)
    readme = await _gh_read_file("README.md", max_chars=3000)
    if readme is None:
        readme = await _gh_read_file("readme.md", max_chars=3000)
    if readme:
        parts.append(f"<readme>\n{readme}\n</readme>")

    # Entry point (main application setup)
    for candidate in entry_point_candidates:
        entry = await _gh_read_file(candidate, max_chars=4000)
        if entry:
            parts.append(f'<source_file path="{candidate}">\n{entry}\n</source_file>')
            break

    return "\n\n".join(parts)


async def generate_repo_handover(
    *,
    repo_id: uuid.UUID,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> HandoverGenerationResult:
    """Generate (or regenerate) the handover doc for a repo.

    Idempotent on (scope_kind='repo', scope_value=<repo.name>,
    parent_id=NULL): a second call updates the on-disk file and the
    DB row's metadata in place. Older briefs are kept on disk in
    case they were referenced elsewhere; only the canonical
    ``index.md`` is rewritten.

    Raises ``ValueError`` if the repo doesn't exist; surfaces
    ``HandoverGenerationResult.used_fallback=True`` if the LLM call
    fails (no doc is created in that case)."""
    settings = settings or get_settings()

    # Load repo metadata
    repo_row = await repo._session.get(Repo, repo_id)
    if repo_row is None:
        raise ValueError(f"repo {repo_id} not found")

    # Pull RepoOwnership for real squad/channel/Jira data
    ownership_row = await repo._session.get(RepoOwnership, repo_id)

    # Emit progress events on the scope's channel. The UI
    # subscribes at click-time using (scope_kind, scope_value) so
    # users see "writing executive summary…" instead of an opaque
    # 30-90s spinner.
    await publish_handover_event(
        "repo",
        repo_row.name,
        "scope_resolved",
        {"repo_name": repo_row.name},
    )

    # Pull entities for this repo, capped to keep the prompt bounded.
    stmt = (
        select(Entity)
        .where(Entity.scope_repo_id == repo_id)
        .order_by(Entity.knowledge_tier, Entity.name)
        .limit(_TOTAL_ENTITY_CAP * 5)  # over-fetch; per-type cap below.
    )
    entities = list((await repo._session.execute(stmt)).scalars().all())
    entity_summary, counts = _summarize_entities(entities)

    if not entities:
        # Nothing to summarize. Surface a stub doc + warn so the user
        # knows to re-extract first instead of generating nonsense.
        logger.warning(
            "handover generator: repo=%s has 0 indexed entities; stub doc only",
            repo_row.name,
        )
        # Return a fallback path: write a stub doc.
        out_dir = _handovers_dir() / "repos" / _slugify(repo_row.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / "index.md"
        final_path.write_text(
            f"# {repo_row.name} — handover (stub)\n\n"
            "This repo has no extracted entities yet. Run an "
            "extraction first, then regenerate the handover.\n",
            encoding="utf-8",
        )
        doc = await _upsert_doc(repo, repo_row, final_path)
        await publish_handover_event(
            "repo",
            repo_row.name,
            "complete",
            {
                "doc_id": str(doc.id),
                "fs_path": str(final_path),
                "used_fallback": True,
                "reason": "no_entities",
            },
        )
        return HandoverGenerationResult(
            doc_id=doc.id,
            fs_path=str(final_path),
            title=f"{repo_row.name} — handover (stub)",
            used_fallback=True,
        )

    # Cross-repo callers: entities in OTHER repos that call into this repo's entities
    from sqlalchemy import text as _sa_text

    caller_rows = (
        (
            await repo._session.execute(
                _sa_text("""
            SELECT DISTINCT r.name AS caller_repo, er.rel_type, COUNT(*) AS edge_count
            FROM entity_relationships er
            JOIN entities src ON src.id = er.source_id
            JOIN entities tgt ON tgt.id = er.target_id
            JOIN repos r ON r.id = src.scope_repo_id
            WHERE tgt.scope_repo_id = :repo_id
              AND src.scope_repo_id != :repo_id
            GROUP BY r.name, er.rel_type
            ORDER BY edge_count DESC
            LIMIT 15
        """),
                {"repo_id": str(repo_id)},
            )
        )
        .mappings()
        .all()
    )

    # Format ownership metadata for the prompt
    ownership_lines: list[str] = []
    if ownership_row:
        if ownership_row.squad:
            ownership_lines.append(f"squad: {ownership_row.squad}")
        if ownership_row.slack_channel:
            ownership_lines.append(f"slack_channel: {ownership_row.slack_channel}")
        if ownership_row.jira_project_key:
            ownership_lines.append(f"jira_project_key: {ownership_row.jira_project_key}")
        if ownership_row.confluence_space_key:
            ownership_lines.append(f"confluence_space_key: {ownership_row.confluence_space_key}")
        tags = ", ".join(ownership_row.compliance_tags) or "none"
        ownership_lines.append(f"compliance_tags: {tags}")
        # Pull primary_contact from raw_manifest
        raw = ownership_row.raw_manifest or {}
        for key in ("contact", "owner", "maintainer", "oncall"):
            val = raw.get(key)
            if val:
                ownership_lines.append(f"manifest_{key}: {val}")
                break
    ownership_context = (
        "\n".join(ownership_lines)
        if ownership_lines
        else "(not yet synced — run sync-ownership to populate)"
    )

    # Format blast radius callers
    blast_lines: list[str] = []
    for row in caller_rows:
        blast_lines.append(
            f"  - {row['caller_repo']} ({row['rel_type']}, {row['edge_count']} edges)"
        )
    blast_context = (
        "\n".join(blast_lines) if blast_lines else "(no cross-repo callers found in entity graph)"
    )

    # Pull related Confluence pages (best-effort) so the model can cite
    # real wiki pages instead of admitting "no documentation index".
    confluence_pages = await _pull_confluence_for_scope(
        repo,
        "repo",
        repo_row.name,
        [repo_row.name],
    )

    # Fetch actual source code from GitHub (README + entry point).
    # Best-effort — empty string if gh is not authenticated.
    source_context = await _fetch_github_source_context(repo_row.url or "")

    # Build the user message
    parts: list[str] = [
        "Repository:",
        wrap_untrusted(
            f"name: {repo_row.name}\n"
            f"url: {repo_row.url or '(none)'}\n"
            f"description: {repo_row.description or '(none)'}",
            "repo_metadata",
        ),
        "",
        f"Entity counts (per type, full repo): {counts}",
        "",
        "Representative entities (sampled, tier-1 first):",
        wrap_untrusted(entity_summary, "entity_slice"),
        "",
    ]
    if source_context:
        parts.extend(
            [
                "Actual source code from the repository (README + entry point):",
                source_context,
                "",
            ]
        )
    parts.extend(
        [
            "Ownership metadata (from manifest sync — USE THIS for the ownership section):",
            wrap_untrusted(ownership_context, "ownership_metadata"),
            "",
            "Cross-repo callers (services that call INTO this repo — USE THIS for blast_radius section):",
            wrap_untrusted(blast_context, "blast_radius_callers"),
            "",
        ]
    )
    confluence_block = _format_confluence_block(confluence_pages)
    if confluence_block:
        parts.extend(confluence_block)
        parts.append("")
    parts.append("Produce the handover doc now. Return the JSON object exactly per the schema.")
    user_message = "\n".join(parts)

    client = ClaudeLlmClient(settings)
    await publish_handover_event(
        "repo",
        repo_row.name,
        "compose_started",
        {"entity_count": len(entities)},
    )
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_HANDOVER_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_HANDOVER_SCHEMA,
            max_turns=1,
            timeout_override=240,  # bigger doc, more sections
        )
        parsed = completion.parsed
    except ClaudeCliError as exc:
        logger.warning(
            "handover generator LLM failed for repo=%s: %s",
            repo_row.name,
            exc,
        )
        await publish_handover_event(
            "repo",
            repo_row.name,
            "error",
            {"reason": str(exc)},
        )
        return HandoverGenerationResult(
            doc_id=uuid.UUID(int=0),
            fs_path="",
            title="",
            used_fallback=True,
        )

    title = str(parsed.get("title") or f"{repo_row.name} — handover").strip()
    await publish_handover_event(
        "repo",
        repo_row.name,
        "compose_done",
        {"title": title},
    )

    # Atomic write: temp + rename so a crash mid-write never leaves a
    # half-formed index.md.
    out_dir = _handovers_dir() / "repos" / _slugify(repo_row.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "index.md"
    # Snapshot the prior body (if any) BEFORE the
    # temp+replace below overwrites it.
    await _snapshot_existing_body_pre_regen(
        repo=repo,
        scope_kind="repo",
        scope_value=repo_row.name,
    )
    tmp_path = out_dir / "index.md.tmp"
    tmp_path.write_text(_render_handover_md(repo_row, parsed), encoding="utf-8")
    tmp_path.replace(final_path)
    await publish_handover_event(
        "repo",
        repo_row.name,
        "fs_written",
        {"fs_path": str(final_path)},
    )

    doc = await _upsert_doc(repo, repo_row, final_path)

    logger.info(
        "handover generated repo=%s doc_id=%s path=%s",
        repo_row.name,
        doc.id,
        final_path,
    )
    await publish_handover_event(
        "repo",
        repo_row.name,
        "complete",
        {
            "doc_id": str(doc.id),
            "fs_path": str(final_path),
            "title": title,
            "used_fallback": False,
        },
    )
    return HandoverGenerationResult(
        doc_id=doc.id,
        fs_path=str(final_path),
        title=title,
    )


async def _upsert_doc(
    repo: EntityRepository,
    repo_row: Repo,
    final_path: Path,
) -> HandoverDoc:
    """Create or update the HandoverDoc row for this repo scope."""
    doc = await _upsert_doc_generic(
        repo=repo,
        scope_kind="repo",
        scope_value=repo_row.name,
        final_path=final_path,
    )
    # A child repo handover just regenerated; flag any
    # parent (domain/team/region) handovers covering this repo as
    # ``enriched_by_children`` so the UI hints at fresh child material.
    # Best-effort — never fail the main generation path on this.
    try:
        await repo.mark_parent_handovers_enriched(repo_row.name)
    except Exception:  # noqa: BLE001
        logger.exception(
            "mark_parent_handovers_enriched failed for repo=%s — continuing",
            repo_row.name,
        )
    return doc


async def _snapshot_existing_body_pre_regen(
    *,
    repo: EntityRepository,
    scope_kind: str,
    scope_value: str,
) -> None:
    """If a handover already exists for ``(scope_kind, scope_value)``,
    snapshot its on-disk body BEFORE the upcoming regen overwrites
    the file. Best-effort — any failure is logged + swallowed.

    pairs with the diff viewer in the UI. Call this
    *before* the temp+replace pattern executes; once the temp file
    has been renamed onto ``final_path`` the prior body is gone.

    No-op when no prior doc / fs_path exists (first generation), or
    when the file can't be read for any reason — first-time
    generations have no history to snapshot, and an unreadable
    pre-existing file isn't worth blocking the regen over.
    """
    try:
        stmt = select(HandoverDoc).where(
            HandoverDoc.scope_kind == scope_kind,
            HandoverDoc.scope_value == scope_value,
            HandoverDoc.parent_id.is_(None),
        )
        existing = (await repo._session.execute(stmt)).scalar_one_or_none()
        if existing is None or not existing.fs_path:
            return
        try:
            old_body = Path(existing.fs_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        await repo.record_handover_body_snapshot(
            doc_id=existing.id,
            body_md=old_body,
            event_type="pre-regen",
        )
    except Exception:  # noqa: BLE001
        # Defensive belt-and-braces: the helper inside repository
        # already swallows snapshot insert errors, but lookup-side
        # failures (e.g. a transient DB blip on the SELECT) shouldn't
        # surface to the regen path either.
        logger.exception(
            "snapshot_existing_body_pre_regen failed for scope=%s/%s — continuing without snapshot",
            scope_kind,
            scope_value,
        )


# ── per-section regeneration ────────────────────────
#
# Regenerating an entire handover (~$1-3 per repo, ~$3-10 per domain)
# is overkill when one section is wrong. ``regenerate_section`` lets a
# user replace a single ``## <Header>``-bounded chunk in-place via a
# focused single-Sonnet-call. The rest of the body — including any
# Q+A appended via ``promote_doubt_candidate`` and any human edits —
# is preserved verbatim.
#
# Vocabulary: the 7 schema fields plus ``common_questions`` (the
# Q+A section appended outside the schema). The route layer validates
# the section_key before calling here; this helper raises ValueError
# on an unknown key for defence-in-depth.

# Maps the canonical section_key (snake_case, matches the schema +
# the URL path param) to the rendered Markdown header text.
_SECTION_KEY_TO_HEADER: dict[str, str] = {
    "executive_summary": "Executive summary",
    "architecture": "Architecture",
    "critical_flows": "Critical flows",
    "ownership": "Ownership",
    "blast_radius": "Blast radius",
    "incident_response": "Incident response",
    "key_configuration": "Key configuration",
    "dependencies": "Dependencies",
    "ktlo_surface": "KTLO surface",
    "glossary": "Glossary",
    # ``common_questions`` is the Q+A pile appended via
    # ``_append_qa_entry`` after promotions; it isn't in
    # ``_HANDOVER_SCHEMA`` but a user may still want to rewrite it
    # (e.g. condense after many promotions). The regen prompt below
    # treats it like any other section.
    "common_questions": "Common questions",
}

# Friendly label per section, for the prompt. Mostly the header text
# but lets us swap in a clearer phrasing in the prompt without
# re-wording the on-disk header.
_SECTION_KEY_TO_PROMPT_LABEL: dict[str, str] = {
    "executive_summary": "executive summary (2-3 paragraphs: what does this scope do, who is it for, what is the team's mission)",
    "architecture": "architecture (3-7 top-level components and their relationships; mermaid only when confident)",
    "critical_flows": "critical flows (3-7 user-visible or business-critical flows in sequence-of-steps form)",
    "ownership": "ownership (names/teams/escalation paths if inferrable; honest about absence)",
    "blast_radius": "blast radius (what services break if this service has an outage or API change)",
    "incident_response": "incident response (step-by-step triage for the 3 most likely failure scenarios: down, wrong output, slow)",
    "key_configuration": "key configuration (critical env vars, feature flags, hardcoded thresholds — mark ⚠️ CRITICAL for anything that could cause an outage)",
    "dependencies": "dependencies (upstream services called; downstream consumers if known)",
    "ktlo_surface": "KTLO surface (operational ownership: on-call, alerting, SLO references)",
    "glossary": "glossary (3-10 entries of repo-specific jargon)",
    "common_questions": "Common questions (a Q+A section accumulated via promotions; condense / clean up while preserving the facts)",
}


def _section_regen_system_prompt(section_label: str) -> str:
    """Build the system prompt for a per-section regeneration.

    The model sees the existing handover body verbatim for context, and
    we ask it to produce ONLY the targeted section's Markdown body
    (without the ``## Header`` line — the splicer adds that). Keeping
    the response minimal lets us splice it back in cleanly without
    parsing nested h2s out of the model's output.
    """
    return f"""\
You are Bifrost's handover-doc author. The user has asked you to
rewrite ONLY the **{section_label}** section of an existing handover
document. The full current body is provided for context.

Rules:
  - Return JSON with a single field ``section_md`` containing the
    rewritten Markdown body of just that section.
  - Do NOT include the ``## <Header>`` line itself — your output is
    the section's body only. The splicer re-adds the header.
  - Keep the same scope and breadth as the existing section. Don't
    drift into other sections' territory; don't pad.
  - Preserve any concrete facts that are clearly grounded (entity
    names, link URLs, team names already cited). Don't invent new
    specifics that aren't in the existing body or other sections.
  - Be honest about uncertainty. Hallucinated specifics are worse
    than admitted gaps.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object: {{"section_md": "..."}}
"""


_SECTION_REGEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["section_md"],
    "properties": {
        "section_md": {"type": "string"},
    },
}


def _splice_section(
    *,
    body_md: str,
    section_header: str,
    new_section_md: str,
) -> str | None:
    """Replace the body of a ``## <section_header>`` block in ``body_md``
    with ``new_section_md`` (the body only — no ``## `` line).

    Finds the line ``## <section_header>`` (case-insensitive on the
    header *text* so user-typed headers like "## glossary" still
    match), then captures everything through the next ``^## ``
    boundary or end of file, and rewrites the captured range as
    ``## <section_header>\\n\\n<new_section_md>\\n\\n``.

    Returns the new body, or ``None`` if the section header isn't
    present (caller must decide whether to append, 404, etc.). The
    section header text is canonicalised in the output to the title
    we pass in — if the user had a slight variant ("## glossary "),
    the regen normalises it.
    """
    # Match the header line — exact case on first letter (it's a
    # title), but tolerate trailing whitespace + the user re-casing
    # the rest. The h2 must be alone on its line; sub-headings (h3+)
    # inside the section are preserved by the body-capture logic.
    header_re = re.compile(
        rf"^## {re.escape(section_header)}\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = header_re.search(body_md)
    if m is None:
        return None
    start = m.start()
    # Capture range: from the header line through the next ``## ``
    # heading (or EOF). The next-heading regex anchors on a newline
    # to avoid false-positives inside a code fence's body.
    next_h2 = re.compile(r"^## (?!#)", re.MULTILINE)
    next_match = next_h2.search(body_md, m.end())
    end = next_match.start() if next_match else len(body_md)
    # Normalise the new content: strip leading/trailing blank lines,
    # ensure a single trailing newline so subsequent splices land on
    # clean boundaries.
    new_body = (new_section_md or "").strip("\n").rstrip()
    # Demote any ``## `` (h2) lines
    # inside the LLM output to ``### `` (h3). The system prompt asks
    # the model not to emit h2s, but a hallucinated or model-error h2
    # would otherwise let a future per-section regen target the wrong
    # region (the regex captures from the *first* h2 forward, so an
    # injected h2 redirects subsequent regens). Demotion keeps the
    # heading intent while preserving section boundaries.
    new_body = re.sub(
        r"(?m)^## (?!#)",
        "### ",
        new_body,
    )
    replacement = f"## {section_header}\n\n{new_body}\n\n"
    return body_md[:start] + replacement + body_md[end:]


async def regenerate_section(
    *,
    doc_id: uuid.UUID,
    section_key: str,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> HandoverDoc:
    """Rewrite a single section of an existing handover doc in-place.

    Steps:

      1. Look up the doc + read its current body from disk.
      2. Validate ``section_key`` against the canonical vocabulary.
      3. Single Sonnet call (``settings.agent_model``, 120s timeout)
         that returns ``{section_md: ...}`` for the target section.
      4. Splice the returned content back into the body using the
         ``## <Header>``-through-next-``## `` pattern.
      5. Snapshot the OLD body via ``record_handover_body_snapshot``
         with ``event_type='pre-edit'`` (we reuse this enum value
         to avoid migrating the check constraint for a single
         new event type).
      6. Atomic write the new body (temp + rename).
      7. Bump ``last_validated_at`` only — this is targeted, not a
         full regen, so ``last_generated_at`` stays put.
      8. Emit an ``edited`` audit row with ``metadata={section: ...}``.

    Raises:
      ``ValueError`` for unknown ``doc_id`` / unknown ``section_key`` /
      missing fs_path / missing section in body / unreadable file.
      ``ClaudeCliError`` if the Sonnet call fails (route layer maps
      to a 500).
    """
    settings = settings or get_settings()

    if section_key not in _SECTION_KEY_TO_HEADER:
        raise ValueError(
            f"unknown section_key {section_key!r}; expected one of {sorted(_SECTION_KEY_TO_HEADER)}"
        )
    section_header = _SECTION_KEY_TO_HEADER[section_key]
    section_label = _SECTION_KEY_TO_PROMPT_LABEL[section_key]

    doc = await repo.get_handover_doc(doc_id)
    if doc is None:
        raise ValueError(f"handover doc {doc_id} not found")
    if not doc.fs_path:
        raise ValueError(f"handover doc {doc_id} has no fs_path; regenerate the whole doc first")

    fs_path = Path(doc.fs_path)
    try:
        existing_body = fs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"couldn't read handover body at {fs_path}: {exc}") from exc

    # Defensive sanity check before paying for a Sonnet call: ensure
    # the section header is actually present. Otherwise splicing
    # would fail anyway and we'd waste the round-trip.
    header_present = re.search(
        rf"^## {re.escape(section_header)}\s*$",
        existing_body,
        re.IGNORECASE | re.MULTILINE,
    )
    if not header_present:
        raise ValueError(
            f"section '## {section_header}' not found in handover body; "
            f"regenerate the whole doc first"
        )

    # Build the user message. We feed the full current body (capped
    # only by the model's context window — 200k tokens is plenty for
    # a single handover) so the model can match the existing scope
    # and idiom. ``wrap_untrusted`` keeps the body inside a marker
    # block so a malicious section in the existing doc can't override
    # the system prompt.
    user_message = "\n".join(
        [
            f"Section to rewrite: **{section_header}**",
            "",
            f"Scope: {doc.scope_kind} / {doc.scope_value}",
            "",
            "Current handover body (for context — match the breadth and "
            "tone of the existing sections):",
            wrap_untrusted(existing_body, "handover_body"),
            "",
            f"Rewrite the {section_label} section now. Return JSON with a "
            "single field ``section_md`` containing the new section body "
            "(no ``## Header`` line).",
        ]
    )

    # Cost log: one Sonnet call on a large body is roughly $0.05-$0.10;
    # no per-call ceiling is enforced, the estimate is only logged.
    logger.info(
        "handover section regen: doc=%s section=%s estimated_cost_usd≈0.05-0.10",
        doc.id,
        section_key,
    )

    client = ClaudeLlmClient(settings)
    completion = await client.call_with_json_schema(
        system_prompt=_section_regen_system_prompt(section_label),
        user_message=user_message,
        model=settings.agent_model,
        json_schema=_SECTION_REGEN_SCHEMA,
        max_turns=1,
        timeout_override=120,
    )
    new_section_md = str(completion.parsed.get("section_md") or "").strip()
    if not new_section_md:
        raise ValueError(f"LLM returned empty section_md for {section_key}")

    spliced = _splice_section(
        body_md=existing_body,
        section_header=section_header,
        new_section_md=new_section_md,
    )
    if spliced is None:
        # Shouldn't happen — we checked header_present above — but be
        # defensive in case the splicer's regex disagrees with the
        # presence-check (e.g. weird whitespace).
        raise ValueError(f"couldn't splice section '{section_header}' back into handover body")
    if not spliced.endswith("\n"):
        spliced = spliced + "\n"

    # Snapshot the prior body BEFORE the temp+replace overwrites it.
    # We reuse 'pre-edit': simpler than extending the check
    # constraint for a single new event type.
    await repo.record_handover_body_snapshot(
        doc_id=doc.id,
        body_md=existing_body,
        event_type="pre-edit",
    )

    # Atomic write — temp + rename. Mirrors ``patch_handover_doc_body``.
    tmp_path = fs_path.with_suffix(fs_path.suffix + ".tmp")
    try:
        tmp_path.write_text(spliced, encoding="utf-8")
        tmp_path.replace(fs_path)
    except OSError as exc:
        raise ValueError(f"couldn't write handover body at {fs_path}: {exc}") from exc

    # Bump validated-at only. ``last_generated_at`` stays put — this
    # is a targeted edit, not a full regen, and freshness is keyed on
    # source-data drift, not on individual section updates.
    from datetime import datetime

    doc.last_validated_at = datetime.now(UTC)
    await repo._session.flush()
    await repo._session.refresh(doc)

    # Audit row. ``event_type='edited'`` (reusing the existing enum
    # value) with ``section`` in metadata so
    # the activity panel can distinguish full-body edits from
    # section-level regens.
    await repo.record_handover_audit(
        doc_id=doc.id,
        event_type="edited",
        actor="system",
        metadata={
            "scope_kind": doc.scope_kind,
            "scope_value": doc.scope_value,
            "section": section_key,
            "section_header": section_header,
            "kind": "section-regen",
        },
    )

    logger.info(
        "handover section regen done: doc=%s section=%s body_len=%d",
        doc.id,
        section_key,
        len(spliced),
    )
    return doc


async def _upsert_doc_generic(
    *,
    repo: EntityRepository,
    scope_kind: str,
    scope_value: str,
    final_path: Path,
) -> HandoverDoc:
    """Create or update the HandoverDoc row for an arbitrary scope.

    Idempotent on (scope_kind, scope_value, parent_id=NULL): a second
    generate call updates the on-disk pointer + freshness in place.
    Used by the repo, domain, and team generators."""
    from datetime import datetime

    stmt = select(HandoverDoc).where(
        HandoverDoc.scope_kind == scope_kind,
        HandoverDoc.scope_value == scope_value,
        HandoverDoc.parent_id.is_(None),
    )
    existing = (await repo._session.execute(stmt)).scalar_one_or_none()

    now = datetime.now(UTC)
    if existing is None:
        doc = await repo.create_handover_doc(
            scope_kind=scope_kind,
            scope_value=scope_value,
            fs_path=str(final_path),
        )
        existing = doc
    else:
        existing.fs_path = str(final_path)

    existing.last_generated_at = now
    existing.last_validated_at = now
    existing.freshness_state = "fresh"
    await repo._session.flush()
    await repo._session.refresh(existing)

    # Emit a ``generated`` audit row. ``actor='system'``
    # because the row write is generator-initiated (the route layer
    # invoked the generator on the caller's behalf, but the audit row
    # itself isn't user-typed). Best-effort — the helper swallows
    # insert errors so a misbehaving audit layer can't break the
    # generation path.
    await repo.record_handover_audit(
        doc_id=existing.id,
        event_type="generated",
        actor="system",
        metadata={
            "scope_kind": scope_kind,
            "scope_value": scope_value,
        },
    )

    # A parent (multi-repo) handover just regenerated.
    # Bump ``last_validated_at`` on every child repo handover under it
    # so the UI's "last touched" stamp on the children reflects the
    # rollup. Repo-scope upserts are routed through ``_upsert_doc``
    # above (which calls the upward-propagation helper instead), so
    # we skip them here to avoid an unnecessary no-op round-trip.
    # Best-effort — never fail the main generation path on this.
    if scope_kind in ("domain", "team", "region"):
        try:
            await repo.mark_children_validated_by_parent(existing.id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "mark_children_validated_by_parent failed for scope=%s/%s doc_id=%s — continuing",
                scope_kind,
                scope_value,
                existing.id,
            )

    return existing


# ── Domain + team handover generators ─────────────
#
# Same single-Sonnet-call shape as the repo generator. The model is
# fed (a) repo-list metadata and (b) a capped, signal-density-bounded
# slice of entities pooled across all repos in the scope, and returns
# the same 7 sections. Where the prompt diverges: it emphasizes
# "you are summarizing a multi-repo unit — call out which repos
# contribute which capabilities" so the doc doesn't read like a
# single-repo handover with a wrong title.


_DOMAIN_HANDOVER_SYSTEM_PROMPT = f"""\
You are Bifrost's domain-scoped handover-doc author. A *domain* is a
named grouping of related repositories owned by the same broader
team / business area. Given metadata for the domain, the list of
repos that belong to it, and a representative slice of the extracted
knowledge graph pooled across those repos, write a handover document
that helps an external engineer (a) understand what this domain owns
in aggregate, (b) know which repo handles which capability, and (c)
frame their own asks of the team.

Produce these sections (returned as separate JSON fields):

  title              — 1 line. Format: "<Domain name> — domain handover".
  executive_summary  — 2-3 paragraphs. What does this domain do, end
                       to end? Which repos make up the domain and
                       what role does each play?
  architecture       — Markdown. Top-level breakdown across the
                       domain's repos. Identify which repo owns which
                       chunk of the system. Mermaid ``flowchart TD``
                       is encouraged when the cross-repo structure is
                       clear; an unfounded diagram is worse than
                       none.
  critical_flows     — Markdown. List the top 3-7 user-visible /
                       business-critical flows that span the domain.
                       Where a flow crosses repos, name the repos in
                       the step. If the entity graph doesn't surface
                       enough flow-typed entities, say so honestly.
  ownership          — Markdown. Names / teams / escalation paths
                       only if inferrable from the entity metadata.
                       If absent, write "Ownership not surfaced in
                       extraction; check the manifest + CODEOWNERS in
                       each repo".
  dependencies       — Markdown. Upstream services this domain calls
                       (collectively); downstream consumers if known.
                       Group by repo where the dependency origin
                       matters.
  ktlo_surface       — Markdown. Operational ownership signals
                       pooled across the domain — on-call hooks,
                       alerting integrations, SLO references found
                       in the entities. Be conservative.
  glossary           — Markdown. Define domain-specific terms an
                       outsider would reasonably trip over. 3-10
                       entries; skip if the domain doesn't have
                       jargon.

Style rules:
  - You are describing a DOMAIN that spans multiple repos. Call out
    which repos contribute which capabilities — don't merge them
    into an undifferentiated blob.
  - Be concrete. Cite entity names verbatim where relevant.
  - Be honest about uncertainty. Hallucinated specifics are worse
    than admitted gaps.
  - Don't invent team names, contact names, or external system
    names that aren't in the source data.
  - Keep each section tight, readable Markdown.
  - Cite Confluence page titles + URLs verbatim from `<confluence_context>`
    blocks where they support a section. Don't invent links.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object specified by the schema.
"""


_TEAM_HANDOVER_SYSTEM_PROMPT = f"""\
You are Bifrost's team-scoped handover-doc author. A *team*
(squad) is the org-chart unit that owns one or more repos in the
ownership catalog. Given the squad name, the list of repos owned by
the squad, and a representative slice of the extracted knowledge
graph pooled across those repos, write a handover document that
helps an external engineer (a) understand what this squad owns,
(b) know which repo handles which capability, and (c) frame their
own asks of the team.

Produce these sections (returned as separate JSON fields):

  title              — 1 line. Format: "<Squad> — team handover".
  executive_summary  — 2-3 paragraphs. What does this squad own end
                       to end? Which repos do they maintain, and
                       what role does each play in the squad's
                       remit?
  architecture       — Markdown. Top-level breakdown across the
                       squad's repos. Identify which repo owns which
                       chunk of the system. Mermaid ``flowchart TD``
                       is encouraged when the cross-repo structure is
                       clear; an unfounded diagram is worse than
                       none.
  critical_flows     — Markdown. List the top 3-7 user-visible /
                       business-critical flows that the squad runs.
                       Where a flow crosses repos, name the repos in
                       the step. If the entity graph doesn't surface
                       enough flow-typed entities, say so honestly.
  ownership          — Markdown. The squad name itself is the
                       primary owner. Name on-call rotation /
                       escalation paths only if inferrable from the
                       entity metadata; otherwise say "On-call /
                       escalation not surfaced in extraction; check
                       the manifest in each repo".
  dependencies       — Markdown. Upstream services this squad's
                       repos call (collectively); downstream
                       consumers if known. Group by repo where the
                       dependency origin matters.
  ktlo_surface       — Markdown. Operational ownership signals
                       pooled across the squad's repos. Be
                       conservative.
  glossary           — Markdown. Define squad-specific terms an
                       outsider would reasonably trip over. 3-10
                       entries; skip if the squad doesn't have
                       jargon.

Style rules:
  - You are describing a TEAM (squad) that owns multiple repos.
    Call out which repos contribute which capabilities — don't
    merge them into an undifferentiated blob.
  - Be concrete. Cite entity names verbatim where relevant.
  - Be honest about uncertainty. Hallucinated specifics are worse
    than admitted gaps.
  - Don't invent team-member names, contact names, or external
    system names that aren't in the source data.
  - Keep each section tight, readable Markdown.
  - Cite Confluence page titles + URLs verbatim from `<confluence_context>`
    blocks where they support a section. Don't invent links.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object specified by the schema.
"""


_REGION_HANDOVER_SYSTEM_PROMPT = f"""\
You are Bifrost's region-scoped handover-doc author. A *region* is a
geographic region (e.g. "us", "eu") of repos served
by the organisation. Given the region name, the list of repos in the region,
and a representative slice of the extracted knowledge graph pooled
across those repos, write a handover document that helps an
external engineer (a) understand what this region owns end to end,
(b) know which repo handles which capability, and (c) frame their
own asks of the teams that operate in this region.

Produce these sections (returned as separate JSON fields):

  title              — 1 line. Format: "<Region> — region handover".
  executive_summary  — 2-3 paragraphs. What does this region cover
                       end to end? Which repos serve customers in
                       this region, and what role does each play in
                       the region's surface area?
  architecture       — Markdown. Top-level breakdown across the
                       region's repos. Identify which repo owns which
                       chunk of the system. Mermaid ``flowchart TD``
                       is encouraged when the cross-repo structure is
                       clear; an unfounded diagram is worse than
                       none.
  critical_flows     — Markdown. List the top 3-7 user-visible /
                       business-critical flows that the region
                       supports. Where a flow crosses repos, name
                       the repos in the step. If the entity graph
                       doesn't surface enough flow-typed entities,
                       say so honestly.
  ownership          — Markdown. The region itself isn't an
                       org-chart unit — multiple squads typically
                       contribute to a region. Roll up squad names
                       from the entity metadata where surfaced. If
                       absent, write "Per-repo ownership not
                       surfaced; check the manifest in each repo".
  dependencies       — Markdown. Upstream services this region's
                       repos call (collectively); downstream
                       consumers if known. Group by repo where the
                       dependency origin matters.
  ktlo_surface       — Markdown. Operational ownership signals
                       pooled across the region's repos. Be
                       conservative.
  glossary           — Markdown. Define region-specific terms an
                       outsider would reasonably trip over (e.g.
                       local terminology, data-residency
                       rules). 3-10 entries; skip if the region
                       doesn't have jargon.

Style rules:
  - You are describing a REGION that spans multiple repos, often
    multiple squads. Call out which repos contribute which
    capabilities — don't merge them into an undifferentiated blob.
  - Be concrete. Cite entity names verbatim where relevant.
  - Be honest about uncertainty. Hallucinated specifics are worse
    than admitted gaps.
  - Don't invent team-member names, contact names, regulator
    names, or external system names that aren't in the source data.
  - Keep each section tight, readable Markdown.
  - Cite Confluence page titles + URLs verbatim from `<confluence_context>`
    blocks where they support a section. Don't invent links.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object specified by the schema.
"""


_PARENT_COMPOSER_SYSTEM_PROMPT = f"""\
You are Bifrost's parent-handover composer. You are looking at N
per-repo handover documents that have ALREADY been written by the
repo-scope generator. Your job is to compose a parent (domain or
team) handover that ABSTRACTS UP from them.

Produce the same 7 sections (returned as separate JSON fields):

  title              — 1 line. Format: "<Scope> — <kind> handover".
  executive_summary  — Paragraph(s). What does this scope DO at the
                       business / engineering level? Don't restate
                       per-repo details; talk about the scope's
                       collective mission.
  architecture       — Markdown. Cross-repo system view. Identify
                       which repo plays which role and how they
                       interact. Mermaid ``flowchart TD`` showing
                       repo-as-nodes is encouraged when the
                       structure is clear from the children.
  critical_flows     — Markdown. End-to-end flows that traverse
                       multiple repos. Cite the contributing repo
                       for each step.
  ownership          — Markdown. Roll up ownership signals from the
                       children. If different repos surface different
                       owners, list each. Don't average / merge.
  dependencies       — Markdown. Outside-the-scope dependencies
                       (services this scope calls into; consumers
                       that call into it). Children may have listed
                       INTRA-scope deps; filter those out — they're
                       intra-architecture, not external dependencies.
  ktlo_surface       — Markdown. Compose KTLO signals across the
                       children. If only some repos have alerting
                       and the rest don't, say so honestly.
  glossary           — Markdown. Terms that appear consistently
                       across multiple children — those are the
                       scope-level vocabulary. Skip anything
                       repo-specific.

Style rules:
  - DO NOT just concatenate child sections. Abstract up.
  - DO cite repo names verbatim when their contribution is specific.
  - Be honest when children don't agree or when some children are
    stubs (no extracted entities). The parent should reflect the
    real coverage state, not pretend.
  - Don't invent team / contact / system names absent from the
    children.
  - Cite Confluence page titles + URLs verbatim from `<confluence_context>`
    blocks where they support a section. Don't invent links.

{BOUNDARY_SENTENCE}

Respond with ONLY the JSON object specified by the schema.
"""


def _format_repo_list(repos: list[Repo]) -> str:
    """Render a repo-list table for the prompt preface."""
    if not repos:
        return "(no repos)"
    lines: list[str] = []
    for r in repos:
        url = r.url or "(no url)"
        desc = (r.description or "").strip()
        if desc:
            desc = desc.splitlines()[0][:200]
            lines.append(f"  - {r.name}  ({url})  — {desc}")
        else:
            lines.append(f"  - {r.name}  ({url})")
    return "\n".join(lines)


async def _spawn_repo_handovers_in_parallel(
    repos_in_scope: list[Repo],
    repo: EntityRepository,
    settings: BifrostSettings,
) -> list[tuple[Repo, str]]:
    """For each repo in scope, ensure a fresh repo-scope handover
    exists, generating in parallel under a concurrency semaphore.

    Returns ``(Repo, body_md)`` pairs for repos whose handover was
    successfully generated AND whose body could be read back from
    disk. Failed children (LLM error, no entities, etc.) are dropped
    from the list — the parent composer is told via the count
    delta when fewer body_mds came back than repos went in.

    The Semaphore caps concurrent CLI subprocesses so a 100-repo
    domain doesn't fork the host into a coma. Cap is ``_CONCURRENT_CHILD_CAP``."""
    import asyncio

    if not repos_in_scope:
        return []

    sem = asyncio.Semaphore(_CONCURRENT_CHILD_CAP)

    async def _run(repo_row: Repo) -> tuple[Repo, str | None]:
        async with sem:
            try:
                result = await generate_repo_handover(
                    repo_id=repo_row.id,
                    repo=repo,
                    settings=settings,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "child handover failed for repo=%s; skipping",
                    repo_row.name,
                )
                return repo_row, None
            try:
                body = Path(result.fs_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                logger.warning(
                    "couldn't read child handover at %s for repo=%s",
                    result.fs_path,
                    repo_row.name,
                )
                return repo_row, None
            return repo_row, body

    pairs = await asyncio.gather(
        *(_run(r) for r in repos_in_scope),
        return_exceptions=False,
    )
    return [(r, b) for r, b in pairs if b]


def _format_child_handovers_for_compose(
    pairs: list[tuple[Repo, str]],
) -> str:
    """Render child handovers as boundary-wrapped blocks for the
    parent-composer prompt. Each child body is truncated to
    ``_CHILD_BODY_CHAR_CAP`` to keep the cumulative prompt bounded
    even with many children."""
    parts: list[str] = []
    for r, body in pairs:
        snippet = (
            body
            if len(body) <= _CHILD_BODY_CHAR_CAP
            else (
                body[:_CHILD_BODY_CHAR_CAP]
                + f"\n\n[truncated — full child handover for `{r.name}` on disk]"
            )
        )
        parts.append(
            wrap_untrusted(
                snippet,
                "child_handover",
                attrs={"repo_name": r.name, "repo_url": r.url or ""},
            ),
        )
    return "\n\n".join(parts)


async def _pull_entities_for_repos(
    repo: EntityRepository,
    repo_ids: list[uuid.UUID],
    *,
    per_repo_cap: int = _PER_TYPE_CAP,
    total_cap: int = _TOTAL_ENTITY_CAP,
) -> list[Entity]:
    """Pull entities across multiple repos with a per-repo cap and a
    pooled total cap. Tier-1 entities first, then by name. Used by the
    domain + team generators to keep the prompt bounded when a
    scope spans many repos."""
    if not repo_ids:
        return []

    pooled: list[Entity] = []
    for rid in repo_ids:
        if len(pooled) >= total_cap:
            break
        per_repo_remaining = max(0, total_cap - len(pooled))
        cap = min(per_repo_cap, per_repo_remaining)
        if cap <= 0:
            break
        stmt = (
            select(Entity)
            .where(Entity.scope_repo_id == rid)
            .order_by(Entity.knowledge_tier, Entity.name)
            .limit(cap)
        )
        rows = list((await repo._session.execute(stmt)).scalars().all())
        pooled.extend(rows)
    return pooled


# Auto-derived cross-repo flow diagram.
#
# The model still authors the architecture prose, but we prepend a
# deterministic Mermaid ``flowchart LR`` derived from real
# ``EntityRelationship`` rows whose source + target entities live in
# DIFFERENT repos within the scope. This guarantees the diagram is
# correct (it can't hallucinate edges that don't exist in the graph)
# while leaving the model free to explain context the data alone
# can't convey.
async def _build_cross_repo_flow_diagram(
    repo: EntityRepository,
    repo_ids: list[uuid.UUID],
) -> str | None:
    """Build a Mermaid ``flowchart LR`` of cross-repo edges in scope.

    Pulls ``EntityRelationship`` rows whose source and target entities
    both have a ``scope_repo_id`` in ``repo_ids`` AND those ids differ.
    Aggregates by (source_repo_name, target_repo_name, rel_type),
    counts edges, keeps the top-``_CROSS_REPO_DIAGRAM_EDGE_CAP`` by
    count, and renders the result as a Mermaid block. Each cross-repo
    edge is rendered with its rel_type and count as a label, e.g.
    ``A -->|calls (3)| B``.

    Returns ``None`` if no cross-repo edges are found or if the rendered
    block fails ``_validate_mermaid_block``. The caller MUST prepend
    only on a non-None return value.

    The query is bounded by ``repo_ids`` on both ends and by the
    aggregate cap, so it stays fast even on large repo sets.
    """
    if not repo_ids or len(repo_ids) < 2:
        # Single repo (or empty) can't have cross-repo edges by construction.
        return None

    src = aliased(Entity, name="src")
    tgt = aliased(Entity, name="tgt")
    src_repo = aliased(Repo, name="src_repo")
    tgt_repo = aliased(Repo, name="tgt_repo")

    edge_count = func.count(EntityRelationship.id).label("edge_count")
    stmt = (
        select(
            src_repo.name.label("src_repo_name"),
            tgt_repo.name.label("tgt_repo_name"),
            EntityRelationship.rel_type,
            edge_count,
        )
        .join(src, src.id == EntityRelationship.source_id)
        .join(tgt, tgt.id == EntityRelationship.target_id)
        .join(src_repo, src_repo.id == src.scope_repo_id)
        .join(tgt_repo, tgt_repo.id == tgt.scope_repo_id)
        .where(src.scope_repo_id.in_(repo_ids))
        .where(tgt.scope_repo_id.in_(repo_ids))
        .where(src.scope_repo_id != tgt.scope_repo_id)
        .group_by(
            src_repo.name,
            tgt_repo.name,
            EntityRelationship.rel_type,
        )
        .order_by(edge_count.desc())
        .limit(_CROSS_REPO_DIAGRAM_EDGE_CAP)
    )

    try:
        rows = list((await repo._session.execute(stmt)).all())
    except Exception:  # noqa: BLE001 — never fail the handover for a diagram pull
        logger.exception("cross-repo diagram pull failed; continuing without it")
        return None

    if not rows:
        return None

    # Build a stable node id per repo name. Mermaid identifiers are
    # restricted (alphanumerics + underscore safest), so we slugify on
    # the fly. The display label is the original repo name in quotes.
    node_ids: dict[str, str] = {}

    def _node_id(name: str) -> str:
        if name in node_ids:
            return node_ids[name]
        slug = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "repo"
        # Disambiguate clashes (e.g. two names that slug identically).
        candidate = slug
        i = 2
        used = set(node_ids.values())
        while candidate in used:
            candidate = f"{slug}_{i}"
            i += 1
        node_ids[name] = candidate
        return candidate

    lines: list[str] = ["flowchart LR"]
    # Emit nodes first so each repo gets a labeled box even if it only
    # appears on one side of an edge in the top-N slice.
    seen_nodes: set[str] = set()
    for src_name, tgt_name, _rel_type, _count in rows:
        for name in (src_name, tgt_name):
            if name in seen_nodes:
                continue
            seen_nodes.add(name)
            nid = _node_id(name)
            # Quote the label so repo names with dashes / dots render as one box.
            lines.append(f'    {nid}["{name}"]')

    # Emit edges. Mermaid edge labels live between ``-->|`` and ``|``;
    # we keep the rel_type readable (lowercased, underscores → spaces)
    # and append the count so the reader sees edge weight at a glance.
    for src_name, tgt_name, rel_type, count in rows:
        s_id = _node_id(src_name)
        t_id = _node_id(tgt_name)
        # Sanitize rel_type for the label: pipe chars and quotes would break the fence.
        clean_rel = re.sub(r"[|\"]", "", str(rel_type or "rel")).strip() or "rel"
        label = f"{clean_rel} ({count})"
        lines.append(f"    {s_id} -->|{label}| {t_id}")

    diagram = "\n".join(lines)
    if not _validate_mermaid_block(diagram):
        # Should be rare — our renderer is conservative — but the
        # contract is "never ship a broken diagram". Drop on failure.
        logger.warning(
            "cross-repo flow diagram failed mermaid shape validation; skipping",
        )
        return None
    return diagram


def _prepend_cross_repo_diagram(parsed: dict[str, Any], diagram: str) -> None:
    """Prepend ``diagram`` (a validated Mermaid body) plus a one-line
    caption to ``parsed['architecture']`` in-place.

    The block is wrapped in a ```` ```mermaid ``` ```` fence so the
    downstream ``_sanitize_mermaid_blocks`` pass can re-validate it
    alongside any model-authored fences. The caption explains the
    diagram is deterministic so readers don't conflate it with the
    model's hand-authored prose.
    """
    caption = "_Cross-repo dependencies derived from extracted entity relationships._"
    fenced = f"```mermaid\n{diagram}\n```"
    existing = str(parsed.get("architecture") or "").strip()
    if existing:
        parsed["architecture"] = f"{fenced}\n\n{caption}\n\n{existing}"
    else:
        parsed["architecture"] = f"{fenced}\n\n{caption}"


async def _pull_confluence_for_scope(
    repo: EntityRepository,
    scope_kind: str,
    scope_value: str,
    repo_names: list[str],
) -> list[ConfluencePage]:
    """Pull cached Confluence pages most relevant to this handover scope.

    Strategy (case-insensitive substring matching against ``space_key`` and
    ``title``):

      * **repo scope** — pages whose space key or title contains the repo
        name. Most-recent-first.
      * **domain / team / region scope** — pages whose space key or
        title matches ANY repo in scope; pages whose title cross-references
        multiple repos are boosted to the front (those are the
        cross-cutting docs a multi-repo handover most wants to cite).

    Bounded to ``_CONFLUENCE_PAGE_CAP`` pages overall. Failures (DB error,
    empty cache, etc.) return an empty list — the caller MUST treat the
    Confluence block as best-effort and continue without it on failure.
    """
    names = [n for n in (rn.strip() for rn in repo_names) if n]
    if not names:
        return []
    names_lower = [n.lower() for n in names]

    try:
        # Pull a generous candidate slate, then filter + rank in Python.
        # We over-fetch (cap * 20) so that even a noisy cache where most
        # pages don't match still has enough genuine matches to fill the
        # final cap. last_modified is the natural recency signal.
        stmt = (
            select(ConfluencePage)
            .order_by(ConfluencePage.last_modified.desc().nulls_last())
            .limit(_CONFLUENCE_PAGE_CAP * 20)
        )
        rows = list((await repo._session.execute(stmt)).scalars().all())
    except Exception:  # noqa: BLE001 — never fail the handover for a Confluence pull
        logger.exception(
            "confluence pull failed for scope=%s/%s; continuing without it",
            scope_kind,
            scope_value,
        )
        return []

    if not rows:
        return []

    def _match_count(page: ConfluencePage) -> int:
        """Number of in-scope repo names this page references."""
        haystack_parts = [
            (page.title or "").lower(),
            (page.space_key or "").lower(),
        ]
        haystack = " | ".join(haystack_parts)
        return sum(1 for n in names_lower if n and n in haystack)

    matched: list[tuple[int, ConfluencePage]] = []
    for page in rows:
        hits = _match_count(page)
        if hits == 0:
            continue
        matched.append((hits, page))

    if not matched:
        return []

    # Cross-reference boost: for multi-repo scopes, pages mentioning
    # 2+ repos sort before single-repo pages. Within each tier we keep
    # the recency order from the SQL query (Python sort is stable).
    if scope_kind != "repo":
        matched.sort(key=lambda pair: -pair[0])

    return [page for _, page in matched[:_CONFLUENCE_PAGE_CAP]]


def _format_confluence_block(pages: list[ConfluencePage]) -> list[str]:
    """Render Confluence pages as a list of prompt lines.

    Returns ``[]`` when ``pages`` is empty so callers can splice the
    result directly into their ``user_message`` parts list. Each page
    body is wrapped in ``<confluence_context>`` with the page title +
    URL as attributes so the model can cite them verbatim.
    """
    if not pages:
        return []
    parts: list[str] = [
        f"Related Confluence pages ({len(pages)}):",
    ]
    for p in pages:
        # ``ConfluencePage`` exposes ``body_excerpt`` (the indexer's
        # snapshot) and ``summary``. Prefer the longer excerpt; fall
        # back to the summary; never ship empty content (the wrapper
        # would still emit the boundary, but the model gets nothing
        # useful).
        body = (p.body_excerpt or "").strip() or (p.summary or "").strip()
        if not body:
            continue
        truncated = body[:_CONFLUENCE_BODY_CHAR_CAP]
        attrs = {
            "title": (p.title or "")[:200],
            "url": p.url or "",
        }
        parts.append(
            wrap_untrusted(truncated, "confluence_context", attrs=attrs),
        )
    # If every candidate had empty bodies, drop the lead-in too.
    if len(parts) == 1:
        return []
    return parts


def _render_multi_repo_handover_md(
    *,
    title_fallback: str,
    scope_label: str,
    scope_kind_label: str,
    repos: list[Repo],
    parsed: dict[str, Any],
) -> str:
    """Compose Markdown for a multi-repo handover (domain or team).

    Adds an explicit "Repos in scope" preamble so the reader can see
    the boundary at a glance, then renders the same 7 sections as
    the repo-scope generator.
    """
    title = str(parsed.get("title") or title_fallback).strip()
    repo_lines = []
    for r in repos:
        if r.url:
            repo_lines.append(f"- [`{r.name}`]({r.url})")
        else:
            repo_lines.append(f"- `{r.name}`")
    repos_md = "\n".join(repo_lines) if repo_lines else "_(no repos in scope)_"

    # scope_kind_label is human ("Domain", "Team", "Region"); normalize for FM.
    fm_scope_kind = scope_kind_label.strip().lower() or "scope"
    fm = _handover_frontmatter(
        scope_kind=fm_scope_kind,
        scope_value=scope_label,
        title=title,
    )
    md = fm + "\n".join(
        [
            f"# {title}",
            "",
            f"_{scope_kind_label}:_ `{scope_label}`",
            "",
            "## Repos in scope",
            repos_md,
            "",
            "## Executive summary",
            str(parsed.get("executive_summary", "")).strip(),
            "",
            "## Architecture",
            str(parsed.get("architecture", "")).strip(),
            "",
            "## Critical flows",
            str(parsed.get("critical_flows", "")).strip(),
            "",
            "## Ownership",
            str(parsed.get("ownership", "")).strip(),
            "",
            "## Dependencies",
            str(parsed.get("dependencies", "")).strip(),
            "",
            "## KTLO surface",
            str(parsed.get("ktlo_surface", "")).strip(),
            "",
            "## Glossary",
            str(parsed.get("glossary", "")).strip(),
            "",
        ]
    )
    return _sanitize_mermaid_blocks(md, parsed)


def _stub_multi_repo_doc_text(
    *,
    title: str,
    scope_kind_label: str,
    scope_label: str,
    repos: list[Repo],
    reason: str,
) -> str:
    repo_lines = []
    for r in repos:
        if r.url:
            repo_lines.append(f"- [`{r.name}`]({r.url})")
        else:
            repo_lines.append(f"- `{r.name}`")
    repos_md = "\n".join(repo_lines) if repo_lines else "_(no repos in scope)_"
    return "\n".join(
        [
            f"# {title}",
            "",
            f"_{scope_kind_label}:_ `{scope_label}`",
            "",
            "## Repos in scope",
            repos_md,
            "",
            "## Status",
            reason,
            "",
        ]
    )


async def generate_domain_handover(
    *,
    domain_id: uuid.UUID,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> HandoverGenerationResult:
    """Generate (or regenerate) the handover doc for a domain.

    Pulls all repos linked via ``repos.domain_id == domain_id``, then
    pools entities across them (capped) into a single Sonnet call.
    Idempotent on (scope_kind='domain', scope_value=<domain.name>,
    parent_id=NULL).

    Raises ``ValueError`` if the domain doesn't exist; surfaces
    ``HandoverGenerationResult.used_fallback=True`` if the LLM call
    fails or no repos are linked yet."""
    settings = settings or get_settings()

    domain_row = await repo._session.get(Domain, domain_id)
    if domain_row is None:
        raise ValueError(f"domain {domain_id} not found")

    # Pull repos linked to this domain.
    repo_stmt = select(Repo).where(Repo.domain_id == domain_id).order_by(Repo.name)
    repos_in_scope = list((await repo._session.execute(repo_stmt)).scalars().all())

    out_dir = _handovers_dir() / "domains" / _slugify(domain_row.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "index.md"

    if not repos_in_scope:
        logger.warning(
            "domain handover: domain=%s has no linked repos; stub doc only",
            domain_row.name,
        )
        title = f"{domain_row.name} — domain handover (stub)"
        final_path.write_text(
            _stub_multi_repo_doc_text(
                title=title,
                scope_kind_label="Domain",
                scope_label=domain_row.name,
                repos=[],
                reason=(
                    "This domain has no registered repos yet. Register repos "
                    "and link them via `repos.domain_id`, then regenerate."
                ),
            ),
            encoding="utf-8",
        )
        doc = await _upsert_doc_generic(
            repo=repo,
            scope_kind="domain",
            scope_value=domain_row.name,
            final_path=final_path,
        )
        return HandoverGenerationResult(
            doc_id=doc.id,
            fs_path=str(final_path),
            title=title,
            used_fallback=True,
        )

    use_tree = len(repos_in_scope) > _TREE_THRESHOLD

    await publish_handover_event(
        "domain",
        domain_row.name,
        "scope_resolved",
        {
            "repos": [r.name for r in repos_in_scope],
            "use_tree": use_tree,
        },
    )

    if use_tree:
        # Tree decomposition: spawn per-repo child handovers
        # in parallel, then compose the parent from their bodies.
        await publish_handover_event(
            "domain",
            domain_row.name,
            "child_started",
            {"count": len(repos_in_scope)},
        )
        child_pairs = await _spawn_repo_handovers_in_parallel(
            repos_in_scope,
            repo,
            settings,
        )
        await publish_handover_event(
            "domain",
            domain_row.name,
            "child_done",
            {
                "succeeded": len(child_pairs),
                "total": len(repos_in_scope),
            },
        )
        if not child_pairs:
            logger.warning(
                "domain handover (tree): domain=%s — all child generations failed; stub doc only",
                domain_row.name,
            )
            title = f"{domain_row.name} — domain handover (stub)"
            final_path.write_text(
                _stub_multi_repo_doc_text(
                    title=title,
                    scope_kind_label="Domain",
                    scope_label=domain_row.name,
                    repos=repos_in_scope,
                    reason=(
                        "All per-repo handovers failed to generate. Inspect "
                        "the server log for child errors, then regenerate."
                    ),
                ),
                encoding="utf-8",
            )
            doc = await _upsert_doc_generic(
                repo=repo,
                scope_kind="domain",
                scope_value=domain_row.name,
                final_path=final_path,
            )
            return HandoverGenerationResult(
                doc_id=doc.id,
                fs_path=str(final_path),
                title=title,
                used_fallback=True,
            )

        children_md = _format_child_handovers_for_compose(child_pairs)
        confluence_pages = await _pull_confluence_for_scope(
            repo,
            "domain",
            domain_row.name,
            [r.name for r in repos_in_scope],
        )
        parts: list[str] = [
            "Domain:",
            wrap_untrusted(
                f"name: {domain_row.name}\n"
                f"description: {domain_row.description or '(none)'}\n"
                f"repo_count: {len(repos_in_scope)}\n"
                f"children_with_bodies: {len(child_pairs)}",
                "domain_metadata",
            ),
            "",
            f"Repos in this domain ({len(repos_in_scope)}):",
            wrap_untrusted(_format_repo_list(repos_in_scope), "repo_list"),
            "",
            f"Per-repo handovers ({len(child_pairs)} successfully generated; "
            "each is a real artifact you can cite):",
            children_md,
            "",
        ]
        confluence_block = _format_confluence_block(confluence_pages)
        if confluence_block:
            parts.extend(confluence_block)
            parts.append("")
        parts.append(
            "Compose the DOMAIN handover by abstracting up from the children. "
            "Return the JSON object exactly per the schema."
        )
        user_message = "\n".join(parts)
        composer_prompt = _PARENT_COMPOSER_SYSTEM_PROMPT
        n_signal = len(child_pairs)
        signal_label = f"child_handovers={n_signal}"
    else:
        # Pooled-entity path — kept for small scopes
        # where the multi-call overhead isn't worth it.
        repo_ids = [r.id for r in repos_in_scope]
        entities = await _pull_entities_for_repos(repo, repo_ids)
        entity_summary, counts = _summarize_entities(entities)

        if not entities:
            logger.warning(
                "domain handover: domain=%s has 0 indexed entities across %d repos; stub doc only",
                domain_row.name,
                len(repos_in_scope),
            )
            title = f"{domain_row.name} — domain handover (stub)"
            final_path.write_text(
                _stub_multi_repo_doc_text(
                    title=title,
                    scope_kind_label="Domain",
                    scope_label=domain_row.name,
                    repos=repos_in_scope,
                    reason=(
                        "Repos are registered but no entities have been extracted "
                        "yet. Run extractions for the linked repos, then "
                        "regenerate."
                    ),
                ),
                encoding="utf-8",
            )
            doc = await _upsert_doc_generic(
                repo=repo,
                scope_kind="domain",
                scope_value=domain_row.name,
                final_path=final_path,
            )
            return HandoverGenerationResult(
                doc_id=doc.id,
                fs_path=str(final_path),
                title=title,
                used_fallback=True,
            )

        confluence_pages = await _pull_confluence_for_scope(
            repo,
            "domain",
            domain_row.name,
            [r.name for r in repos_in_scope],
        )
        parts: list[str] = [
            "Domain:",
            wrap_untrusted(
                f"name: {domain_row.name}\n"
                f"description: {domain_row.description or '(none)'}\n"
                f"repo_count: {len(repos_in_scope)}",
                "domain_metadata",
            ),
            "",
            f"Repos in this domain ({len(repos_in_scope)}):",
            wrap_untrusted(_format_repo_list(repos_in_scope), "repo_list"),
            "",
            f"Entity counts (per type, pooled across the domain): {counts}",
            "",
            "Representative entities (sampled across all repos, tier-1 first):",
            wrap_untrusted(entity_summary, "entity_slice"),
            "",
        ]
        confluence_block = _format_confluence_block(confluence_pages)
        if confluence_block:
            parts.extend(confluence_block)
            parts.append("")
        parts.append(
            "Produce the domain handover doc now. Return the JSON object exactly "
            "per the schema. Remember: this is a DOMAIN handover — call out which "
            "repos contribute which capabilities."
        )
        user_message = "\n".join(parts)
        composer_prompt = _DOMAIN_HANDOVER_SYSTEM_PROMPT
        n_signal = len(entities)
        signal_label = f"entities={n_signal}"

    client = ClaudeLlmClient(settings)
    await publish_handover_event(
        "domain",
        domain_row.name,
        "compose_started",
        {"signal": signal_label, "tree": use_tree},
    )
    try:
        completion = await client.call_with_json_schema(
            system_prompt=composer_prompt,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_HANDOVER_SCHEMA,
            max_turns=1,
            timeout_override=300,  # bigger scope, more sections to write.
        )
        parsed = completion.parsed
    except ClaudeCliError as exc:
        logger.warning(
            "handover generator LLM failed for domain=%s: %s",
            domain_row.name,
            exc,
        )
        await publish_handover_event(
            "domain",
            domain_row.name,
            "error",
            {"reason": str(exc)},
        )
        return HandoverGenerationResult(
            doc_id=uuid.UUID(int=0),
            fs_path="",
            title="",
            used_fallback=True,
        )

    title = str(parsed.get("title") or f"{domain_row.name} — domain handover").strip()
    await publish_handover_event(
        "domain",
        domain_row.name,
        "compose_done",
        {"title": title},
    )

    # Prepend a deterministic cross-repo flow diagram derived
    # from real entity relationships. Done BEFORE _sanitize_mermaid_blocks
    # runs (inside _render_multi_repo_handover_md) so the auto-derived
    # fence also gets the safety pass.
    diagram = await _build_cross_repo_flow_diagram(
        repo,
        [r.id for r in repos_in_scope],
    )
    if diagram is not None:
        _prepend_cross_repo_diagram(parsed, diagram)

    # Snapshot prior body before temp+replace.
    await _snapshot_existing_body_pre_regen(
        repo=repo,
        scope_kind="domain",
        scope_value=domain_row.name,
    )
    tmp_path = out_dir / "index.md.tmp"
    tmp_path.write_text(
        _render_multi_repo_handover_md(
            title_fallback=f"{domain_row.name} — domain handover",
            scope_label=domain_row.name,
            scope_kind_label="Domain",
            repos=repos_in_scope,
            parsed=parsed,
        ),
        encoding="utf-8",
    )
    tmp_path.replace(final_path)
    await publish_handover_event(
        "domain",
        domain_row.name,
        "fs_written",
        {"fs_path": str(final_path)},
    )

    doc = await _upsert_doc_generic(
        repo=repo,
        scope_kind="domain",
        scope_value=domain_row.name,
        final_path=final_path,
    )

    logger.info(
        "handover generated domain=%s doc_id=%s path=%s repos=%d %s tree=%s",
        domain_row.name,
        doc.id,
        final_path,
        len(repos_in_scope),
        signal_label,
        use_tree,
    )
    await publish_handover_event(
        "domain",
        domain_row.name,
        "complete",
        {
            "doc_id": str(doc.id),
            "fs_path": str(final_path),
            "title": title,
            "used_fallback": False,
        },
    )
    return HandoverGenerationResult(
        doc_id=doc.id,
        fs_path=str(final_path),
        title=title,
    )


async def generate_team_handover(
    *,
    squad: str,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> HandoverGenerationResult:
    """Generate (or regenerate) the handover doc for a team
    (squad) scope.

    Pulls org_repos rows where ``squad == squad`` that have been
    linked to a registered Repo (via ``registered_repo_id``), then
    pools entities across those repos. Idempotent on
    (scope_kind='team', scope_value=<squad>, parent_id=NULL).

    Raises ``ValueError`` if the squad string is empty; surfaces
    ``HandoverGenerationResult.used_fallback=True`` if no registered
    repos exist for the squad or the LLM call fails."""
    settings = settings or get_settings()

    squad = (squad or "").strip()
    if not squad:
        raise ValueError("squad must be a non-empty string")

    # Pull registered repos owned by this squad. We join through
    # OrgRepo.registered_repo_id rather than re-deriving via URN —
    # the catalog row is the canonical place where ``squad`` lives.
    join_stmt = (
        select(Repo)
        .join(OrgRepo, OrgRepo.registered_repo_id == Repo.id)
        .where(OrgRepo.squad == squad)
        .order_by(Repo.name)
    )
    repos_in_scope = list((await repo._session.execute(join_stmt)).scalars().all())
    # Dedupe by id in case of multiple OrgRepo rows mapping to the same Repo.
    seen: set[uuid.UUID] = set()
    deduped: list[Repo] = []
    for r in repos_in_scope:
        if r.id in seen:
            continue
        seen.add(r.id)
        deduped.append(r)
    repos_in_scope = deduped

    out_dir = _handovers_dir() / "teams" / _slugify(squad)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "index.md"

    if not repos_in_scope:
        logger.warning(
            "team handover: squad=%s has no registered repos; stub doc only",
            squad,
        )
        title = f"{squad} — team handover (stub)"
        final_path.write_text(
            _stub_multi_repo_doc_text(
                title=title,
                scope_kind_label="Team",
                scope_label=squad,
                repos=[],
                reason=(
                    "This squad has no registered repos yet. Sync the ownership "
                    "catalog and register repos owned by this squad, then "
                    "regenerate."
                ),
            ),
            encoding="utf-8",
        )
        doc = await _upsert_doc_generic(
            repo=repo,
            scope_kind="team",
            scope_value=squad,
            final_path=final_path,
        )
        return HandoverGenerationResult(
            doc_id=doc.id,
            fs_path=str(final_path),
            title=title,
            used_fallback=True,
        )

    use_tree = len(repos_in_scope) > _TREE_THRESHOLD

    await publish_handover_event(
        "team",
        squad,
        "scope_resolved",
        {
            "repos": [r.name for r in repos_in_scope],
            "use_tree": use_tree,
        },
    )

    if use_tree:
        # Tree decomposition: spawn per-repo child handovers
        # in parallel, then compose the parent from their bodies.
        await publish_handover_event(
            "team",
            squad,
            "child_started",
            {"count": len(repos_in_scope)},
        )
        child_pairs = await _spawn_repo_handovers_in_parallel(
            repos_in_scope,
            repo,
            settings,
        )
        await publish_handover_event(
            "team",
            squad,
            "child_done",
            {
                "succeeded": len(child_pairs),
                "total": len(repos_in_scope),
            },
        )
        if not child_pairs:
            logger.warning(
                "team handover (tree): squad=%s — all child generations failed; stub doc only",
                squad,
            )
            title = f"{squad} — team handover (stub)"
            final_path.write_text(
                _stub_multi_repo_doc_text(
                    title=title,
                    scope_kind_label="Team",
                    scope_label=squad,
                    repos=repos_in_scope,
                    reason=(
                        "All per-repo handovers failed to generate. Inspect "
                        "the server log for child errors, then regenerate."
                    ),
                ),
                encoding="utf-8",
            )
            doc = await _upsert_doc_generic(
                repo=repo,
                scope_kind="team",
                scope_value=squad,
                final_path=final_path,
            )
            return HandoverGenerationResult(
                doc_id=doc.id,
                fs_path=str(final_path),
                title=title,
                used_fallback=True,
            )

        children_md = _format_child_handovers_for_compose(child_pairs)
        confluence_pages = await _pull_confluence_for_scope(
            repo,
            "team",
            squad,
            [r.name for r in repos_in_scope],
        )
        parts: list[str] = [
            "Team (squad):",
            wrap_untrusted(
                f"squad: {squad}\n"
                f"repo_count: {len(repos_in_scope)}\n"
                f"children_with_bodies: {len(child_pairs)}",
                "team_metadata",
            ),
            "",
            f"Repos owned by this squad ({len(repos_in_scope)}):",
            wrap_untrusted(_format_repo_list(repos_in_scope), "repo_list"),
            "",
            f"Per-repo handovers ({len(child_pairs)} successfully generated; "
            "each is a real artifact you can cite):",
            children_md,
            "",
        ]
        confluence_block = _format_confluence_block(confluence_pages)
        if confluence_block:
            parts.extend(confluence_block)
            parts.append("")
        parts.append(
            "Compose the TEAM handover by abstracting up from the children. "
            "Return the JSON object exactly per the schema."
        )
        user_message = "\n".join(parts)
        composer_prompt = _PARENT_COMPOSER_SYSTEM_PROMPT
        n_signal = len(child_pairs)
        signal_label = f"child_handovers={n_signal}"
    else:
        # Pooled-entity path — for small squads.
        repo_ids = [r.id for r in repos_in_scope]
        entities = await _pull_entities_for_repos(repo, repo_ids)
        entity_summary, counts = _summarize_entities(entities)

        if not entities:
            logger.warning(
                "team handover: squad=%s has 0 indexed entities across %d repos; stub doc only",
                squad,
                len(repos_in_scope),
            )
            title = f"{squad} — team handover (stub)"
            final_path.write_text(
                _stub_multi_repo_doc_text(
                    title=title,
                    scope_kind_label="Team",
                    scope_label=squad,
                    repos=repos_in_scope,
                    reason=(
                        "Repos are registered but no entities have been extracted "
                        "yet. Run extractions for the squad's repos, then "
                        "regenerate."
                    ),
                ),
                encoding="utf-8",
            )
            doc = await _upsert_doc_generic(
                repo=repo,
                scope_kind="team",
                scope_value=squad,
                final_path=final_path,
            )
            return HandoverGenerationResult(
                doc_id=doc.id,
                fs_path=str(final_path),
                title=title,
                used_fallback=True,
            )

        confluence_pages = await _pull_confluence_for_scope(
            repo,
            "team",
            squad,
            [r.name for r in repos_in_scope],
        )
        parts: list[str] = [
            "Team (squad):",
            wrap_untrusted(
                f"squad: {squad}\nrepo_count: {len(repos_in_scope)}",
                "team_metadata",
            ),
            "",
            f"Repos owned by this squad ({len(repos_in_scope)}):",
            wrap_untrusted(_format_repo_list(repos_in_scope), "repo_list"),
            "",
            f"Entity counts (per type, pooled across the squad): {counts}",
            "",
            "Representative entities (sampled across all repos, tier-1 first):",
            wrap_untrusted(entity_summary, "entity_slice"),
            "",
        ]
        confluence_block = _format_confluence_block(confluence_pages)
        if confluence_block:
            parts.extend(confluence_block)
            parts.append("")
        parts.append(
            "Produce the team handover doc now. Return the JSON object exactly "
            "per the schema. Remember: this is a TEAM (squad) handover — call out "
            "which repos contribute which capabilities."
        )
        user_message = "\n".join(parts)
        composer_prompt = _TEAM_HANDOVER_SYSTEM_PROMPT
        n_signal = len(entities)
        signal_label = f"entities={n_signal}"

    client = ClaudeLlmClient(settings)
    await publish_handover_event(
        "team",
        squad,
        "compose_started",
        {"signal": signal_label, "tree": use_tree},
    )
    try:
        completion = await client.call_with_json_schema(
            system_prompt=composer_prompt,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_HANDOVER_SCHEMA,
            max_turns=1,
            timeout_override=300,
        )
        parsed = completion.parsed
    except ClaudeCliError as exc:
        logger.warning(
            "handover generator LLM failed for squad=%s: %s",
            squad,
            exc,
        )
        await publish_handover_event(
            "team",
            squad,
            "error",
            {"reason": str(exc)},
        )
        return HandoverGenerationResult(
            doc_id=uuid.UUID(int=0),
            fs_path="",
            title="",
            used_fallback=True,
        )

    title = str(parsed.get("title") or f"{squad} — team handover").strip()
    await publish_handover_event(
        "team",
        squad,
        "compose_done",
        {"title": title},
    )

    # Prepend deterministic cross-repo flow diagram (see
    # generate_domain_handover for the rationale).
    diagram = await _build_cross_repo_flow_diagram(
        repo,
        [r.id for r in repos_in_scope],
    )
    if diagram is not None:
        _prepend_cross_repo_diagram(parsed, diagram)

    # Snapshot prior body before temp+replace.
    await _snapshot_existing_body_pre_regen(
        repo=repo,
        scope_kind="team",
        scope_value=squad,
    )
    tmp_path = out_dir / "index.md.tmp"
    tmp_path.write_text(
        _render_multi_repo_handover_md(
            title_fallback=f"{squad} — team handover",
            scope_label=squad,
            scope_kind_label="Team",
            repos=repos_in_scope,
            parsed=parsed,
        ),
        encoding="utf-8",
    )
    tmp_path.replace(final_path)
    await publish_handover_event(
        "team",
        squad,
        "fs_written",
        {"fs_path": str(final_path)},
    )

    doc = await _upsert_doc_generic(
        repo=repo,
        scope_kind="team",
        scope_value=squad,
        final_path=final_path,
    )

    logger.info(
        "handover generated squad=%s doc_id=%s path=%s repos=%d %s tree=%s",
        squad,
        doc.id,
        final_path,
        len(repos_in_scope),
        signal_label,
        use_tree,
    )
    await publish_handover_event(
        "team",
        squad,
        "complete",
        {
            "doc_id": str(doc.id),
            "fs_path": str(final_path),
            "title": title,
            "used_fallback": False,
        },
    )
    return HandoverGenerationResult(
        doc_id=doc.id,
        fs_path=str(final_path),
        title=title,
    )


# ── Region handover generator ────────────────────────


async def generate_region_handover(
    *,
    region: str,
    repo: EntityRepository,
    settings: BifrostSettings | None = None,
) -> HandoverGenerationResult:
    """Generate (or regenerate) the handover doc for a region scope.

    Pulls org_repos rows where ``region == region`` that have been
    linked to a registered Repo (via ``registered_repo_id``), then
    pools entities across those repos. Idempotent on
    (scope_kind='region', scope_value=<region>, parent_id=NULL).

    Structurally identical to :func:`generate_team_handover` —
    only the join column differs (``OrgRepo.region`` vs
    ``OrgRepo.squad``). Same tree-decomposition branch over
    ``_TREE_THRESHOLD``.

    Raises ``ValueError`` if the region string is empty; surfaces
    ``HandoverGenerationResult.used_fallback=True`` if no registered
    repos exist for the region or the LLM call fails."""
    settings = settings or get_settings()

    region = (region or "").strip()
    if not region:
        raise ValueError("region must be a non-empty string")

    # Pull registered repos in this region. Joined through
    # OrgRepo.registered_repo_id rather than re-deriving via URN —
    # the catalog row is the canonical place where ``region`` lives.
    join_stmt = (
        select(Repo)
        .join(OrgRepo, OrgRepo.registered_repo_id == Repo.id)
        .where(OrgRepo.region == region)
        .order_by(Repo.name)
    )
    repos_in_scope = list((await repo._session.execute(join_stmt)).scalars().all())
    # Dedupe by id in case of multiple OrgRepo rows mapping to the same Repo.
    seen: set[uuid.UUID] = set()
    deduped: list[Repo] = []
    for r in repos_in_scope:
        if r.id in seen:
            continue
        seen.add(r.id)
        deduped.append(r)
    repos_in_scope = deduped

    out_dir = _handovers_dir() / "regions" / _slugify(region)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "index.md"

    if not repos_in_scope:
        logger.warning(
            "region handover: region=%s has no registered repos; stub doc only",
            region,
        )
        title = f"{region} — region handover (stub)"
        final_path.write_text(
            _stub_multi_repo_doc_text(
                title=title,
                scope_kind_label="Region",
                scope_label=region,
                repos=[],
                reason=(
                    "This region has no registered repos yet. Sync the ownership "
                    "catalog and register repos in this region, then "
                    "regenerate."
                ),
            ),
            encoding="utf-8",
        )
        doc = await _upsert_doc_generic(
            repo=repo,
            scope_kind="region",
            scope_value=region,
            final_path=final_path,
        )
        return HandoverGenerationResult(
            doc_id=doc.id,
            fs_path=str(final_path),
            title=title,
            used_fallback=True,
        )

    use_tree = len(repos_in_scope) > _TREE_THRESHOLD

    await publish_handover_event(
        "region",
        region,
        "scope_resolved",
        {
            "repos": [r.name for r in repos_in_scope],
            "use_tree": use_tree,
        },
    )

    if use_tree:
        # Tree decomposition: spawn per-repo child handovers
        # in parallel, then compose the parent from their bodies.
        await publish_handover_event(
            "region",
            region,
            "child_started",
            {"count": len(repos_in_scope)},
        )
        child_pairs = await _spawn_repo_handovers_in_parallel(
            repos_in_scope,
            repo,
            settings,
        )
        await publish_handover_event(
            "region",
            region,
            "child_done",
            {
                "succeeded": len(child_pairs),
                "total": len(repos_in_scope),
            },
        )
        if not child_pairs:
            logger.warning(
                "region handover (tree): region=%s — all child generations failed; stub doc only",
                region,
            )
            title = f"{region} — region handover (stub)"
            final_path.write_text(
                _stub_multi_repo_doc_text(
                    title=title,
                    scope_kind_label="Region",
                    scope_label=region,
                    repos=repos_in_scope,
                    reason=(
                        "All per-repo handovers failed to generate. Inspect "
                        "the server log for child errors, then regenerate."
                    ),
                ),
                encoding="utf-8",
            )
            doc = await _upsert_doc_generic(
                repo=repo,
                scope_kind="region",
                scope_value=region,
                final_path=final_path,
            )
            return HandoverGenerationResult(
                doc_id=doc.id,
                fs_path=str(final_path),
                title=title,
                used_fallback=True,
            )

        children_md = _format_child_handovers_for_compose(child_pairs)
        confluence_pages = await _pull_confluence_for_scope(
            repo,
            "region",
            region,
            [r.name for r in repos_in_scope],
        )
        parts: list[str] = [
            "Region:",
            wrap_untrusted(
                f"region: {region}\n"
                f"repo_count: {len(repos_in_scope)}\n"
                f"children_with_bodies: {len(child_pairs)}",
                "region_metadata",
            ),
            "",
            f"Repos in this region ({len(repos_in_scope)}):",
            wrap_untrusted(_format_repo_list(repos_in_scope), "repo_list"),
            "",
            f"Per-repo handovers ({len(child_pairs)} successfully generated; "
            "each is a real artifact you can cite):",
            children_md,
            "",
        ]
        confluence_block = _format_confluence_block(confluence_pages)
        if confluence_block:
            parts.extend(confluence_block)
            parts.append("")
        parts.append(
            "Compose the REGION handover by abstracting up from the children. "
            "Return the JSON object exactly per the schema."
        )
        user_message = "\n".join(parts)
        composer_prompt = _PARENT_COMPOSER_SYSTEM_PROMPT
        n_signal = len(child_pairs)
        signal_label = f"child_handovers={n_signal}"
    else:
        # Pooled-entity path — for small regions.
        repo_ids = [r.id for r in repos_in_scope]
        entities = await _pull_entities_for_repos(repo, repo_ids)
        entity_summary, counts = _summarize_entities(entities)

        if not entities:
            logger.warning(
                "region handover: region=%s has 0 indexed entities across %d repos; stub doc only",
                region,
                len(repos_in_scope),
            )
            title = f"{region} — region handover (stub)"
            final_path.write_text(
                _stub_multi_repo_doc_text(
                    title=title,
                    scope_kind_label="Region",
                    scope_label=region,
                    repos=repos_in_scope,
                    reason=(
                        "Repos are registered but no entities have been extracted "
                        "yet. Run extractions for the region's repos, then "
                        "regenerate."
                    ),
                ),
                encoding="utf-8",
            )
            doc = await _upsert_doc_generic(
                repo=repo,
                scope_kind="region",
                scope_value=region,
                final_path=final_path,
            )
            return HandoverGenerationResult(
                doc_id=doc.id,
                fs_path=str(final_path),
                title=title,
                used_fallback=True,
            )

        confluence_pages = await _pull_confluence_for_scope(
            repo,
            "region",
            region,
            [r.name for r in repos_in_scope],
        )
        parts: list[str] = [
            "Region:",
            wrap_untrusted(
                f"region: {region}\nrepo_count: {len(repos_in_scope)}",
                "region_metadata",
            ),
            "",
            f"Repos in this region ({len(repos_in_scope)}):",
            wrap_untrusted(_format_repo_list(repos_in_scope), "repo_list"),
            "",
            f"Entity counts (per type, pooled across the region): {counts}",
            "",
            "Representative entities (sampled across all repos, tier-1 first):",
            wrap_untrusted(entity_summary, "entity_slice"),
            "",
        ]
        confluence_block = _format_confluence_block(confluence_pages)
        if confluence_block:
            parts.extend(confluence_block)
            parts.append("")
        parts.append(
            "Produce the region handover doc now. Return the JSON object exactly "
            "per the schema. Remember: this is a REGION handover — call out "
            "which repos contribute which capabilities."
        )
        user_message = "\n".join(parts)
        composer_prompt = _REGION_HANDOVER_SYSTEM_PROMPT
        n_signal = len(entities)
        signal_label = f"entities={n_signal}"

    client = ClaudeLlmClient(settings)
    await publish_handover_event(
        "region",
        region,
        "compose_started",
        {"signal": signal_label, "tree": use_tree},
    )
    try:
        completion = await client.call_with_json_schema(
            system_prompt=composer_prompt,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_HANDOVER_SCHEMA,
            max_turns=1,
            timeout_override=300,
        )
        parsed = completion.parsed
    except ClaudeCliError as exc:
        logger.warning(
            "handover generator LLM failed for region=%s: %s",
            region,
            exc,
        )
        await publish_handover_event(
            "region",
            region,
            "error",
            {"reason": str(exc)},
        )
        return HandoverGenerationResult(
            doc_id=uuid.UUID(int=0),
            fs_path="",
            title="",
            used_fallback=True,
        )

    title = str(parsed.get("title") or f"{region} — region handover").strip()
    await publish_handover_event(
        "region",
        region,
        "compose_done",
        {"title": title},
    )

    # Prepend deterministic cross-repo flow diagram (see
    # generate_domain_handover for the rationale).
    diagram = await _build_cross_repo_flow_diagram(
        repo,
        [r.id for r in repos_in_scope],
    )
    if diagram is not None:
        _prepend_cross_repo_diagram(parsed, diagram)

    # Snapshot prior body before temp+replace.
    await _snapshot_existing_body_pre_regen(
        repo=repo,
        scope_kind="region",
        scope_value=region,
    )
    tmp_path = out_dir / "index.md.tmp"
    tmp_path.write_text(
        _render_multi_repo_handover_md(
            title_fallback=f"{region} — region handover",
            scope_label=region,
            scope_kind_label="Region",
            repos=repos_in_scope,
            parsed=parsed,
        ),
        encoding="utf-8",
    )
    tmp_path.replace(final_path)
    await publish_handover_event(
        "region",
        region,
        "fs_written",
        {"fs_path": str(final_path)},
    )

    doc = await _upsert_doc_generic(
        repo=repo,
        scope_kind="region",
        scope_value=region,
        final_path=final_path,
    )

    logger.info(
        "handover generated region=%s doc_id=%s path=%s repos=%d %s tree=%s",
        region,
        doc.id,
        final_path,
        len(repos_in_scope),
        signal_label,
        use_tree,
    )
    await publish_handover_event(
        "region",
        region,
        "complete",
        {
            "doc_id": str(doc.id),
            "fs_path": str(final_path),
            "title": title,
            "used_fallback": False,
        },
    )
    return HandoverGenerationResult(
        doc_id=doc.id,
        fs_path=str(final_path),
        title=title,
    )
