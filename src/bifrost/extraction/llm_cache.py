"""Content-addressed LLM response cache — the cost-control chokepoint.

Every ``claude -p`` call is content-addressed by a hash of the FULL request
(system prompt + user message + model + json schema + max_turns + tool/dir
config). On an exact re-hit, the stored response is returned for $0 instead of
re-spawning the subprocess and re-burning output tokens (the dominant cost).

This is the single cross-cutting lever that helps every feature: re-extraction
of unchanged file batches (phases D/E — 56% of burn), repeated triage, research
sub-agents on identical scope, and benchmark re-runs all become free on the
second pass.

Storage reuses the ``knowledge_text_cache`` KV table but writes **directly**
(zlib-compressed, NO truncation) — unlike ``KnowledgeCache.set`` which enforces
a 5K-token budget that would corrupt a large extraction payload.

Every DB operation is **defensive**: any error (no DB, schema drift, conflict)
is swallowed and the caller proceeds with a live LLM call. Caching can make
things cheaper; it can never make them break.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bifrost.cli.config import BifrostSettings

logger = logging.getLogger(__name__)

_PREFIX = "llmcache:"


def make_cache_key(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    json_schema: dict | None = None,
    max_turns: int = 1,
    allowed_tools: list[str] | None = None,
    add_dirs: list[str] | None = None,
) -> str:
    """Deterministic key for a request. Anything that changes the output must
    be in here, so two keys collide iff the calls are genuinely identical."""
    payload = json.dumps(
        {
            "s": system_prompt,
            "u": user_message,
            "m": model,
            "j": json_schema,
            "t": max_turns,
            "tools": sorted(allowed_tools or []),
            "dirs": sorted(add_dirs or []),
        },
        sort_keys=True,
        default=str,
    )
    return _PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LlmResponseCache:
    """Defensive KV cache for LLM completions over ``knowledge_text_cache``."""

    def __init__(self, settings: BifrostSettings | None = None) -> None:
        from bifrost.cli.config import get_settings

        self._settings = settings or get_settings()
        self._session_factory: Any = None

    def _factory(self) -> Any:
        if self._session_factory is None:
            from bifrost.api.deps import get_session_factory

            self._session_factory = get_session_factory(self._settings)
        return self._session_factory

    async def get(self, key: str) -> dict | None:
        """Return the cached payload dict, or None on miss / any failure."""
        try:
            from sqlalchemy import select

            from bifrost.storage.models import KnowledgeTextCache

            async with self._factory()() as session:
                row = (
                    await session.execute(
                        select(KnowledgeTextCache).where(KnowledgeTextCache.path == key)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                raw = zlib.decompress(bytes.fromhex(row.content)).decode("utf-8")
                return json.loads(raw)
        except Exception:  # noqa: BLE001 — caching must never break a call
            logger.debug("llm_cache.get failed (non-fatal)", exc_info=True)
            return None

    async def record_saving(self, *, model: str, saved_usd: float) -> None:
        """Log a cache hit as a skipped row in ``llm_cost_log`` so the Spend
        screen can show hit count + $ avoided. Defensive (non-fatal)."""
        try:
            from bifrost.storage.models import LLMCostLog

            row = LLMCostLog(
                task_id="cache",
                phase="cache",
                model=model or "unknown",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=float(saved_usd or 0.0),
                skipped=True,
                skip_reason="llm_cache_hit",
            )
            async with self._factory()() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("llm_cache.record_saving failed (non-fatal)", exc_info=True)

    async def put(self, key: str, payload: dict) -> None:
        """Store the payload. No-op on any failure (defensive)."""
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from bifrost.storage.models import KnowledgeTextCache

            blob = zlib.compress(json.dumps(payload, default=str).encode("utf-8"), 6).hex()
            content_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
            stmt = (
                pg_insert(KnowledgeTextCache)
                .values(path=key, content=blob, content_hash=content_hash)
                .on_conflict_do_update(
                    index_elements=["path"],
                    set_={"content": blob, "content_hash": content_hash},
                )
            )
            async with self._factory()() as session:
                await session.execute(stmt)
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("llm_cache.put failed (non-fatal)", exc_info=True)
