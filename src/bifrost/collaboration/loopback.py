"""Doubt-candidate loop-back.

Closes the loop between collaboration sessions and handover docs.
When the same kind of question keeps surfacing in sessions and the
relevant handover doesn't answer it, that's a candidate for the
doc itself — eventually the human edits the handover so the
clarifier can resolve future occurrences as ``answered_by_doc``
without bothering the owning team.

First cut: surface the candidates. The promotion flow (where
a human writes the answer back into the handover) is separate.

Algorithm:
  1. For a given handover doc, find recent collaboration sessions
     whose requirement / chat mention the doc's scope_value.
  2. From those sessions, pull doubts in status ``unresolved`` or
     ``escalated``.
  3. Cluster doubts by question similarity:
       a. Fast-path: normalize-equal questions cluster first
          (lowercase + stripped punctuation + collapsed whitespace) —
          skips the embedding cost when the answer is obvious.
       b. Embedding-similarity: each remaining doubt is embedded with
          sentence-transformers/all-MiniLM-L6-v2 and assigned to the
          nearest existing cluster (by cosine similarity to the
          centroid) when ``>= BIFROST_DOUBT_CLUSTER_THRESHOLD``
          (default 0.85). Otherwise a new cluster is started.
       c. On any embedding failure (model load error, GPU OOM, etc.)
          we silently fall back to normalized-string
          clustering. Function never raises.
  4. Return clusters with ``occurrence_count >= min_occurrences``
     (default 3), most recent first, capped.

Cost: pure Python + one batched local embedding pass — no LLM API
call. Cheap enough to run on every handover-detail GET (the model
loads lazily once per process and is reused thereafter).
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from bifrost.storage.models import (
    CollabDoubt,
    CollaborationSession,
    HandoverDoc,
)
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


# Defaults — tunable later if telemetry shows they're wrong.
_DEFAULT_MIN_OCCURRENCES = 3
_DEFAULT_SESSION_LOOKBACK = 200  # most-recent sessions to scan
_DEFAULT_CANDIDATE_LIMIT = 10  # cap clusters returned

# Cosine-similarity threshold above which a doubt joins an existing
# cluster. 0.85 is empirically conservative for MiniLM on short
# question-style text — paraphrases like "what's the SLA for returns?"
# vs "how fast does return processing happen?" cleared 0.85 in
# probes; unrelated questions land near 0.3-0.6.
_DEFAULT_CLUSTER_THRESHOLD = 0.85


def _cluster_threshold() -> float:
    """Doubt-clustering similarity floor from the validated config surface.

    Reads the bounded ``doubt_cluster_threshold`` field
    (``BIFROST_DOUBT_CLUSTER_THRESHOLD``) instead of parsing the env raw. An
    out-of-range or non-numeric value now fails fast at boot via
    ``validate_startup`` rather than being silently swallowed here.
    """
    from bifrost.cli.config import get_settings

    return get_settings().doubt_cluster_threshold


@dataclass
class DoubtCandidate:
    """One cluster of recurring doubts surfaced as a handover-enhancement
    candidate. ``occurrence_count`` is the number of distinct sessions
    that asked this same kind of question; the UI uses it to rank +
    label "N teams asked this"."""

    normalized_question: str
    representative_question: str
    occurrence_count: int
    session_ids: list[str]
    last_seen_at: datetime
    first_seen_at: datetime


_PUNCTUATION_RE = re.compile(r"[^\w\s]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(question: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace. Two doubts
    with the same shape but different capitalization / trailing
    punctuation cluster together."""
    s = question.strip().lower()
    s = _PUNCTUATION_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip()


async def find_doubt_candidates_for_handover(
    *,
    repo: EntityRepository,
    handover_doc_id: uuid.UUID,
    min_occurrences: int = _DEFAULT_MIN_OCCURRENCES,
    limit: int = _DEFAULT_CANDIDATE_LIMIT,
) -> list[DoubtCandidate]:
    """Find recurring unresolved doubts that should arguably live in
    the handover doc.

    Returns clusters keyed on ``normalized_question`` with
    ``occurrence_count >= min_occurrences``, ordered by recency.
    Empty list when the handover doesn't exist or no candidates
    cross the threshold — UI hides the panel in that case."""
    doc = await repo.get_handover_doc(handover_doc_id)
    if doc is None:
        return []

    sessions = await _find_sessions_matching_handover(repo, doc)
    if not sessions:
        logger.info(
            "loopback: handover=%s/%s has no matching sessions",
            doc.scope_kind,
            doc.scope_value,
        )
        return []

    doubts = await _collect_open_risk_doubts(repo, sessions)
    if len(doubts) < min_occurrences:
        return []

    clusters = _cluster_doubts(doubts)
    candidates = [c for c in clusters if c.occurrence_count >= min_occurrences]
    candidates.sort(key=lambda c: c.last_seen_at, reverse=True)
    truncated = candidates[:limit]

    logger.info(
        "loopback: handover=%s/%s sessions_scanned=%d doubts_found=%d "
        "clusters_total=%d candidates_returned=%d",
        doc.scope_kind,
        doc.scope_value,
        len(sessions),
        len(doubts),
        len(clusters),
        len(truncated),
    )
    return truncated


