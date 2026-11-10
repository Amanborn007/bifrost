"""Per-conversation research-bundle cache.

A "research bundle" is the metadata produced by one chat turn's
research pass — which repos contributed, which entity URNs were
walked, which Jira/Confluence/PR sources were consulted, the prior
answer's text, and (optionally) the embedding of the asking question.
The follow-up planner reads the prior bundle to decide whether the
next turn can ``EXTEND_BUNDLE`` (reuse the subgraph, fan out to a
narrow new repo set) or ``CLARIFY`` (answer purely from the bundle)
instead of doing a fresh research pass.

This module owns the in-memory side of that store: an ``asyncio.Lock``-
protected ``OrderedDict`` with TTL eviction. The repository layer
(``bifrost.storage.repository``) writes the same bundle through to
``conversation_research_state`` so an API restart or a multi-replica
deployment doesn't lose the context.

Bundle contents are deliberately metadata-only — entity URNs, source
IDs, summary strings — never raw entity bodies or LLM outputs. That
keeps both the in-memory dict and the JSONB column bounded.

Closes part of the PRD §3.7 gap (chat that learns from prior turns).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

_DEFAULT_TTL_SECONDS = 60 * 60  # 1 hour — covers a typical chat session
_DEFAULT_MAX_SIZE = 256  # bounded; eviction is LRU-by-insertion
_DEFAULT_MAX_SUBGRAPH_NODES = 200  # cap stored URN list to keep payload small
_DEFAULT_MAX_SOURCES = 50


@dataclass
class BundleSource:
    """Reference to one external source consulted during research.

    Stays compact: source-system identifier + display title only.
    """

    kind: str  # "jira" | "confluence" | "github_pr" | "slack" | "repo"
    ref: str  # ticket key, page id, PR url, etc.
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchBundle:
    """One chat turn's research metadata, retained for follow-ups."""

    conversation_id: str
    last_question: str
    last_answer: str = ""
    confidence: str = "uncertain"  # confirmed / inferred / uncertain
    repos_used: list[str] = field(default_factory=list)  # repo UUIDs
    repos_used_names: list[str] = field(default_factory=list)
    scope_rationale: str = ""
    subgraph_entity_urns: list[str] = field(default_factory=list)  # walked URNs
    sources: list[BundleSource] = field(default_factory=list)
    last_question_embedding: list[float] | None = None  # 1536-dim, optional
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def trim(
        self,
        *,
        max_subgraph_nodes: int = _DEFAULT_MAX_SUBGRAPH_NODES,
        max_sources: int = _DEFAULT_MAX_SOURCES,
    ) -> ResearchBundle:
        """Return a copy with bounded list lengths.

        Long subgraph walks and large source sets bloat memory + DB
        rows. Truncation here is fine because the canonical entities
        live in Postgres and the bundle only carries pointers.
        """
        return ResearchBundle(
            conversation_id=self.conversation_id,
            last_question=self.last_question[:4000],
            last_answer=self.last_answer[:8000],
            confidence=self.confidence,
            repos_used=list(self.repos_used),
            repos_used_names=list(self.repos_used_names),
            scope_rationale=self.scope_rationale[:2000],
            subgraph_entity_urns=list(self.subgraph_entity_urns)[:max_subgraph_nodes],
            sources=list(self.sources)[:max_sources],
            last_question_embedding=self.last_question_embedding,
            created_at=self.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "last_question": self.last_question,
            "last_answer": self.last_answer,
            "confidence": self.confidence,
            "repos_used": list(self.repos_used),
            "repos_used_names": list(self.repos_used_names),
            "scope_rationale": self.scope_rationale,
            "subgraph_entity_urns": list(self.subgraph_entity_urns),
            "sources": [s.to_dict() for s in self.sources],
            "last_question_embedding": (
                list(self.last_question_embedding)
                if self.last_question_embedding is not None
                else None
            ),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchBundle:
        sources_raw = raw.get("sources") or []
        created_raw = raw.get("created_at")
        if isinstance(created_raw, datetime):
            created = created_raw
        elif isinstance(created_raw, str):
            try:
                created = datetime.fromisoformat(created_raw)
            except ValueError:
                created = datetime.now(UTC)
        else:
            created = datetime.now(UTC)
        return cls(
            conversation_id=str(raw.get("conversation_id") or ""),
            last_question=str(raw.get("last_question") or ""),
            last_answer=str(raw.get("last_answer") or ""),
            confidence=str(raw.get("confidence") or "uncertain"),
            repos_used=list(raw.get("repos_used") or []),
            repos_used_names=list(raw.get("repos_used_names") or []),
            scope_rationale=str(raw.get("scope_rationale") or ""),
            subgraph_entity_urns=list(raw.get("subgraph_entity_urns") or []),
            sources=[
                BundleSource(
                    kind=str(s.get("kind", "")),
                    ref=str(s.get("ref", "")),
                    title=str(s.get("title", "")),
                )
                for s in sources_raw
                if s.get("ref")
            ],
            last_question_embedding=(
                [float(x) for x in emb]
                if isinstance((emb := raw.get("last_question_embedding")), list) and emb
                else None
            ),
            created_at=created,
        )


class BundleCache:
    """In-memory bundle store.

    Bounded by ``max_size`` (LRU-by-insertion eviction) and ``ttl``
    (lazy expiry on read). All access is awaited via an ``asyncio.Lock``
    because chat-turn save and follow-up load can race on a busy
    conversation.
    """

    def __init__(
        self,
        *,
        max_size: int = _DEFAULT_MAX_SIZE,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._store: OrderedDict[str, tuple[float, ResearchBundle]] = OrderedDict()
        self._max = max_size
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def save(self, bundle: ResearchBundle) -> None:
        bundle = bundle.trim()
        now = time.monotonic()
        async with self._lock:
            # Update value in-place, then move to end — avoids a gap where
            # the key is absent between pop() and re-insert.
            self._store[bundle.conversation_id] = (now, bundle)
            self._store.move_to_end(bundle.conversation_id)
            self._evict_locked(now)

    async def load(self, conversation_id: str) -> ResearchBundle | None:
        now = time.monotonic()
        async with self._lock:
            entry = self._store.get(conversation_id)
            if entry is None:
                return None
            saved_at, bundle = entry
            if now - saved_at > self._ttl:
                self._store.pop(conversation_id, None)
                return None
            self._store.move_to_end(conversation_id)
            return bundle

    async def invalidate(self, conversation_id: str) -> None:
        async with self._lock:
            self._store.pop(conversation_id, None)

    async def size(self) -> int:
        async with self._lock:
            return len(self._store)

    def _evict_locked(self, now: float) -> None:
        """Drop expired + over-cap entries. Caller holds the lock."""
        # Lazy TTL pass.
        expired = [cid for cid, (saved_at, _) in self._store.items() if now - saved_at > self._ttl]
        for cid in expired:
            self._store.pop(cid, None)
        # LRU bound.
        while len(self._store) > self._max:
            self._store.popitem(last=False)


# Module-level singleton, created on first ``get_bundle_cache()`` so
# tests can monkeypatch the constructor without import-time side
# effects.
_GLOBAL_CACHE: BundleCache | None = None


def get_bundle_cache() -> BundleCache:
    global _GLOBAL_CACHE
    if _GLOBAL_CACHE is None:
        _GLOBAL_CACHE = BundleCache()
    return _GLOBAL_CACHE


def reset_bundle_cache_for_tests() -> None:
    """Drop the module singleton — used by tests that want a clean store."""
    global _GLOBAL_CACHE
    _GLOBAL_CACHE = None
