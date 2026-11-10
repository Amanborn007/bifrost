"""Cross-repo edge linker.

Phase G extracts EntityRelationship rows within a single repo. This
module scans every entity's metadata (internal_dependencies,
exported_interfaces, imports, etc.) and, when a dependency string
resolves to an entity in ANOTHER repo, writes an explicit cross-repo
edge.

These edges are what let the graph-based retriever walk across repo
boundaries — "OrderRouter in order-intake calls X in
shared-carrier-lib" becomes a real graph edge, not a string in metadata
nobody ever reads.

Matching strategy (v1, pragmatic):
  1. Build a global symbol index keyed by:
       - lowercased `Entity.name`
       - lowercased `Entity.qualified_name` (full form)
       - lowercased last segment of qualified_name (e.g., after `.` / `::`
         / `/`)
  2. For each entity E, pull candidate dep strings from metadata.
  3. For each dep string D, extract candidate lookup keys (the name
     after the last separator, plus the full path) and probe the index.
  4. For each match M where `M.repo_id != E.repo_id` — emit a
     cross-repo edge `E --cross_repo_depends_on--> M`.
  5. Skip symbols shorter than 4 chars (too ambiguous) and symbols that
     resolve to >5 candidates (too ambiguous).

Idempotent: uses ON CONFLICT DO NOTHING against the unique constraint
`(source_id, target_id, rel_type)`, so repeat runs are a no-op.

Exposed entry points:
    link_all(session_factory) -> LinkerStats
        runs the full linker and returns stats.

Invoked via admin endpoint POST /api/v1/admin/link-cross-repo.
"""

from __future__ import annotations

import logging
import re
import uuid as _uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import get_settings
from bifrost.research.confidence_policy import build_source_basis
from bifrost.storage.models import Entity, EntityRelationship, Repo
from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)


# Dependency-bearing metadata keys we scan per entity. Add here as new
# extraction phases surface new dep-like fields.
_DEP_METADATA_KEYS = (
    "internal_dependencies",
    "external_dependencies",
    "imports",
    "uses",
    "calls",
    "depends_on",
)

# Rel type emitted by this linker. Kept distinct from intra-repo
# rel_types so consumers can filter/color them in the UI.
_CROSS_REPO_REL = "cross_repo_depends_on"

# Guardrails.
_MIN_SYMBOL_LEN = 6  # excludes "Error", "Config", "Types"
_MAX_CANDIDATES_PER_SYMBOL = 3  # 4+ means the symbol is too generic

# Single-segment names that are almost always false-positive when
# matched bare. If the dep string IS this (no qualifier), skip. If it's
# a qualified path ending in this, still allow — the qualifier grounds it.
_GENERIC_NAMES: frozenset[str] = frozenset(
    {
        "schemas",
        "models",
        "config",
        "configs",
        "service",
        "services",
        "client",
        "clients",
        "response",
        "request",
        "error",
        "errors",
        "handler",
        "handlers",
        "metrics",
        "util",
        "utils",
        "helpers",
        "types",
        "store",
        "stores",
        "domain",
        "common",
        "internal",
        "adapter",
        "adapters",
        "repository",
        "repositories",
        "database",
        "logger",
        "events",
        "event",
        "entity",
        "entities",
        "test",
        "tests",
        "main",
        "app",
        "api",
        "context",
        "contexts",
        "server",
        "factory",
        "factories",
        "controller",
        "controllers",
        "base",
        "options",
        "params",
        "param",
        "result",
        "results",
        "state",
        "status",
        "value",
    }
)


@dataclass
class LinkerStats:
    entities_scanned: int = 0
    deps_scanned: int = 0
    candidates_matched: int = 0
    edges_created: int = 0
    edges_skipped_duplicate: int = 0
    ambiguous_skipped: int = 0
    by_repo_pair: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "entities_scanned": self.entities_scanned,
            "deps_scanned": self.deps_scanned,
            "candidates_matched": self.candidates_matched,
            "edges_created": self.edges_created,
            "edges_skipped_duplicate": self.edges_skipped_duplicate,
            "ambiguous_skipped": self.ambiguous_skipped,
            "top_repo_pairs": sorted(self.by_repo_pair.items(), key=lambda kv: -kv[1])[:10],
        }