async def _find_sessions_matching_handover(
    repo: EntityRepository,
    doc: HandoverDoc,
) -> list[CollaborationSession]:
    """Recent sessions whose requirement_text or any requester message
    mentions the handover's ``scope_value`` (case-insensitive,
    word-boundary-ish — same matcher style as ``matching.py``)."""
    if not doc.scope_value:
        return []

    # Tokenize the scope_value the same way the scope matcher does so
    # ``order-intake`` matches "order-intake" and "order intake" but
    # not "reorder intake".
    tokens = re.findall(r"[a-z0-9]+", doc.scope_value.lower())
    if not tokens:
        return []
    pattern = r"\b" + r"[\s\-_]*".join(re.escape(t) for t in tokens) + r"\b"
    needle = re.compile(pattern, re.IGNORECASE)

    stmt = (
        select(CollaborationSession)
        .order_by(CollaborationSession.created_at.desc())
        .limit(_DEFAULT_SESSION_LOOKBACK)
    )
    rows = (await repo._session.execute(stmt)).scalars().all()

    matched: list[CollaborationSession] = []
    for s in rows:
        if needle.search(s.requirement_text or ""):
            matched.append(s)
            continue
        # Fall back to scanning the chat history for late-mentioned
        # scope values (e.g. user pivots into order-intake mid-thread).
        # Bounded: only check requester turns to keep it cheap.
        msgs = await repo.list_collab_messages(s.id)
        if any(m.role == "requester" and needle.search(m.content_md or "") for m in msgs):
            matched.append(s)
    return matched


async def _collect_open_risk_doubts(
    repo: EntityRepository,
    sessions: Iterable[CollaborationSession],
) -> list[CollabDoubt]:
    """Pull doubts in status ``unresolved`` or ``escalated`` from the
    given sessions. ``open`` doubts are still actively being
    clarified — only the explicitly-accepted-as-risk and the
    escalated ones are loop-back candidates."""
    out: list[CollabDoubt] = []
    for s in sessions:
        for d in await repo.list_collab_doubts(s.id):
            if d.status in ("unresolved", "escalated"):
                out.append(d)
    return out


def _cluster_doubts(doubts: Sequence[CollabDoubt]) -> list[DoubtCandidate]:
    """Cluster doubts by semantic similarity with a normalized-
    string fast-path and a graceful fallback to pure normalized
    clustering on any embedding failure.

    Steps:
      1. Bucket by ``_normalize(question)`` so trivially-identical
         doubts cluster instantly without paying the embedding cost.
      2. Embed each bucket's representative (longest) question with
         sentence-transformers; greedy-assign each bucket to the
         nearest existing cluster centroid above the cosine-similarity
         threshold. New cluster otherwise.
      3. On any embedding error → return the fast-path buckets as
         clusters (this is exactly the normalized-string behavior). Function never
         raises.
    """
    # ── Step 1: fast-path normalized buckets ─────────────────────────
    buckets: dict[str, list[CollabDoubt]] = {}
    for d in doubts:
        norm = _normalize(d.question)
        if not norm:
            continue
        buckets.setdefault(norm, []).append(d)

    if not buckets:
        return []

    # ── Step 2: try embedding-based merge of buckets ─────────────────
    try:
        return _cluster_buckets_by_embedding(buckets)
    except Exception as exc:  # noqa: BLE001 — broad: any st/torch issue
        logger.warning(
            "loopback: embedding clustering failed (%s: %s); "
            "falling back to normalized-string clustering",
            type(exc).__name__,
            exc,
        )
        return [_bucket_to_candidate(norm, group) for norm, group in buckets.items()]


