"""EntityRepository — async CRUD for the knowledge graph.

Single data access layer for all entity, relationship, and repo operations.
Uses SQLAlchemy 2.0 async sessions with INSERT ... ON CONFLICT for upserts.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC
from typing import Any

from sqlalchemy import String, and_, case, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.storage.models import (
    CollabArtifact,
    CollabDoubt,
    CollabMessage,
    CollaborationSession,
    Conversation,
    ConversationMessage,
    ConversationResearchState,
    Domain,
    Entity,
    EntityRelationship,
    HandoverAudit,
    HandoverBodyHistory,
    HandoverDoc,
    HandoverSectionFeedback,
    KnowledgeTextCache,
    OrgRepo,
    Repo,
    RepoFreshness,
)
from bifrost.storage.models import (
    ExtractionTask as Task,
)
from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)


# Singleton scanner — reused across all chat / output_log persistence sites
# so the regex compiles once. Closes the gap where assistant
# content was persisted without scrubbing.
_SCANNER = None


def _scrub_secrets(text: str, *, source: str = "chat") -> str:
    """Run the extraction-time secret scanner on ``text`` and return the
    redacted variant.

    Keeps ``SecretScanner`` lazy-imported so the storage layer doesn't pull
    the extraction package at module-import time (avoids circular imports
    in the migration alembic workers).
    """
    global _SCANNER
    if _SCANNER is None:
        from bifrost.extraction.secrets import SecretScanner

        _SCANNER = SecretScanner()
    result = _SCANNER.scan_and_redact(text, file_path=source)
    return result.redacted_content


def _build_tsquery(query: str) -> str:
    """Convert a free-text search ``query`` to a ``to_tsquery``-safe
    AND-joined boolean expression.

    ``to_tsquery`` parses its input as a strict boolean syntax —
    unbalanced parens, stray ``&`` / ``|`` / ``!`` / ``:`` characters
    or punctuation will raise. The route caller is doing free-text
    search, so we sanitise here: split on whitespace, drop anything
    that isn't a word/digit/dash/underscore, lower-case, and
    AND-combine the survivors. Tokens shorter than 2 chars are
    dropped (would match too aggressively against the ``english``
    dictionary's stopword list).

    Returns an empty string if no usable token survives — the caller
    treats that as "empty result set" rather than running an invalid
    query.
    """
    if not query:
        return ""
    safe_tokens: list[str] = []
    for raw in query.split():
        # Keep word chars, digits, dash, underscore. ``:`` and ``&``
        # would be interpreted by to_tsquery; drop them.
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
        if len(cleaned) < 2:
            continue
        safe_tokens.append(cleaned.lower())
    return " & ".join(safe_tokens)


def _orm_to_column_values(model: type, values: dict[str, Any]) -> dict[str, Any]:
    """Map ORM attribute names to DB column keys for raw INSERT statements.

    Handles cases where the ORM attribute name differs from the DB column
    name (e.g. ``entity_metadata`` → ``metadata``).
    """
    mapper = model.__mapper__  # type: ignore[attr-defined]
    attr_to_col: dict[str, str] = {}
    for prop in mapper.column_attrs:
        for col in prop.columns:
            attr_to_col[prop.key] = col.key
    return {attr_to_col.get(k, k): v for k, v in values.items()}


class EntityRepository:
    """Async CRUD repository for all knowledge graph tables."""

    def __init__(self, session: AsyncSession, *, skip_session_check: bool = False) -> None:
        self._session = session
        # True only under identity.provider=none, where one local operator
        # owns every conversation.
        self._skip_session_check = skip_session_check

    async def commit(self) -> None:
        """Make pending writes durable before handing ids to another session."""
        await self._session.commit()

    # ── Repos ─────────────────────────────────────────────────────────

    async def upsert_repo(self, **values: Any) -> Repo:
        """Insert or update a repo by URN."""
        stmt = (
            pg_insert(Repo)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["urn"],
                set_={k: v for k, v in values.items() if k != "id"},
            )
            .returning(Repo)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_repo(self, repo_id: uuid.UUID) -> Repo | None:
        result = await self._session.execute(select(Repo).where(Repo.id == repo_id))
        return result.scalar_one_or_none()

    async def get_repo_by_urn(self, urn: str) -> Repo | None:
        result = await self._session.execute(select(Repo).where(Repo.urn == urn))
        return result.scalar_one_or_none()

    async def list_repos(self) -> Sequence[Repo]:
        result = await self._session.execute(select(Repo).order_by(Repo.name))
        return result.scalars().all()

    # ── Domains ───────────────────────────────────────────────────────

    async def upsert_domain(self, **values: Any) -> Domain:
        stmt = (
            pg_insert(Domain)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["urn"],
                set_={k: v for k, v in values.items() if k != "id"},
            )
            .returning(Domain)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_domain(self, domain_id: uuid.UUID) -> Domain | None:
        result = await self._session.execute(select(Domain).where(Domain.id == domain_id))
        return result.scalar_one_or_none()

    async def list_domains(self) -> Sequence[Domain]:
        result = await self._session.execute(select(Domain).order_by(Domain.name))
        return result.scalars().all()

    # ── Entities ──────────────────────────────────────────────────────

    async def upsert_entity(self, **values: Any) -> Entity:
        """Insert or update an entity by URN.

        Accepts ORM attribute names (e.g. ``entity_metadata``). They are
        mapped to the real DB column names before being passed to the raw
        INSERT statement.
        """
        # Translate ORM attribute names → DB column keys
        col_values = _orm_to_column_values(Entity, values)
        update_set = {k: v for k, v in col_values.items() if k not in ("id", "created_at")}

        stmt = (
            pg_insert(Entity.__table__)
            .values(**col_values)
            .on_conflict_do_update(
                index_elements=["urn"],
                set_=update_set,
            )
            .returning(Entity.__table__)
        )
        result = await self._session.execute(stmt)
        row = result.mappings().one()
        return await self._session.get(Entity, row["id"])  # type: ignore[return-value]

    async def get_entity(self, entity_id: uuid.UUID) -> Entity | None:
        result = await self._session.execute(select(Entity).where(Entity.id == entity_id))
        return result.scalar_one_or_none()

    async def get_entity_by_urn(self, urn: str) -> Entity | None:
        result = await self._session.execute(select(Entity).where(Entity.urn == urn))
        return result.scalar_one_or_none()

    async def list_entities(
        self,
        entity_type: str | None = None,
        repo_id: uuid.UUID | None = None,
        domain_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Entity]:
        stmt = select(Entity)
        if entity_type is not None:
            stmt = stmt.where(Entity.entity_type == entity_type)
        if repo_id is not None:
            stmt = stmt.where(Entity.scope_repo_id == repo_id)
        if domain_id is not None:
            stmt = stmt.where(Entity.scope_domain_id == domain_id)
        stmt = stmt.order_by(Entity.name).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def search_entities_by_embedding(
        self,
        embedding: list[float],
        limit: int = 10,
        entity_type: str | None = None,
    ) -> Sequence[tuple[Entity, float]]:
        """Find entities similar to the given embedding using pgvector cosine distance.

        Returns (entity, distance) pairs ordered by ascending distance (most similar first).
        """
        stmt = select(
            Entity,
            Entity.embedding.cosine_distance(embedding).label("distance"),
        ).where(Entity.embedding.isnot(None))

        if entity_type is not None:
            stmt = stmt.where(Entity.entity_type == entity_type)

        stmt = stmt.order_by("distance").limit(limit)
        result = await self._session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def batch_upsert_entities(self, entities: list[dict[str, Any]]) -> int:
        """Upsert multiple entities in a single INSERT ... ON CONFLICT statement.

        Returns the number of rows affected.
        """
        if not entities:
            return 0
        stmt = pg_insert(Entity).values(entities)
        stmt = stmt.on_conflict_do_update(
            index_elements=["urn"],
            set_={
                "name": stmt.excluded.name,
                "entity_type": stmt.excluded.entity_type,
                "qualified_name": stmt.excluded.qualified_name,
                "knowledge_tier": stmt.excluded.knowledge_tier,
                "metadata": stmt.excluded.metadata,
                "confidence": stmt.excluded.confidence,
                "confidence_level": stmt.excluded.confidence_level,
                "source_basis": stmt.excluded.source_basis,
                "freshness_state": stmt.excluded.freshness_state,
            },
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def search_entities_by_keyword(
        self,
        query: str,
        repo_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        limit: int = 20,
    ) -> Sequence[tuple[Entity, int]]:
        """Search entities by keyword matching against name, qualified_name, and metadata.

        Returns (entity, match_count) pairs ordered by most matches first.
        """
        raw_terms = [t.lower() for t in query.split() if len(t) > 2]
        if not raw_terms:
            return []
        # Basic stemming: also include singular forms
        terms: list[str] = []
        for t in raw_terms:
            terms.append(t)
            if t.endswith("ies"):
                terms.append(t[:-3] + "y")
            elif t.endswith("es"):
                terms.append(t[:-2])
            elif t.endswith("s") and not t.endswith("ss"):
                terms.append(t[:-1])
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_terms: list[str] = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                unique_terms.append(t)
        terms = unique_terms

        # Build a score column: count how many terms match any searchable field
        metadata_text = func.coalesce(cast(Entity.entity_metadata, String), "")
        match_exprs = []
        for term in terms:
            pattern = f"%{term}%"
            match_exprs.append(
                case(
                    (
                        or_(
                            func.lower(Entity.name).like(pattern),
                            func.lower(func.coalesce(Entity.qualified_name, "")).like(pattern),
                            func.lower(func.coalesce(Entity.entity_type, "")).like(pattern),
                            func.lower(metadata_text).like(pattern),
                        ),
                        1,
                    ),
                    else_=0,
                )
            )

        # Sum the match expressions using SQLAlchemy's + operator
        score_expr = match_exprs[0]
        for expr in match_exprs[1:]:
            score_expr = score_expr + expr  # type: ignore[assignment]
        score = score_expr.label("score")

        stmt = select(Entity, score).where(score > 0)

        if repo_id is not None:
            stmt = stmt.where(Entity.scope_repo_id == repo_id)
        if entity_type is not None:
            stmt = stmt.where(Entity.entity_type == entity_type)

        stmt = stmt.order_by(score.desc(), Entity.name).limit(limit)
        result = await self._session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    # ── Relationships ─────────────────────────────────────────────────

    async def upsert_relationship(self, **values: Any) -> EntityRelationship:
        col_values = _orm_to_column_values(EntityRelationship, values)
        update_set = {
            k: v
            for k, v in col_values.items()
            if k not in ("id", "source_id", "target_id", "rel_type", "created_at")
        }
        # ON CONFLICT DO UPDATE requires at least one column in SET
        if not update_set:
            update_set = {"rel_type": col_values["rel_type"]}
        stmt = (
            pg_insert(EntityRelationship.__table__)
            .values(**col_values)
            .on_conflict_do_update(
                constraint="uq_rel_unique_edge",
                set_=update_set,
            )
            .returning(EntityRelationship.__table__)
        )
        result = await self._session.execute(stmt)
        row = result.mappings().one()
        return await self._session.get(EntityRelationship, row["id"])  # type: ignore[return-value]

    async def get_relationships(
        self,
        entity_id: uuid.UUID,
        direction: str = "outgoing",
        rel_type: str | None = None,
    ) -> Sequence[EntityRelationship]:
        """Get relationships for an entity. direction: 'outgoing', 'incoming', or 'both'."""
        stmts = []
        if direction in ("outgoing", "both"):
            stmt = select(EntityRelationship).where(EntityRelationship.source_id == entity_id)
            if rel_type:
                stmt = stmt.where(EntityRelationship.rel_type == rel_type)
            stmts.append(stmt)
        if direction in ("incoming", "both"):
            stmt = select(EntityRelationship).where(EntityRelationship.target_id == entity_id)
            if rel_type:
                stmt = stmt.where(EntityRelationship.rel_type == rel_type)
            stmts.append(stmt)

        results: list[EntityRelationship] = []
        for s in stmts:
            result = await self._session.execute(s)
            results.extend(result.scalars().all())
        return results

    async def batch_upsert_relationships(self, relationships: list[dict[str, Any]]) -> int:
        """Batch upsert relationships using INSERT ... ON CONFLICT."""
        if not relationships:
            return 0
        stmt = pg_insert(EntityRelationship).values(relationships)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_rel_unique_edge",
            set_={
                "rel_metadata": stmt.excluded.rel_metadata,
                "confidence": stmt.excluded.confidence,
                "source_basis": stmt.excluded.source_basis,
                "freshness_state": stmt.excluded.freshness_state,
            },
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    # ── Repo Freshness ────────────────────────────────────────────────

    async def upsert_repo_freshness(self, **values: Any) -> RepoFreshness:
        stmt = (
            pg_insert(RepoFreshness)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["repo_id"],
                set_={k: v for k, v in values.items() if k != "repo_id"},
            )
            .returning(RepoFreshness)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_repo_freshness(self, repo_id: uuid.UUID) -> RepoFreshness | None:
        result = await self._session.execute(
            select(RepoFreshness).where(RepoFreshness.repo_id == repo_id)
        )
        return result.scalar_one_or_none()

    # ── Tasks ─────────────────────────────────────────────────────────

    async def create_task(self, **values: Any) -> Task:
        task = Task(**values)
        self._session.add(task)
        await self._session.flush()
        return task

    async def get_task(self, task_id: uuid.UUID) -> Task | None:
        result = await self._session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Task]:
        stmt = select(Task)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_entities_by_type_for_repo(self, repo_id: uuid.UUID) -> dict[str, int]:
        """Return {entity_type: count} for all entities scoped to a repo."""
        stmt = (
            select(Entity.entity_type, func.count(Entity.id))
            .where(Entity.scope_repo_id == repo_id)
            .group_by(Entity.entity_type)
        )
        result = await self._session.execute(stmt)
        return {row[0]: int(row[1]) for row in result.all()}

    async def get_latest_extraction_task_for_repo(self, repo_id: uuid.UUID) -> Task | None:
        """Most recent extraction task that targeted this repo.

        Tasks are matched by ``scope_json['repo_ids']`` containing the repo id
        as a string. V1 scale — no GIN index needed.
        """
        stmt = (
            select(Task)
            .where(Task.task_type == "extraction")
            .where(Task.scope_json["repo_ids"].astext.contains(str(repo_id)))
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Knowledge Text Cache ──────────────────────────────────────────

    async def get_knowledge_text(self, path: str) -> KnowledgeTextCache | None:
        result = await self._session.execute(
            select(KnowledgeTextCache).where(KnowledgeTextCache.path == path)
        )
        return result.scalar_one_or_none()

    async def upsert_knowledge_text(self, **values: Any) -> KnowledgeTextCache:
        stmt = (
            pg_insert(KnowledgeTextCache)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["path"],
                set_={k: v for k, v in values.items() if k != "path"},
            )
            .returning(KnowledgeTextCache)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def list_knowledge_texts_by_repo(
        self, repo_id: uuid.UUID
    ) -> Sequence[KnowledgeTextCache]:
        result = await self._session.execute(
            select(KnowledgeTextCache)
            .where(KnowledgeTextCache.repo_id == repo_id)
            .order_by(KnowledgeTextCache.path)
        )
        return result.scalars().all()

    async def delete_knowledge_text(self, path: str) -> bool:
        row = await self.get_knowledge_text(path)
        if row is None:
            return False
        await self._session.delete(row)
        return True

    # ── Conversations (chat threads) ───────────────────────────────────────

    async def create_conversation(
        self,
        *,
        session_id: str,
        title: str,
        scope_repo_ids: list[str] | None = None,
        scope_domain_id: uuid.UUID | None = None,
        owner_email: str | None = None,
    ) -> Conversation:
        conv = Conversation(
            session_id=session_id,
            owner_email=owner_email,
            title=title[:255],
            scope_repo_ids=scope_repo_ids or [],
            scope_domain_id=scope_domain_id,
        )
        self._session.add(conv)
        await self._session.flush()
        return conv

    async def get_conversation(self, conv_id: uuid.UUID) -> Conversation | None:
        return await self._session.get(Conversation, conv_id)

    async def get_chat_settings(self, conv_id: uuid.UUID) -> dict[str, Any]:
        """Return the raw ``settings_json`` dict for a conversation,
        or ``{}`` if the conv is missing or has no settings yet. Caller
        wraps with
        :func:`bifrost.api.schemas.chat_settings.settings_from_stored`
        to apply Pydantic defaults — keeping this layer schema-free
        means a new knob doesn't need a repository edit.
        """
        conv = await self._session.get(Conversation, conv_id)
        if conv is None:
            return {}
        return dict(conv.settings_json or {})

    async def update_chat_settings(
        self,
        conv_id: uuid.UUID,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge ``patch`` into the stored JSONB and return the
        merged dict (raw — caller wraps in ``ChatSettings`` to
        validate). Raises ``ValueError`` when the conv doesn't
        exist; the route layer translates to 404.
        """
        conv = await self._session.get(Conversation, conv_id)
        if conv is None:
            raise ValueError(f"conversation {conv_id} not found")
        merged = dict(conv.settings_json or {})
        merged.update(patch)
        conv.settings_json = merged
        await self._session.flush()
        return merged

    async def get_conversation_for_session(
        self,
        conv_id: uuid.UUID,
        *,
        session_id: str,
        include_deleted: bool = False,
    ) -> Conversation | None:
        """Get a conversation, but only if ``session_id`` owns it and
        (by default) it isn't soft-deleted.

        Use this on every per-conversation route — get / append /
        rename / delete / archive / stream — so a leaked conversation
        UUID can't be exploited by a different cookie session. Returns
        ``None`` for both "no such id" and "you don't own it"; the
        route layer translates both to 404 (don't leak existence).
        """
        conv = await self._session.get(Conversation, conv_id)
        if conv is None:
            return None
        if not self._skip_session_check and conv.session_id != session_id:
            return None
        if not include_deleted and conv.deleted_at is not None:
            return None
        return conv

    async def list_conversations(
        self,
        *,
        session_id: str,
        include_archived: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> Sequence[Conversation]:
        """List conversations owned by ``session_id``. Deleted rows are
        hidden by default."""
        stmt = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
        if not self._skip_session_check:
            stmt = stmt.where(Conversation.session_id == session_id)
        if not include_archived:
            stmt = stmt.where(Conversation.archived.is_(False))
        if not include_deleted:
            stmt = stmt.where(Conversation.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_conversations_with_summary(
        self,
        *,
        session_id: str,
        include_archived: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> Sequence[tuple[Conversation, int, str | None]]:
        """List conversations with message_count + last_message_preview
        in ONE query.

        The previous flow called ``list_conversations`` then iterated
        with N separate ``list_messages`` calls — 51 round-trips for
        50 rows. This single statement uses correlated subqueries so
        the planner can satisfy both columns from
        ``idx_conv_messages_conv (conversation_id, turn_index)`` with
        no joins.

        Returns ``[(Conversation, count, last_preview)]`` per row.
        """
        from sqlalchemy import select as sa_select

        # Per-conversation max turn index (the LATEST message).
        latest_turn = (
            sa_select(func.max(ConversationMessage.turn_index))
            .where(ConversationMessage.conversation_id == Conversation.id)
            .correlate(Conversation)
            .scalar_subquery()
        )

        # The latest message's content (LATERAL-style subquery, expressed
        # as a correlated SELECT pulling content where turn_index == max).
        last_preview = (
            sa_select(func.substr(ConversationMessage.content, 1, 120))
            .where(
                ConversationMessage.conversation_id == Conversation.id,
                ConversationMessage.turn_index == latest_turn,
            )
            .correlate(Conversation)
            .scalar_subquery()
        )

        # Count messages — uses the composite index by conv_id.
        msg_count = (
            sa_select(func.count(ConversationMessage.id))
            .where(ConversationMessage.conversation_id == Conversation.id)
            .correlate(Conversation)
            .scalar_subquery()
        )

        stmt = (
            select(Conversation, msg_count, last_preview)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        if not include_archived:
            stmt = stmt.where(Conversation.archived.is_(False))
        if not include_deleted:
            stmt = stmt.where(Conversation.deleted_at.is_(None))

        result = await self._session.execute(stmt)
        out: list[tuple[Conversation, int, str | None]] = []
        for row in result.all():
            conv, count, preview = row
            out.append((conv, int(count or 0), preview))
        return out

    async def rename_conversation(
        self,
        conv_id: uuid.UUID,
        title: str,
    ) -> Conversation | None:
        conv = await self.get_conversation(conv_id)
        if conv is None:
            return None
        conv.title = title[:255]
        await self._session.flush()
        return conv

    async def archive_conversation(self, conv_id: uuid.UUID) -> bool:
        from datetime import datetime

        conv = await self.get_conversation(conv_id)
        if conv is None:
            return False
        conv.archived = True
        conv.archived_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def restore_conversation(self, conv_id: uuid.UUID) -> bool:
        """Un-archive AND un-soft-delete in one call."""
        conv = await self.get_conversation(conv_id)
        if conv is None:
            return False
        conv.archived = False
        conv.archived_at = None
        conv.deleted_at = None
        await self._session.flush()
        return True

    async def delete_conversation(
        self,
        conv_id: uuid.UUID,
        *,
        hard: bool = False,
    ) -> bool:
        """Soft-delete by default.

        Soft delete sets ``deleted_at`` but keeps the row + every
        ConversationMessage. The row disappears from default lists but
        a future ``restore`` reverses it. Pass ``hard=True`` to actually
        drop the row (cascades to messages); only an admin tool should
        do that.
        """
        from datetime import datetime

        conv = await self.get_conversation(conv_id)
        if conv is None:
            return False
        if hard:
            await self._session.delete(conv)
        else:
            conv.deleted_at = datetime.now(UTC)
            conv.archived = True
            conv.archived_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def list_messages(self, conv_id: uuid.UUID) -> Sequence[ConversationMessage]:
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conv_id)
            .order_by(ConversationMessage.turn_index)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def latest_user_message_id(
        self,
        conv_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Return the most recent user-role message id (by turn_index)."""
        stmt = (
            select(ConversationMessage.id)
            .where(
                ConversationMessage.conversation_id == conv_id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.turn_index.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        return row[0] if row else None

    async def next_turn_index(self, conv_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(ConversationMessage.turn_index), -1)).where(
            ConversationMessage.conversation_id == conv_id,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one()) + 1

    async def append_message(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        sources: list[dict[str, Any]] | None = None,
        confidence: str | None = None,
        reasoning: str | None = None,
        gaps: list[str] | None = None,
        token_counts: dict[str, Any] | None = None,
        cost_usd: float | None = None,
        suggested_followups: list[dict[str, Any]] | None = None,
    ) -> ConversationMessage:
        # Scrub credential-shaped substrings before persistence.
        # Assistant turns frequently quote Jira ticket bodies, Slack
        # messages, or repo files that may contain tokens. Without this
        # pass, those tokens land in conversation_messages.content and are
        # served back via the API.
        if content:
            content = _scrub_secrets(content, source=f"chat-{role}")
        if reasoning:
            reasoning = _scrub_secrets(reasoning, source=f"chat-{role}-reasoning")

        # Dedup + sort sources by relevance DESC, name ASC
        # before persistence. Coordinator can emit the same URN multiple
        # times across sub-agent reports; without dedup the UI renders
        # duplicate citations and ordering shifts on each list.
        # ``relevance`` is CATEGORICAL ("high"/"medium"/
        # "low"/"none") on chat sources — coercing it directly with
        # ``float()`` blew up the entire persist path with
        # "could not convert string to float: 'medium'", losing every
        # synthesis answer. Map categorical → numeric, fall back to
        # parsing if a numeric str sneaks through, default to 0.
        _RELEVANCE_RANK = {
            "high": 3.0,
            "medium": 2.0,
            "low": 1.0,
            "none": 0.0,
            "skipped": 0.0,
            "": 0.0,
        }

        def _relevance_score(src: dict[str, Any]) -> float:
            v = src.get("relevance")
            if v is None:
                return 0.0
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).strip().lower()
            if s in _RELEVANCE_RANK:
                return _RELEVANCE_RANK[s]
            try:
                return float(s)
            except (ValueError, TypeError):
                return 0.0

        if sources:
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for src in sources:
                if not isinstance(src, dict):
                    continue
                urn = str(src.get("urn") or "")
                if not urn or urn in seen:
                    continue
                seen.add(urn)
                deduped.append(src)
            deduped.sort(
                key=lambda s: (
                    -_relevance_score(s),
                    str(s.get("name") or s.get("urn") or ""),
                )
            )
            sources = deduped

        turn_index = await self.next_turn_index(conversation_id)
        msg = ConversationMessage(
            conversation_id=conversation_id,
            turn_index=turn_index,
            role=role,
            content=content,
            sources=sources or [],
            confidence=confidence,
            reasoning=reasoning,
            gaps=gaps or [],
            token_counts=token_counts or {},
            suggested_followups=suggested_followups or [],
            cost_usd=cost_usd,
        )
        self._session.add(msg)
        await self._session.flush()
        # Touch the parent conversation's updated_at. ``func.now()``
        # produces a SQL ``now()`` literal that Postgres evaluates per
        # transaction, so concurrent appends in distinct transactions
        # get distinct timestamps. A Python-side ``datetime.now(...)``
        # broke because ``conversations.updated_at`` is declared
        # ``TIMESTAMP WITHOUT TIME ZONE``; asyncpg refused the
        # tz-aware value with "can't subtract offset-naive and
        # offset-aware datetimes". Restoring ``func.now()`` is both
        # correct AND avoids the type mismatch.
        conv = await self.get_conversation(conversation_id)
        if conv is not None:
            conv.updated_at = func.now()  # type: ignore[assignment]
        return msg

    # ── Research state (per-conversation bundles) ─────────────────────

    async def save_research_state(
        self,
        *,
        conversation_id: uuid.UUID,
        bundle: dict[str, Any],
        last_question_embedding: list[float] | None,
    ) -> None:
        """Upsert the per-conversation research bundle.

        Called by the coordinator at the end of each successful
        research turn so a follow-up turn — even after API restart or
        on a sibling replica — can reuse the prior subgraph rather
        than re-researching from scratch.
        """
        stmt = (
            pg_insert(ConversationResearchState)
            .values(
                conversation_id=conversation_id,
                bundle=bundle,
                last_question_embedding=last_question_embedding,
            )
            .on_conflict_do_update(
                index_elements=["conversation_id"],
                set_={
                    "bundle": bundle,
                    "last_question_embedding": last_question_embedding,
                    "updated_at": func.now(),
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def load_research_state(
        self,
        conversation_id: uuid.UUID,
    ) -> ConversationResearchState | None:
        result = await self._session.execute(
            select(ConversationResearchState).where(
                ConversationResearchState.conversation_id == conversation_id,
            ),
        )
        return result.scalar_one_or_none()

    async def delete_research_state(
        self,
        conversation_id: uuid.UUID,
    ) -> None:
        """Drop the bundle for a conversation (e.g. on archive / reset)."""
        from sqlalchemy import delete as _delete

        await self._session.execute(
            _delete(ConversationResearchState).where(
                ConversationResearchState.conversation_id == conversation_id,
            ),
        )

    # ── Collaboration sessions ──────────────────────────────────

    async def create_collab_session(
        self,
        *,
        requester: str,
        requirement_text: str,
    ) -> CollaborationSession:
        sess = CollaborationSession(
            requester=requester,
            requirement_text=requirement_text,
            state="open",
            matched_doc_ids=[],
        )
        self._session.add(sess)
        await self._session.flush()
        return sess

    async def get_collab_session(
        self,
        session_id: uuid.UUID,
    ) -> CollaborationSession | None:
        return await self._session.get(CollaborationSession, session_id)

    async def list_collab_sessions(
        self,
        *,
        requester: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> Sequence[CollaborationSession]:
        stmt = (
            select(CollaborationSession)
            .order_by(CollaborationSession.created_at.desc())
            .limit(limit)
        )
        if requester:
            stmt = stmt.where(CollaborationSession.requester == requester)
        if state:
            stmt = stmt.where(CollaborationSession.state == state)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def update_collab_session_state(
        self,
        session_id: uuid.UUID,
        new_state: str,
        *,
        resolved: bool = False,
    ) -> CollaborationSession | None:
        sess = await self.get_collab_session(session_id)
        if sess is None:
            return None
        sess.state = new_state
        if resolved and sess.resolved_at is None:
            from datetime import datetime

            sess.resolved_at = datetime.now(UTC)
        await self._session.flush()
        # ``updated_at`` is server-computed via ``onupdate=func.now()``;
        # without an explicit refresh, the ORM holds the pre-flush
        # value and any subsequent attribute read triggers a sync
        # lazy-load that errors out under asyncpg ("MissingGreenlet").
        await self._session.refresh(sess)
        return sess

    async def append_collab_message(
        self,
        *,
        session_id: uuid.UUID,
        role: str,
        content_md: str,
        source_citations: list[str] | None = None,
        doubt_id: uuid.UUID | None = None,
    ) -> CollabMessage:
        # Compute next ord by querying the current max — cheap given the
        # session-message index. Atomic enough for V1; if we ever see
        # concurrent writes per session we'll switch to a DB sequence.
        next_ord_stmt = select(
            func.coalesce(func.max(CollabMessage.ord), -1) + 1,
        ).where(CollabMessage.session_id == session_id)
        next_ord = (await self._session.execute(next_ord_stmt)).scalar_one()
        msg = CollabMessage(
            session_id=session_id,
            ord=next_ord,
            role=role,
            content_md=_scrub_secrets(content_md, source="collab_message"),
            source_citations=source_citations or [],
            doubt_id=doubt_id,
        )
        self._session.add(msg)
        await self._session.flush()
        return msg

    async def list_collab_messages(
        self,
        session_id: uuid.UUID,
    ) -> Sequence[CollabMessage]:
        stmt = (
            select(CollabMessage)
            .where(CollabMessage.session_id == session_id)
            .order_by(CollabMessage.ord)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def add_collab_doubt(
        self,
        *,
        session_id: uuid.UUID,
        question: str,
    ) -> CollabDoubt:
        doubt = CollabDoubt(
            session_id=session_id,
            question=question,
            status="open",
        )
        self._session.add(doubt)
        await self._session.flush()
        return doubt

    async def list_collab_doubts(
        self,
        session_id: uuid.UUID,
    ) -> Sequence[CollabDoubt]:
        stmt = (
            select(CollabDoubt)
            .where(CollabDoubt.session_id == session_id)
            .order_by(CollabDoubt.created_at)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_collab_doubt(
        self,
        doubt_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> CollabDoubt | None:
        return await self._session.get(
            CollabDoubt,
            doubt_id,
            with_for_update=for_update or None,
            populate_existing=for_update,
        )

    async def resolve_collab_doubt(
        self,
        doubt_id: uuid.UUID,
        *,
        status: str,
        resolved_md: str | None = None,
        source_ref: str | None = None,
    ) -> CollabDoubt | None:
        doubt = await self._session.get(CollabDoubt, doubt_id)
        if doubt is None:
            return None
        doubt.status = status
        doubt.resolved_md = resolved_md
        doubt.source_ref = source_ref
        from datetime import datetime

        doubt.resolved_at = datetime.now(UTC)
        await self._session.flush()
        return doubt

    async def add_collab_artifact(
        self,
        *,
        session_id: uuid.UUID,
        kind: str,
        fs_path: str,
    ) -> CollabArtifact:
        artifact = CollabArtifact(
            session_id=session_id,
            kind=kind,
            fs_path=fs_path,
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact

    async def list_collab_artifacts(
        self,
        session_id: uuid.UUID,
    ) -> Sequence[CollabArtifact]:
        stmt = (
            select(CollabArtifact)
            .where(CollabArtifact.session_id == session_id)
            .order_by(CollabArtifact.created_at)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    # ── Handover docs (minimal, session-scoped only) ───────────────────────

    async def create_handover_doc(
        self,
        *,
        scope_kind: str,
        scope_value: str,
        parent_id: uuid.UUID | None = None,
        fs_path: str | None = None,
    ) -> HandoverDoc:
        doc = HandoverDoc(
            scope_kind=scope_kind,
            scope_value=scope_value,
            parent_id=parent_id,
            fs_path=fs_path,
        )
        self._session.add(doc)
        await self._session.flush()
        return doc

    async def get_handover_doc(
        self,
        doc_id: uuid.UUID,
    ) -> HandoverDoc | None:
        return await self._session.get(HandoverDoc, doc_id)

    async def search_handover_docs(
        self,
        *,
        query: str,
        limit: int = 20,
    ) -> Sequence[tuple[HandoverDoc, float]]:
        """Full-text search live (non-archived, non-session) handover
        docs against ``scope_value || scope_kind``.

        Backed by the ``search_tsv`` generated column +
        ``idx_handover_docs_search_tsv`` GIN index from the baseline migration.
        Orders by ``ts_rank`` desc, then ``last_generated_at`` desc so
        ties between equally-relevant docs surface the freshest one
        first.

        ``query`` is sanitised before it reaches Postgres — we drop
        characters that would otherwise blow up ``to_tsquery`` (which
        requires a strict boolean syntax) and AND-combine the
        remaining tokens. An empty effective query returns an empty
        list (callers should validate
        non-empty input upstream and surface a 400). Body-text search
        intentionally lives in the route — bodies are on disk under
        ``fs_path``, not in the row, and the route already needs to
        read them to build the snippet.

        Returns ``[(doc, rank), ...]``; the caller turns rank into the
        ``score`` field on ``HandoverSearchHit``.
        """
        tsquery = _build_tsquery(query)
        if not tsquery:
            return []

        # ``websearch_to_tsquery`` would be friendlier to user input, but
        # ``to_tsquery`` keeps semantics predictable for this internal
        # tool: every token is an AND'd lexeme. We've already sanitised
        # the input so the query is always well-formed.
        ts_query = func.to_tsquery("english", tsquery)
        rank = func.ts_rank(HandoverDoc.search_tsv, ts_query).label("rank")

        stmt = (
            select(HandoverDoc, rank)
            .where(HandoverDoc.search_tsv.op("@@")(ts_query))
            .where(HandoverDoc.archived_at.is_(None))
            .where(HandoverDoc.scope_kind != "session")
            .order_by(
                rank.desc(),
                HandoverDoc.last_generated_at.desc().nulls_last(),
                HandoverDoc.created_at.desc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(row[0], float(row[1] or 0.0)) for row in result.all()]

    async def list_handover_docs(
        self,
        *,
        scope_kind: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> Sequence[HandoverDoc]:
        """List handover docs, ordered by recency.

        archived rows (``archived_at IS NOT NULL``) are
        filtered out by default. Pass ``include_archived=True`` to
        include them — typical use is the UI's "Show archived" toggle
        which lets a user restore a previously-archived doc.
        """
        stmt = (
            select(HandoverDoc)
            .order_by(
                HandoverDoc.last_generated_at.desc().nulls_last(),
                HandoverDoc.created_at.desc(),
            )
            .limit(limit)
        )
        if scope_kind:
            stmt = stmt.where(HandoverDoc.scope_kind == scope_kind)
        if not include_archived:
            stmt = stmt.where(HandoverDoc.archived_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def archive_handover_doc(self, doc_id: uuid.UUID) -> bool:
        """Soft-archive a handover doc by stamping ``archived_at`` to now.

        Idempotent: re-archiving an already-archived doc bumps the
        timestamp (cheap, makes for a clean ``last archived`` audit
        line) and still returns True. Returns False only when the
        doc id doesn't exist. Caller commits.
        """
        from datetime import datetime

        now = datetime.now(UTC)
        stmt = update(HandoverDoc).where(HandoverDoc.id == doc_id).values(archived_at=now)
        result = await self._session.execute(stmt)
        return affected_rows(result) > 0

    async def restore_handover_doc(self, doc_id: uuid.UUID) -> bool:
        """Clear ``archived_at`` on a previously-archived handover doc.

        Idempotent: restoring an already-live doc is a no-op that
        still returns True (the row exists). Returns False only when
        the doc id doesn't exist. Caller commits.
        """
        stmt = update(HandoverDoc).where(HandoverDoc.id == doc_id).values(archived_at=None)
        result = await self._session.execute(stmt)
        return affected_rows(result) > 0

    async def record_handover_audit(
        self,
        *,
        doc_id: uuid.UUID,
        event_type: str,
        actor: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one row to ``handover_audits``. Best-effort.

        every mutating handover event (generate / archive /
        restore / edit / promote) calls this. The insert is best-effort:
        any failure is logged and swallowed so a misbehaving audit
        layer never breaks user-facing flows. The route caller still
        commits its own session — we just stage the INSERT here so it
        rides the same transaction as the underlying mutation
        (rollback together if the parent operation later fails).
        """
        try:
            # ``pg_insert`` takes DB column keys, not ORM attribute
            # keys, so the JSONB column is referenced as ``metadata``
            # (its DB name) even though the ORM attribute is
            # ``audit_metadata`` (kept distinct from the reserved
            # ``Base.metadata`` name). We target the raw ``__table__``
            # so SQLAlchemy doesn't try to resolve ``metadata`` against
            # the ORM class — that lookup hits the declarative
            # ``MetaData`` registry and fails.
            row_values = _orm_to_column_values(
                HandoverAudit,
                {
                    "doc_id": doc_id,
                    "event_type": event_type,
                    "actor": actor,
                    "audit_metadata": dict(metadata or {}),
                },
            )
            stmt = pg_insert(HandoverAudit.__table__).values(**row_values)
            await self._session.execute(stmt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "record_handover_audit failed (doc_id=%s event=%s) — "
                "swallowing to keep the parent op succeeding",
                doc_id,
                event_type,
            )

    async def list_handover_audits(
        self,
        doc_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> Sequence[HandoverAudit]:
        """Return up to ``limit`` audit rows for ``doc_id``, newest first.

        Drives ``GET /handover-docs/{doc_id}/audit``. Index-only:
        ``idx_handover_audits_doc_created`` is keyed on
        ``(doc_id, created_at DESC)`` so the planner can satisfy this
        in a single index range scan.
        """
        stmt = (
            select(HandoverAudit)
            .where(HandoverAudit.doc_id == doc_id)
            .order_by(HandoverAudit.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    # ── body-history snapshots ────────────────────

    # How many snapshots we keep per doc. Bounds storage so a single
    # over-edited handover can't accumulate unboundedly. 20 is enough
    # to cover a normal back-and-forth edit cycle while still being
    # a hard ceiling.
    _HANDOVER_BODY_HISTORY_RETENTION = 20

    async def record_handover_body_snapshot(
        self,
        *,
        doc_id: uuid.UUID,
        body_md: str,
        event_type: str,
    ) -> None:
        """Snapshot a handover body before it gets overwritten. Best-effort.

        every site that overwrites the on-disk Markdown
        (regen / edit / promote) calls this with the *old* body text
        so the UI diff viewer can show what changed. The insert is
        best-effort: any failure is logged and swallowed so a
        misbehaving snapshot layer never breaks a body update from
        landing.

        After the insert, this also trims older rows for the same
        ``doc_id`` past ``_HANDOVER_BODY_HISTORY_RETENTION`` so
        storage stays bounded. The trim runs in the same transaction
        as the insert + parent op — if the parent rolls back, both
        the new snapshot AND the trim revert together, leaving the
        prior history intact.
        """
        try:
            stmt = pg_insert(HandoverBodyHistory.__table__).values(
                doc_id=doc_id,
                body_md=body_md,
                event_type=event_type,
            )
            await self._session.execute(stmt)

            # Trim to the most-recent N rows. We can't ORDER BY +
            # LIMIT inside a DELETE directly on PG without a
            # subquery, so we pick the IDs to keep first and delete
            # the rest. This is one extra round-trip per snapshot
            # but keeps the SQL portable to plain-SQLAlchemy.
            keep_ids_stmt = (
                select(HandoverBodyHistory.id)
                .where(HandoverBodyHistory.doc_id == doc_id)
                .order_by(HandoverBodyHistory.created_at.desc())
                .limit(self._HANDOVER_BODY_HISTORY_RETENTION)
            )
            keep_result = await self._session.execute(keep_ids_stmt)
            keep_ids = [row[0] for row in keep_result.all()]
            if keep_ids:
                trim_stmt = (
                    delete(HandoverBodyHistory)
                    .where(HandoverBodyHistory.doc_id == doc_id)
                    .where(HandoverBodyHistory.id.notin_(keep_ids))
                )
                await self._session.execute(trim_stmt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "record_handover_body_snapshot failed "
                "(doc_id=%s event=%s) — swallowing to keep the "
                "parent op succeeding",
                doc_id,
                event_type,
            )

    async def list_handover_body_history(
        self,
        doc_id: uuid.UUID,
        *,
        limit: int = 20,
    ) -> Sequence[HandoverBodyHistory]:
        """Return up to ``limit`` body snapshots for ``doc_id``, newest first.

        Drives ``GET /handover-docs/{doc_id}/history``. Index-only:
        ``idx_handover_body_history_doc_created`` is keyed on
        ``(doc_id, created_at DESC)`` so the planner can satisfy this
        in a single index range scan.
        """
        stmt = (
            select(HandoverBodyHistory)
            .where(HandoverBodyHistory.doc_id == doc_id)
            .order_by(HandoverBodyHistory.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    # ── Section feedback (team-collab gap closure) ─────────────────

    async def add_section_feedback(
        self,
        *,
        doc_id: uuid.UUID,
        section_key: str,
        kind: str,
        note: str | None,
        actor: str | None,
    ) -> HandoverSectionFeedback:
        """Insert one section-feedback row for a handover doc.

        Mirrors the pattern of :meth:`add_collab_doubt` — caller commits.
        Length / enum validation lives at the route layer (Pydantic);
        the DB CHECK on ``kind`` provides defense-in-depth.
        """
        feedback = HandoverSectionFeedback(
            doc_id=doc_id,
            section_key=section_key,
            kind=kind,
            note=note,
            actor=actor,
        )
        self._session.add(feedback)
        await self._session.flush()
        return feedback

    async def get_section_feedback(
        self,
        feedback_id: uuid.UUID,
    ) -> HandoverSectionFeedback | None:
        """Look up one feedback row by id."""
        return await self._session.get(HandoverSectionFeedback, feedback_id)

    async def list_section_feedback(
        self,
        doc_id: uuid.UUID,
        *,
        include_resolved: bool = False,
    ) -> Sequence[HandoverSectionFeedback]:
        """Return feedback rows for ``doc_id``, newest first.

        ``include_resolved=False`` (the default) hides rows where
        ``resolved_at IS NOT NULL`` so the doc owner sees only the open
        backlog. Pass ``include_resolved=True`` to fetch the full
        history (open + resolved). Index plan:

          * Open-only path uses ``idx_handover_feedback_open`` (partial
            on ``resolved_at IS NULL``).
          * Full-history path uses ``idx_handover_feedback_doc``
            (composite on ``doc_id, created_at DESC``).
        """
        stmt = (
            select(HandoverSectionFeedback)
            .where(HandoverSectionFeedback.doc_id == doc_id)
            .order_by(HandoverSectionFeedback.created_at.desc())
        )
        if not include_resolved:
            stmt = stmt.where(HandoverSectionFeedback.resolved_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def resolve_section_feedback(
        self,
        feedback_id: uuid.UUID,
        *,
        resolved_by: str | None,
    ) -> HandoverSectionFeedback | None:
        """Mark one feedback row resolved.

        Idempotent — re-resolving an already-resolved row bumps the
        ``resolved_at`` timestamp and rewrites ``resolved_by`` (cheap,
        keeps the "last action" honest). Returns ``None`` only when
        the feedback id doesn't exist; caller commits.
        """
        from datetime import datetime

        feedback = await self._session.get(
            HandoverSectionFeedback,
            feedback_id,
        )
        if feedback is None:
            return None
        feedback.resolved_at = datetime.now(UTC)
        feedback.resolved_by = resolved_by
        await self._session.flush()
        return feedback

    async def count_open_section_feedback(
        self,
        doc_id: uuid.UUID,
    ) -> int:
        """Return the number of open feedback rows on a doc.

        Cheap aggregate hit by the inbox endpoint — uses
        ``idx_handover_feedback_open`` (partial index on rows where
        ``resolved_at IS NULL``) so the planner can satisfy this from
        the index alone.
        """
        stmt = (
            select(func.count())
            .select_from(HandoverSectionFeedback)
            .where(HandoverSectionFeedback.doc_id == doc_id)
            .where(HandoverSectionFeedback.resolved_at.is_(None))
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one() or 0)

    async def section_metadata(
        self,
        doc_id: uuid.UUID,
    ) -> dict[str, dict[str, Any]]:
        """Per-section metadata for a handover doc.

        Returns ``{section_key: {last_regenerated_at, feedback_open_count,
        last_feedback_at}}`` aggregated across the audit log + feedback
        table.

        Section keys are sourced from:
          * Any audit row that records a section-level regen — today
            the generator writes ``event_type='edited'`` with
            ``metadata.kind='section-regen'`` and ``metadata.section``
            holding the section key (reuses the existing enum value
            rather than minting a new one). For
            forward compat we also accept a literal
            ``event_type='section_regenerated'`` with
            ``metadata.section_key`` so a future audit-vocab cleanup
            doesn't break this aggregator.
          * Any ``handover_section_feedback`` row's ``section_key``.

        Sections with no signals at all aren't included — a UI that
        wants to display every canonical section iterates the static
        section list and reads ``out.get(key, {default…})`` per slot.

        Implementation is a Python-side merge — both source sets are
        small (~10 sections × tens of events) so we avoid the
        gymnastics of a JSONB-keyed GROUP BY.
        """
        # 1. Pull all audit rows for this doc that carry a section_key
        # signal in metadata. We accept two shapes (see docstring): a
        # literal ``section_regenerated`` event, or the current
        # generator's ``edited`` + ``kind=section-regen`` convention.
        audit_stmt = (
            select(HandoverAudit)
            .where(HandoverAudit.doc_id == doc_id)
            .where(
                or_(
                    HandoverAudit.event_type == "section_regenerated",
                    and_(
                        HandoverAudit.event_type == "edited",
                        HandoverAudit.audit_metadata["kind"].astext == "section-regen",
                    ),
                ),
            )
            .order_by(HandoverAudit.created_at.desc())
        )
        audit_rows = (await self._session.execute(audit_stmt)).scalars().all()

        # 2. Pull all feedback rows for this doc (open + resolved).
        fb_stmt = (
            select(HandoverSectionFeedback)
            .where(HandoverSectionFeedback.doc_id == doc_id)
            .order_by(HandoverSectionFeedback.created_at.desc())
        )
        fb_rows = (await self._session.execute(fb_stmt)).scalars().all()

        # 3. Merge. ``last_regenerated_at`` = newest audit row whose
        # metadata identifies a section; ``feedback_open_count`` =
        # count of feedback rows where resolved_at IS NULL;
        # ``last_feedback_at`` = newest feedback row's created_at.
        out: dict[str, dict[str, Any]] = {}

        def _slot(section_key: str) -> dict[str, Any]:
            return out.setdefault(
                section_key,
                {
                    "last_regenerated_at": None,
                    "feedback_open_count": 0,
                    "last_feedback_at": None,
                },
            )

        for row in audit_rows:
            md = row.audit_metadata or {}
            # Two key conventions: ``section_key`` (preferred, future)
            # and ``section`` (current generator).
            sk = md.get("section_key") or md.get("section")
            if not sk:
                continue
            slot = _slot(sk)
            # Audit rows are ordered desc — first one wins.
            if slot["last_regenerated_at"] is None:
                slot["last_regenerated_at"] = row.created_at

        for fb in fb_rows:
            slot = _slot(fb.section_key)
            # Feedback rows are also ordered desc — first one wins for
            # last_feedback_at.
            if slot["last_feedback_at"] is None:
                slot["last_feedback_at"] = fb.created_at
            if fb.resolved_at is None:
                slot["feedback_open_count"] += 1

        return out

    # ── Cross-references back to a handover (team-collab gap closure) ──

    async def list_sessions_referencing_doc(
        self,
        doc_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> Sequence[CollaborationSession]:
        """Collab sessions whose ``matched_doc_ids`` JSONB array
        contains ``doc_id``.

        Drives ``GET /handover-docs/{doc_id}/referencing-sessions`` —
        a doc owner's view of "who's been asking about my doc". Order
        is ``created_at DESC``; capped at ``limit``.

        We use the JSONB ``@>`` containment operator with the doc id
        rendered as a one-element string array — ``matched_doc_ids``
        stores UUIDs as JSON strings, so the literal must match that
        encoding exactly.
        """
        # Render the id as a JSONB string-array literal: the column
        # holds ``["<uuid-str>", ...]`` so containment tests against
        # ``["<uuid-str>"]``. Casting the Python list to ``JSONB`` here
        # keeps the binding explicit and asyncpg-friendly.
        from sqlalchemy import literal
        from sqlalchemy.dialects.postgresql import JSONB

        target = literal([str(doc_id)], type_=JSONB)
        stmt = (
            select(CollaborationSession)
            .where(CollaborationSession.matched_doc_ids.op("@>")(target))
            .order_by(CollaborationSession.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_sessions_referencing_doc(
        self,
        doc_id: uuid.UUID,
    ) -> int:
        """Count of distinct collab sessions whose ``matched_doc_ids``
        contains ``doc_id``. Used by the inbox aggregate.
        """
        from sqlalchemy import literal
        from sqlalchemy.dialects.postgresql import JSONB

        target = literal([str(doc_id)], type_=JSONB)
        stmt = (
            select(func.count())
            .select_from(CollaborationSession)
            .where(CollaborationSession.matched_doc_ids.op("@>")(target))
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one() or 0)

    async def list_handover_inbox(
        self,
        *,
        actor: str,
        limit: int = 5,
    ) -> Sequence[HandoverDoc]:
        """Return up to ``limit`` handover docs the ``actor`` has
        recently touched, newest-touch-first.

        "Touched" = appears as ``actor`` on at least one row in
        ``handover_audits``. We rank by the most-recent audit
        timestamp per doc and surface the top N. Archived docs are
        excluded (the inbox is about live work).

        Returns the ORM rows; the route layer enriches each with
        per-doc aggregates (open feedback count, referencing session
        count, doubt candidate count) before serialising.
        """
        # Subquery: pick the latest audit-log timestamp per doc id for
        # this actor. Join back to the doc table, filter out archived.
        latest_audit = (
            select(
                HandoverAudit.doc_id.label("doc_id"),
                func.max(HandoverAudit.created_at).label("last_touched_at"),
            )
            .where(HandoverAudit.actor == actor)
            .group_by(HandoverAudit.doc_id)
            .subquery()
        )

        stmt = (
            select(HandoverDoc, latest_audit.c.last_touched_at)
            .join(latest_audit, latest_audit.c.doc_id == HandoverDoc.id)
            .where(HandoverDoc.archived_at.is_(None))
            .order_by(latest_audit.c.last_touched_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        # Stash the joined timestamp on the row via Python attribute so
        # the route layer doesn't need a second round-trip. SQLAlchemy
        # returns ``Row`` here so we expose both halves directly.
        rows: list[HandoverDoc] = []
        for doc, last_touched_at in result.all():
            doc.__bifrost_last_touched_at__ = last_touched_at  # type: ignore[attr-defined]
            rows.append(doc)
        return rows

    async def mark_handover_docs_stale_by_repo_name(
        self,
        repo_name: str,
    ) -> int:
        """Flag ``fresh`` handover docs that cover ``repo_name`` as ``stale``.

        Called by the extraction worker when a repo extraction completes
        successfully — the underlying entity graph has shifted, so any cached
        handover narrative covering this repo is no longer guaranteed
        accurate. The UI surfaces this with a banner + Refresh button.

        Also flags the multi-repo handovers this repo participates in:

          * ``scope_kind='repo'``      where ``scope_value == repo_name``
          * ``scope_kind='domain'``    where ``scope_value`` matches the
            ``Domain.name`` resolved from ``repos.domain_id``
          * ``scope_kind='team'`` where ``scope_value`` matches any
            ``OrgRepo.squad`` joined to the repo via
            ``OrgRepo.registered_repo_id``. A repo can appear in multiple
            ``org_repos`` rows (different squads); ALL matching team
            handovers are flagged.

        Only flips ``fresh`` → ``stale``. Leaves ``very_stale`` alone
        (already worse than stale) and the children-derived states
        untouched. The caller commits its session.

        Edge cases:
          * Unknown repo (no ``Repo`` row): only the repo-scope predicate
            runs — domain/squad lookups are skipped. Behavior matches the
            repo-only path so legacy callers are unaffected.
          * Repo without a domain: domain predicate is skipped, no error.
          * Repo without any ``org_repos`` rows (or all squads NULL):
            team predicate is skipped, no error.

        Returns the total number of rows updated across all three scopes.
        """
        # Resolve the repo's domain name and the set of squads it belongs
        # to. Both lookups are cheap (indexed on repo name / squad) and
        # we issue them as one round-trip via SELECT joins.
        domain_stmt = (
            select(Domain.name)
            .join(Repo, Repo.domain_id == Domain.id)
            .where(Repo.name == repo_name)
            .limit(1)
        )
        squads_stmt = (
            select(OrgRepo.squad)
            .join(Repo, Repo.id == OrgRepo.registered_repo_id)
            .where(
                Repo.name == repo_name,
                OrgRepo.squad.isnot(None),
            )
            .distinct()
        )
        domain_name = (await self._session.execute(domain_stmt)).scalar_one_or_none()
        squads_result = await self._session.execute(squads_stmt)
        squads: list[str] = [s for s in squads_result.scalars().all() if s]

        # Build the OR'd scope predicate. The repo-scope clause always runs
        # (preserves repo-only behavior even when the Repo row doesn't exist).
        scope_clauses = [
            and_(
                HandoverDoc.scope_kind == "repo",
                HandoverDoc.scope_value == repo_name,
            ),
        ]
        if domain_name:
            scope_clauses.append(
                and_(
                    HandoverDoc.scope_kind == "domain",
                    HandoverDoc.scope_value == domain_name,
                ),
            )
        if squads:
            scope_clauses.append(
                and_(
                    HandoverDoc.scope_kind == "team",
                    HandoverDoc.scope_value.in_(squads),
                ),
            )

        stmt = (
            update(HandoverDoc)
            .where(
                HandoverDoc.freshness_state == "fresh",
                or_(*scope_clauses),
            )
            .values(freshness_state="stale")
        )
        result = await self._session.execute(stmt)
        # rowcount is reliable for UPDATE in SQLAlchemy core; cast to int for
        # narrow typing (it can be ``-1`` on some backends, which we coerce to
        # 0 since "no rows reported" ≈ "nothing flagged").
        flagged = max(affected_rows(result), 0)

        logger.info(
            "auto-flag-stale repo=%s domain=%s squad=%s flagged=%d",
            repo_name,
            domain_name or "-",
            ",".join(squads) if squads else "-",
            flagged,
        )
        return flagged

    async def mark_parent_handovers_enriched(
        self,
        repo_name: str,
    ) -> int:
        """Flag ``fresh`` parent (domain/team/region) handovers covering
        ``repo_name`` as ``enriched_by_children``.

        Downward → upward propagation: when a CHILD repo
        handover regenerates, the parent multi-repo docs (domain, team,
        region) that roll it up have new child material available. We
        flag them ``enriched_by_children`` so the UI can hint "your
        parent handover has new child content; consider regenerating"
        without forcing a full regeneration.

        Scope resolution mirrors :meth:`mark_handover_docs_stale_by_repo_name`:

          * ``scope_kind='domain'``    via ``Domain.name`` from
            ``repos.domain_id``
          * ``scope_kind='team'`` via ``OrgRepo.squad`` joined to
            the repo through ``OrgRepo.registered_repo_id``
          * ``scope_kind='region'``    via ``OrgRepo.region`` joined to
            the repo through ``OrgRepo.registered_repo_id``

        Only flips ``fresh`` → ``enriched_by_children``. Leaves all
        other states alone — in particular **stale wins**: a parent
        already marked ``stale`` (because a different child repo
        re-extracted) stays ``stale`` rather than being
        downgraded to a softer "enriched" hint.

        Edge cases:
          * Unknown repo (no ``Repo`` row): no parent scopes resolve,
            no rows updated, returns 0 — silent no-op.
          * Repo with no ``domain_id``: domain predicate is skipped.
          * Repo with no ``org_repos`` rows (or all squads/regions
            NULL): respective predicate is skipped.
          * No parent handovers exist for any matching scope: returns 0.

        Returns the total number of rows updated. Caller commits.
        """
        domain_stmt = (
            select(Domain.name)
            .join(Repo, Repo.domain_id == Domain.id)
            .where(Repo.name == repo_name)
            .limit(1)
        )
        squads_stmt = (
            select(OrgRepo.squad)
            .join(Repo, Repo.id == OrgRepo.registered_repo_id)
            .where(
                Repo.name == repo_name,
                OrgRepo.squad.isnot(None),
            )
            .distinct()
        )
        regions_stmt = (
            select(OrgRepo.region)
            .join(Repo, Repo.id == OrgRepo.registered_repo_id)
            .where(
                Repo.name == repo_name,
                OrgRepo.region.isnot(None),
            )
            .distinct()
        )
        domain_name = (await self._session.execute(domain_stmt)).scalar_one_or_none()
        squads_result = await self._session.execute(squads_stmt)
        squads: list[str] = [s for s in squads_result.scalars().all() if s]
        regions_result = await self._session.execute(regions_stmt)
        regions: list[str] = [r for r in regions_result.scalars().all() if r]

        # Only multi-repo (parent) scopes — repo-scope is the child here.
        scope_clauses: list[Any] = []
        if domain_name:
            scope_clauses.append(
                and_(
                    HandoverDoc.scope_kind == "domain",
                    HandoverDoc.scope_value == domain_name,
                ),
            )
        if squads:
            scope_clauses.append(
                and_(
                    HandoverDoc.scope_kind == "team",
                    HandoverDoc.scope_value.in_(squads),
                ),
            )
        if regions:
            scope_clauses.append(
                and_(
                    HandoverDoc.scope_kind == "region",
                    HandoverDoc.scope_value.in_(regions),
                ),
            )
        if not scope_clauses:
            # No parent scopes apply (orphan repo, or repo with no
            # squad/region/domain) — nothing to enrich.
            logger.info(
                "auto-flag-enriched repo=%s domain=- squad=- region=- flagged=0",
                repo_name,
            )
            return 0

        stmt = (
            update(HandoverDoc)
            .where(
                HandoverDoc.freshness_state == "fresh",
                or_(*scope_clauses),
            )
            .values(freshness_state="enriched_by_children")
        )
        result = await self._session.execute(stmt)
        flagged = max(affected_rows(result), 0)

        logger.info(
            "auto-flag-enriched repo=%s domain=%s squad=%s region=%s flagged=%d",
            repo_name,
            domain_name or "-",
            ",".join(squads) if squads else "-",
            ",".join(regions) if regions else "-",
            flagged,
        )
        return flagged

    async def mark_children_validated_by_parent(
        self,
        parent_doc_id: uuid.UUID,
    ) -> int:
        """Bump ``last_validated_at`` on child repo handovers that were
        rolled up into ``parent_doc_id``.

        Downward propagation: when a parent (domain/
        team/region) handover regenerates via tree decomposition, every
        child repo handover under it was just re-validated as part of
        the parent rollup. We bump their ``last_validated_at`` to now
        so the "last touched" stamp the UI shows on the child reflects
        the rollup activity.

        Children are resolved by **scope membership** (not by
        ``parent_id``, which doesn't physically link them). Given the
        parent's scope_kind:

          * ``domain``     → repos with ``Repo.domain_id == Domain.id``
            for ``Domain.name == scope_value``
          * ``team`` → repos joined through ``OrgRepo.squad``
            == scope_value
          * ``region``     → repos joined through ``OrgRepo.region``
            == scope_value

        Then update repo-scope handover docs whose ``scope_value`` is
        in that repo-name set. Does NOT touch ``freshness_state`` —
        children remain whatever they were (``fresh`` stays fresh,
        ``stale`` stays stale). Only the timestamp moves forward.

        Edge cases:
          * Parent doc doesn't exist: returns 0.
          * Parent is repo-scoped (no children semantically): returns 0.
          * Parent has zero member repos (empty domain/squad/region):
            returns 0.
          * No child repo handover docs exist for the resolved set:
            returns 0.

        Returns the number of child rows whose timestamp was bumped.
        Caller commits.
        """
        from datetime import datetime

        parent = await self._session.get(HandoverDoc, parent_doc_id)
        if parent is None:
            logger.info(
                "validate-children parent_id=%s flagged=0 reason=parent_missing",
                parent_doc_id,
            )
            return 0

        scope_kind = parent.scope_kind
        scope_value = parent.scope_value

        if scope_kind == "domain":
            child_repo_stmt = (
                select(Repo.name)
                .join(Domain, Domain.id == Repo.domain_id)
                .where(Domain.name == scope_value)
            )
        elif scope_kind == "team":
            child_repo_stmt = (
                select(Repo.name)
                .join(OrgRepo, OrgRepo.registered_repo_id == Repo.id)
                .where(OrgRepo.squad == scope_value)
                .distinct()
            )
        elif scope_kind == "region":
            child_repo_stmt = (
                select(Repo.name)
                .join(OrgRepo, OrgRepo.registered_repo_id == Repo.id)
                .where(OrgRepo.region == scope_value)
                .distinct()
            )
        else:
            # Repo-scope or session-scope parents have no children to
            # roll up — silent no-op.
            logger.info(
                "validate-children parent_id=%s scope=%s flagged=0 reason=non_parent_scope",
                parent_doc_id,
                scope_kind,
            )
            return 0

        rows = await self._session.execute(child_repo_stmt)
        child_repo_names: list[str] = [n for n in rows.scalars().all() if n]
        if not child_repo_names:
            logger.info(
                "validate-children parent_id=%s scope=%s/%s flagged=0 reason=empty_scope",
                parent_doc_id,
                scope_kind,
                scope_value,
            )
            return 0

        now = datetime.now(UTC)
        stmt = (
            update(HandoverDoc)
            .where(
                HandoverDoc.scope_kind == "repo",
                HandoverDoc.scope_value.in_(child_repo_names),
                HandoverDoc.parent_id.is_(None),
            )
            .values(last_validated_at=now)
        )
        result = await self._session.execute(stmt)
        flagged = max(affected_rows(result), 0)

        logger.info(
            "validate-children parent_id=%s scope=%s/%s repos=%d flagged=%d",
            parent_doc_id,
            scope_kind,
            scope_value,
            len(child_repo_names),
            flagged,
        )
        return flagged
