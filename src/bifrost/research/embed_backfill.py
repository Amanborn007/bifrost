"""Batch-populate embeddings for entities / Confluence pages / Jira issues.

Idempotent: by default only processes rows WHERE embedding IS NULL.
Pass ``force=True`` to recompute everything (e.g. after a model change).

Typical invocation via the /admin/embed endpoint:
    POST /api/v1/admin/embed/entities      → backfills all entity rows
    POST /api/v1/admin/embed/confluence    → all cached Confluence pages
    POST /api/v1/admin/embed/jira          → all cached Jira issues
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.research.embeddings import (
    confluence_embed_text,
    embed_texts,
    entity_embed_text,
    jira_embed_text,
)
from bifrost.storage.models import ConfluencePage, Entity, JiraIssue

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    table: str
    total_candidates: int
    embedded: int
    errors: int


async def backfill_entity_embeddings(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 64,
    force: bool = False,
) -> BackfillResult:
    """Embed every entity (or every NULL-embedding entity if force=False)."""
    async with session_factory() as session:
        stmt = select(
            Entity.id,
            Entity.name,
            Entity.entity_type,
            Entity.qualified_name,
            Entity.entity_metadata,
        )
        if not force:
            stmt = stmt.where(Entity.embedding.is_(None))
        rows = (await session.execute(stmt)).all()

    total = len(rows)
    if total == 0:
        return BackfillResult("entities", 0, 0, 0)

    embedded = 0
    errors = 0
    logger.info("Backfilling %d entity embeddings (batch=%d)", total, batch_size)

    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        texts = [
            entity_embed_text(
                name=r.name,
                entity_type=r.entity_type,
                purpose=(r.entity_metadata or {}).get("purpose"),
                qualified_name=r.qualified_name,
                source_file=(r.entity_metadata or {}).get("source_file"),
            )
            for r in batch
        ]
        try:
            vectors = embed_texts(texts)
        except Exception:
            logger.exception("embed batch failed at %d", start)
            errors += len(batch)
            continue

        async with session_factory() as session:
            for row, vec in zip(batch, vectors, strict=True):
                try:
                    await session.execute(
                        update(Entity).where(Entity.id == row.id).values(embedding=vec),
                    )
                    embedded += 1
                except Exception:
                    logger.exception("entity update failed id=%s", row.id)
                    errors += 1
            await session.commit()
        logger.info("  embedded %d / %d entities", embedded, total)

    return BackfillResult("entities", total, embedded, errors)


async def backfill_confluence_embeddings(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 64,
    force: bool = False,
) -> BackfillResult:
    async with session_factory() as session:
        stmt = select(
            ConfluencePage.id,
            ConfluencePage.title,
            ConfluencePage.summary,
            ConfluencePage.body_excerpt,
        )
        if not force:
            stmt = stmt.where(ConfluencePage.embedding.is_(None))
        rows = (await session.execute(stmt)).all()

    total = len(rows)
    if total == 0:
        return BackfillResult("confluence_pages", 0, 0, 0)

    embedded = errors = 0
    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        texts = [
            confluence_embed_text(
                title=r.title,
                summary=r.summary,
                body_excerpt=r.body_excerpt,
            )
            for r in batch
        ]
        try:
            vectors = embed_texts(texts)
        except Exception:
            logger.exception("confluence embed batch failed at %d", start)
            errors += len(batch)
            continue
        async with session_factory() as session:
            for row, vec in zip(batch, vectors, strict=True):
                await session.execute(
                    update(ConfluencePage).where(ConfluencePage.id == row.id).values(embedding=vec),
                )
                embedded += 1
            await session.commit()

    return BackfillResult("confluence_pages", total, embedded, errors)


async def backfill_jira_embeddings(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 64,
    force: bool = False,
) -> BackfillResult:
    async with session_factory() as session:
        stmt = select(
            JiraIssue.id,
            JiraIssue.issue_key,
            JiraIssue.title,
            JiraIssue.summary,
            JiraIssue.description_excerpt,
            JiraIssue.labels,
        )
        if not force:
            stmt = stmt.where(JiraIssue.embedding.is_(None))
        rows = (await session.execute(stmt)).all()

    total = len(rows)
    if total == 0:
        return BackfillResult("jira_issues", 0, 0, 0)

    embedded = errors = 0
    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        texts = [
            jira_embed_text(
                issue_key=r.issue_key,
                title=r.title,
                summary=r.summary,
                description_excerpt=r.description_excerpt,
                labels=list(r.labels) if r.labels else None,
            )
            for r in batch
        ]
        try:
            vectors = embed_texts(texts)
        except Exception:
            logger.exception("jira embed batch failed at %d", start)
            errors += len(batch)
            continue
        async with session_factory() as session:
            for row, vec in zip(batch, vectors, strict=True):
                await session.execute(
                    update(JiraIssue).where(JiraIssue.id == row.id).values(embedding=vec),
                )
                embedded += 1
            await session.commit()

    return BackfillResult("jira_issues", total, embedded, errors)