async def link_all(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dry_run: bool = False,
) -> LinkerStats:
    """Build the global symbol index and write cross-repo edges.

    Runs in a single session. One big SELECT to load entity rows, one
    multi-row INSERT (batched) to write edges. Safe to run repeatedly.
    """
    stats = LinkerStats()

    async with session_factory() as session:
        # 0. Load repos so dep strings like `com.example.<X>` can be
        # resolved to a specific repo — this is how we keep the
        # last-segment fallback from producing nonsense matches.
        repo_rows = (await session.execute(select(Repo.id, Repo.name))).all()
        # normalized_name -> repo_id. Normalize: lowercase, strip
        # non-alphanumerics. "StockService" and
        # "stock-service" both collapse to "stockservice".
        repo_by_norm: dict[str, _uuid.UUID] = {}
        for rid, rname in repo_rows:
            key = _normalize_repo_name(rname)
            if key:
                repo_by_norm[key] = rid

        # Fuzzy hint resolver — `com.example.stock.X` should
        # resolve to `StockService` even though "stock"
        # != "stockservice". Requires a UNIQUE substring match
        # to avoid a generic word matching many different repos.
        def _resolve_hint(h: str | None) -> _uuid.UUID | None:
            if not h:
                return None
            if h in repo_by_norm:
                return repo_by_norm[h]
            # Substring match in either direction, but only if unique.
            candidates = [
                rid
                for norm, rid in repo_by_norm.items()
                if (h in norm or norm in h) and len(h) >= 5
            ]
            if len(candidates) == 1:
                return candidates[0]
            return None

        # 1. Load every entity. We only need: id, name, qualified_name,
        # scope_repo_id, entity_metadata. We intentionally do NOT load
        # the embedding column (heavy vector payload we don't need here).
        stmt = select(
            Entity.id,
            Entity.name,
            Entity.qualified_name,
            Entity.entity_type,
            Entity.scope_repo_id,
            Entity.entity_metadata,
        ).where(Entity.scope_repo_id.isnot(None))
        rows = (await session.execute(stmt)).all()
        stats.entities_scanned = len(rows)
        if not rows:
            return stats

        # 2. Build the symbol → [(entity_id, repo_id, type)] index.
        index: dict[str, list[tuple[_uuid.UUID, _uuid.UUID, str]]] = defaultdict(list)
        entities_by_id: dict[_uuid.UUID, tuple[_uuid.UUID, str, str]] = {}

        for eid, name, qname, etype, repo_id, meta in rows:
            entities_by_id[eid] = (repo_id, name, etype)
            for key in _index_keys(name, qname):
                index[key].append((eid, repo_id, etype))

        # 3. Scan each entity's deps; collect candidate edges.
        # Dedup in-memory before insert so ambiguous matches don't flood
        # the INSERT path.
        # Dict instead of set so we carry per-edge provenance: the
        # exact dep string that produced the match, the match method,
        # and the derived confidence tier. Keyed by (source, target)
        # so repeat matches on the same edge keep the strongest one.
        proposed: dict[
            tuple[_uuid.UUID, _uuid.UUID],
            tuple[str, str, float, str],
        ] = {}

        hint_patterns = _repo_hint_patterns(get_settings().linking.internal_package_prefixes)
        for eid, _name, _qname, _etype, repo_id, meta in rows:
            if not isinstance(meta, dict):
                continue
            dep_strings = _collect_deps(meta)
            stats.deps_scanned += len(dep_strings)

            for dep in dep_strings:
                full_keys, last_keys, last_keys_even_if_generic = _lookup_keys_tiered(dep)
                if not full_keys and not last_keys and not last_keys_even_if_generic:
                    continue

                hint = _dep_target_repo_hint(dep, hint_patterns)
                hinted_repo: _uuid.UUID | None = _resolve_hint(hint)
                # If the hint resolves back to the source repo, this
                # dep is INTRA-repo — skip. This kills the big
                # false-positive class (ErrorResponse inside the same
                # service matched to unrelated ErrorResponse classes).
                if hint and hinted_repo == repo_id:
                    continue

                # Candidate hits — tiered to keep precision high.
                hits: list[tuple[_uuid.UUID, _uuid.UUID, str]] = []

                # Tier 1: full-string key match (very specific — low
                # false positive rate even without a hint).
                for k in full_keys:
                    if k in index:
                        hits.extend(index[k])

                # Tier 2: last-segment match.
                # Without a hint: require non-generic segment so we
                # don't match `metrics` everywhere.
                # With a valid hint: allow even generic segments — the
                # hint already narrows ambiguity to one repo.
                if hinted_repo is not None and hinted_repo != repo_id:
                    for k in last_keys + last_keys_even_if_generic:
                        if k in index:
                            hits.extend(h for h in index[k] if h[1] == hinted_repo)
                else:
                    for k in last_keys:
                        if k in index:
                            hits.extend(index[k])

                if not hits:
                    continue

                # Filter to other-repo and non-self.
                other_repo_hits = [h for h in hits if h[1] != repo_id and h[0] != eid]
                if not other_repo_hits:
                    continue

                # If a hint resolves to a known repo, tighten to that
                # repo only (even tier-1 matches shouldn't bleed into
                # unrelated repos when the dep is package-qualified).
                if hinted_repo is not None:
                    other_repo_hits = [h for h in other_repo_hits if h[1] == hinted_repo]
                    if not other_repo_hits:
                        continue
                # De-dup by target.
                seen_targets: set[_uuid.UUID] = set()
                unique = []
                for h in other_repo_hits:
                    if h[0] in seen_targets:
                        continue
                    seen_targets.add(h[0])
                    unique.append(h)
                # Drop ambiguous — too many candidates means the symbol
                # is too generic (e.g., "User", "Response").
                if len(unique) > _MAX_CANDIDATES_PER_SYMBOL:
                    stats.ambiguous_skipped += 1
                    continue
                stats.candidates_matched += len(unique)
                # Classify match precision for confidence tiering.
                # - Hint resolved uniquely AND the full-path key hit →
                #   T3 metadata_match (0.70): package-qualified import
                # - Hint resolved uniquely via last-segment only → T3
                #   lower-conf (0.60): name match in the right repo
                # - No hint + last-segment match → T5 fuzzy_match (0.35):
                #   could be wrong; synthesis must caveat
                via_full_path = any(k in index for k in full_keys)
                if hinted_repo is not None and via_full_path:
                    match_source = "metadata_match"
                    match_conf = 0.72
                    match_method = "package_qualified+repo_hint"
                elif hinted_repo is not None:
                    match_source = "metadata_match"
                    match_conf = 0.60
                    match_method = "last_segment+repo_hint"
                else:
                    match_source = "fuzzy_match"
                    match_conf = 0.35
                    match_method = "last_segment_only"
                for target_id, target_repo, _ttype in unique:
                    key = (eid, target_id)
                    # Keep the STRONGEST match for an edge — if a
                    # previous dep matched via fuzzy and this one via
                    # package-qualified, upgrade to the stronger.
                    existing = proposed.get(key)
                    if existing is None or existing[2] < match_conf:
                        proposed[key] = (
                            dep,
                            match_source,
                            match_conf,
                            match_method,
                        )
                    pair_key = f"{repo_id} -> {target_repo}"
                    stats.by_repo_pair[pair_key] = stats.by_repo_pair.get(pair_key, 0) + 1

        if dry_run:
            stats.edges_created = len(proposed)
            return stats

        # 4. Batch insert with ON CONFLICT DO NOTHING. Each edge carries
        # its own confidence + source_basis derived from how precisely
        # it was matched.
        if proposed:
            values = []
            for (sid, tid), (dep_str, source_type, conf, method) in proposed.items():
                values.append(
                    {
                        "id": _uuid.uuid4(),
                        "source_id": sid,
                        "target_id": tid,
                        "rel_type": _CROSS_REPO_REL,
                        "confidence": conf,
                        "freshness_state": "fresh",
                        "source_basis": build_source_basis(
                            source_type,  # type: ignore[arg-type]
                            reference=f"cross_repo_linker: {dep_str[:100]}",
                            method=method,
                            matched_on=dep_str[:120],
                        ),
                    }
                )
            # Chunk the insert — some Postgres instances choke on
            # >10K-row single inserts.
            BATCH = 500
            created = 0
            for i in range(0, len(values), BATCH):
                chunk = values[i : i + BATCH]
                stmt = pg_insert(EntityRelationship).values(chunk)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["source_id", "target_id", "rel_type"]
                )
                result = await session.execute(stmt)
                # rowcount on ON CONFLICT is the # actually inserted
                created += affected_rows(result)
            await session.commit()
            stats.edges_created = created
            stats.edges_skipped_duplicate = len(proposed) - created

        return stats