def _cluster_buckets_by_embedding(
    buckets: dict[str, list[CollabDoubt]],
) -> list[DoubtCandidate]:
    """Greedy embedding-similarity merge of normalized buckets into
    semantic clusters. Order-dependent (greedy) but stable — buckets
    are processed by insertion order which mirrors doubt arrival.

    Each bucket contributes one embedding (its longest question, the
    same string we'd return as the cluster's representative). Buckets
    are merged into a cluster when the cosine similarity to that
    cluster's centroid is ``>= threshold``; otherwise a new cluster
    is seeded. Centroids are running means of member embeddings,
    L2-normalized so cosine == dot.
    """
    # Lazy import — keeps module-import-time cheap and avoids surfacing
    # sentence-transformers init errors at module load. The
    # ``embed_texts`` helper itself caches the model process-wide.
    from bifrost.research.embeddings import embed_texts

    threshold = _cluster_threshold()
    bucket_items: list[tuple[str, list[CollabDoubt]]] = list(buckets.items())

    # One embedding per bucket — using the longest question as the
    # canonical text (it's what we'd display as ``representative``).
    rep_texts = [max(group, key=lambda d: len(d.question)).question for _, group in bucket_items]
    embeddings = embed_texts(rep_texts)  # L2-normalized by default
    if len(embeddings) != len(bucket_items):
        # Defensive: never seen in practice but if the embedder drops
        # rows we'd silently cluster wrong things together.
        raise RuntimeError(
            f"embed_texts returned {len(embeddings)} vectors for {len(bucket_items)} buckets",
        )

    # Each cluster: list of bucket indices + running centroid (sum
    # vector + member count; normalize when comparing).
    cluster_members: list[list[int]] = []
    cluster_sums: list[list[float]] = []
    cluster_counts: list[int] = []

    for idx, vec in enumerate(embeddings):
        best_cluster = -1
        best_sim = -1.0
        for c_idx in range(len(cluster_members)):
            centroid = _normalize_vec(cluster_sums[c_idx])
            sim = _dot(centroid, vec)
            if sim > best_sim:
                best_sim = sim
                best_cluster = c_idx
        if best_cluster >= 0 and best_sim >= threshold:
            cluster_members[best_cluster].append(idx)
            cluster_sums[best_cluster] = _add_vec(
                cluster_sums[best_cluster],
                vec,
            )
            cluster_counts[best_cluster] += 1
        else:
            cluster_members.append([idx])
            cluster_sums.append(list(vec))
            cluster_counts.append(1)

    # ── Step 3: materialize DoubtCandidate per cluster ───────────────
    clusters: list[DoubtCandidate] = []
    for member_indices in cluster_members:
        # Flatten all CollabDoubt rows from every bucket in this cluster.
        group: list[CollabDoubt] = []
        norms: list[str] = []
        for b_idx in member_indices:
            norm, doubts_in_bucket = bucket_items[b_idx]
            group.extend(doubts_in_bucket)
            norms.append(norm)
        # Pick representative = longest question across the whole cluster.
        rep_doubt = max(group, key=lambda d: len(d.question))
        # Normalized-question label = the bucket whose longest question
        # is the cluster representative — keeps API-level invariant
        # ``_normalize(representative_question) == normalized_question``.
        rep_norm = _normalize(rep_doubt.question)
        clusters.append(
            DoubtCandidate(
                normalized_question=rep_norm or norms[0],
                representative_question=rep_doubt.question,
                occurrence_count=len({str(d.session_id) for d in group}),
                session_ids=sorted({str(d.session_id) for d in group}),
                last_seen_at=max(d.created_at for d in group),
                first_seen_at=min(d.created_at for d in group),
            ),
        )
    return clusters


def _bucket_to_candidate(
    norm: str,
    group: list[CollabDoubt],
) -> DoubtCandidate:
    """Materialize one normalized bucket as a DoubtCandidate — used
    by the fallback path so its output shape matches the embedding
    path exactly."""
    sessions = sorted({str(d.session_id) for d in group})
    representative = max(group, key=lambda d: len(d.question)).question
    return DoubtCandidate(
        normalized_question=norm,
        representative_question=representative,
        occurrence_count=len(sessions),
        session_ids=sessions,
        last_seen_at=max(d.created_at for d in group),
        first_seen_at=min(d.created_at for d in group),
    )


# ── Tiny vector ops — avoids pulling numpy into this module ──────────


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _add_vec(a: list[float], b: list[float]) -> list[float]:
    return [x + y for x, y in zip(a, b, strict=False)]


def _normalize_vec(v: list[float]) -> list[float]:
    """L2-normalize so dot product == cosine similarity. Returns the
    input unchanged when norm is zero (shouldn't happen with MiniLM
    output, but defensive)."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return v
    return [x / norm for x in v]