# ── Key extraction helpers ──────────────────────────────────────────


def _index_keys(name: str, qualified_name: str | None) -> Iterable[str]:
    """Emit every way a dependency string might spell this entity.

    Includes:
      - the name itself
      - the qualified_name (full)
      - the last segment after `/`, `.`, `::`, `\\`
    All lowercased, filtered by _MIN_SYMBOL_LEN.
    """
    keys: set[str] = set()
    if name and len(name) >= _MIN_SYMBOL_LEN:
        keys.add(name.lower())
    if qualified_name:
        qn = qualified_name.strip()
        if qn and len(qn) >= _MIN_SYMBOL_LEN:
            keys.add(qn.lower())
        # last segment (most stable under rename)
        for sep in ("::", "/", ".", "\\"):
            if sep in qn:
                last = qn.rsplit(sep, 1)[-1]
                if last and len(last) >= _MIN_SYMBOL_LEN:
                    keys.add(last.lower())
    return keys


def _lookup_keys_tiered(
    dep: str,
) -> tuple[list[str], list[str], list[str]]:
    """Return (full_keys, last_non_generic, last_even_if_generic).

    * full_keys: whole dep string lowercased — very specific.
    * last_non_generic: last segment, filtered against _GENERIC_NAMES.
      Safe to use without a repo-hint.
    * last_even_if_generic: last segment INCLUDING generic names like
      "metrics", "schemas". Caller may use this only when a repo-hint
      already narrows matches to a single repo (in which case
      ambiguity is bounded to that repo).
    """
    if not dep or len(dep) < _MIN_SYMBOL_LEN:
        return [], [], []
    d = dep.strip()
    full: list[str] = []
    last_ng: list[str] = []
    last_any: list[str] = []

    has_qualifier = any(sep in d for sep in ("::", "/", ".", "\\"))

    if has_qualifier or d.lower() not in _GENERIC_NAMES:
        full.append(d.lower())

    if has_qualifier:
        for sep in ("::", "/", ".", "\\", " "):
            if sep in d:
                segment = d.rsplit(sep, 1)[-1].strip("()[]<>,\"'`")
                if segment and len(segment) >= _MIN_SYMBOL_LEN:
                    seg_lower = segment.lower()
                    if seg_lower in _GENERIC_NAMES:
                        last_any.append(seg_lower)
                    else:
                        last_ng.append(seg_lower)
                        last_any.append(seg_lower)
                    break

    return full, last_ng, last_any


def _normalize_repo_name(name: str | None) -> str:
    """Collapse a repo name so `StockService` and
    `stock-service` both become `stockservice`."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _repo_hint_patterns(prefixes: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """Compile repo-name hint patterns from ``linking.internal_package_prefixes``.

    ``com.example.`` -> Java/Kotlin package, ``@example/`` -> npm scope,
    ``github.com/example/`` -> Go module path.
    """
    patterns: list[re.Pattern[str]] = []
    for prefix in prefixes:
        if not prefix:
            continue
        esc = re.escape(prefix)
        if prefix.startswith("@"):
            patterns.append(re.compile(esc + r"([a-z0-9-]+)/"))
        elif "/" in prefix:
            patterns.append(re.compile(esc + r"([a-z0-9._-]+)/", re.IGNORECASE))
        else:
            patterns.append(re.compile("^" + esc + r"([a-z0-9-]+)\."))
    return tuple(patterns)


def _dep_target_repo_hint(
    dep: str, patterns: Sequence[re.Pattern[str]] | None = None
) -> str | None:
    """Return the normalized repo name embedded in the dep string, if any.

    The output is normalized via `_normalize_repo_name` so it matches
    the `repo_by_norm` index regardless of casing/hyphenation. ``patterns``
    defaults to those built from the configured internal package prefixes.
    """
    if not dep:
        return None
    if patterns is None:
        patterns = _repo_hint_patterns(get_settings().linking.internal_package_prefixes)
    s = dep.strip()
    for pat in patterns:
        m = pat.search(s)
        if m:
            return _normalize_repo_name(m.group(1))
    return None


def _collect_deps(meta: dict) -> list[str]:
    """Pull every dep-looking string out of one entity's metadata."""
    out: list[str] = []
    for k in _DEP_METADATA_KEYS:
        v = meta.get(k)
        if not v:
            continue
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    # {"name": "...", "kind": "..."} shape
                    nm = item.get("name") or item.get("symbol") or ""
                    if isinstance(nm, str) and nm:
                        out.append(nm)
    return out
