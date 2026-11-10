"""SQLAlchemy ORM models — 12 tables per TRD §3.

All tables in a single PostgreSQL 16+ database with pgvector extension.
Uses SQLAlchemy 2.0 Mapped[] style with asyncpg driver.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    DateTime as sqlalchemy_DateTime,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bifrost.core.crypto import SecretJSON

# Naming convention for consistent constraint/index names across Alembic migrations
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)


# ── 1. repos ──────────────────────────────────────────────────────────────────


class Repo(Base):
    """Registered source repositories."""

    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    urn: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    local_clone_path: Mapped[str | None] = mapped_column(Text)
    adapter_config: Mapped[dict[str, Any]] = mapped_column(
        SecretJSON, nullable=False, server_default="{}"
    )
    # Per-team designation. When a domain has multiple repos, the
    # ``is_primary`` one is the canonical answer for chat queries
    # against that team — set via the manifest's ``is_primary`` flag.
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    domain: Mapped[Domain | None] = relationship(back_populates="repos")
    entities: Mapped[list[Entity]] = relationship(back_populates="scope_repo")
    knowledge_texts: Mapped[list[KnowledgeTextCache]] = relationship(back_populates="repo")
    freshness: Mapped[RepoFreshness | None] = relationship(back_populates="repo", uselist=False)

    __table_args__ = (
        Index("idx_repos_urn", "urn", unique=True),
        Index("idx_repos_domain", "domain_id", postgresql_where=domain_id.isnot(None)),
    )


# ── 2. domains ────────────────────────────────────────────────────────────────


class Domain(Base):
    """Domain groupings. Repos belong to at most one domain."""

    __tablename__ = "domains"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    urn: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    repos: Mapped[list[Repo]] = relationship(back_populates="domain")
    entities: Mapped[list[Entity]] = relationship(back_populates="scope_domain")

    __table_args__ = (Index("idx_domains_urn", "urn", unique=True),)


# ── 3. entities ───────────────────────────────────────────────────────────────


class Entity(Base):
    """Polymorphic knowledge graph nodes. All entity types stored here."""

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    urn: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str | None] = mapped_column(Text)
    scope_repo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE")
    )
    scope_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL")
    )
    knowledge_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    entity_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    # 384-dim matches sentence-transformers/all-MiniLM-L6-v2 (local, no API key).
    embedding: Mapped[Any | None] = mapped_column(Vector(384))
    # Louvain-assigned community id (Stage 6). Nullable because extraction
    # runs BEFORE clustering, so entities exist unclustered.
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity_communities.id", ondelete="SET NULL"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.5)
    confidence_level: Mapped[str] = mapped_column(Text, nullable=False, default="uncertain")
    source_basis: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    freshness_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    last_refreshed: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    scope_repo: Mapped[Repo | None] = relationship(back_populates="entities")
    scope_domain: Mapped[Domain | None] = relationship(back_populates="entities")
    outgoing_relationships: Mapped[list[EntityRelationship]] = relationship(
        foreign_keys="EntityRelationship.source_id", back_populates="source"
    )
    incoming_relationships: Mapped[list[EntityRelationship]] = relationship(
        foreign_keys="EntityRelationship.target_id", back_populates="target"
    )

    __table_args__ = (
        CheckConstraint("knowledge_tier BETWEEN 0 AND 3", name="valid_knowledge_tier"),
        CheckConstraint("confidence BETWEEN 0.0 AND 1.0", name="valid_confidence"),
        CheckConstraint(
            "confidence_level IN ("
            "'confirmed', 'inferred', 'stale', "
            "'uncertain', 'unknown', 'conflicting')",
            name="valid_confidence_level",
        ),
        CheckConstraint("freshness_state IN ('fresh', 'stale', 'unknown')", name="valid_freshness"),
        Index("idx_entities_type", "entity_type"),
        Index(
            "idx_entities_scope_repo", "scope_repo_id", postgresql_where=scope_repo_id.isnot(None)
        ),
        Index(
            "idx_entities_scope_domain",
            "scope_domain_id",
            postgresql_where=scope_domain_id.isnot(None),
        ),
        Index("idx_entities_qualified_name", "qualified_name"),
        Index("idx_entities_freshness", "freshness_state", "last_refreshed"),
        Index(
            "idx_entities_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("idx_entities_metadata", "metadata", postgresql_using="gin"),
    )


# ── 4. entity_relationships ───────────────────────────────────────────────────


class EntityRelationship(Base):
    """Typed directional edges between entities in the knowledge graph."""

    __tablename__ = "entity_relationships"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    rel_type: Mapped[str] = mapped_column(Text, nullable=False)
    rel_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.5)
    source_basis: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    freshness_state: Mapped[str] = mapped_column(Text, nullable=False, default="fresh")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    source: Mapped[Entity] = relationship(
        foreign_keys=[source_id], back_populates="outgoing_relationships"
    )
    target: Mapped[Entity] = relationship(
        foreign_keys=[target_id], back_populates="incoming_relationships"
    )

    __table_args__ = (
        CheckConstraint("source_id != target_id", name="no_self_reference"),
        CheckConstraint("confidence BETWEEN 0.0 AND 1.0", name="valid_confidence"),
        UniqueConstraint("source_id", "target_id", "rel_type", name="uq_rel_unique_edge"),
        Index("idx_rel_source", "source_id"),
        Index("idx_rel_target", "target_id"),
        Index("idx_rel_type", "rel_type"),
        Index("idx_rel_source_type", "source_id", "rel_type"),
    )


# ── 5. knowledge_text_cache ───────────────────────────────────────────────────


class KnowledgeTextCache(Base):
    """AgentStateBackend Track A storage. Text documents indexed by path."""

    __tablename__ = "knowledge_text_cache"

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    repo: Mapped[Repo | None] = relationship(back_populates="knowledge_texts")

    __table_args__ = (
        Index("idx_ktc_repo", "repo_id"),
        Index("idx_ktc_path_prefix", "path", postgresql_ops={"path": "text_pattern_ops"}),
    )


# ── 6. agent_configs ──────────────────────────────────────────────────────────


class AgentConfig(Base):
    """Agent configuration per agent type and scope."""

    __tablename__ = "agent_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False, default="repo")
    config_json: Mapped[dict[str, Any]] = mapped_column(SecretJSON, nullable=False)
    track_a_config: Mapped[dict[str, Any] | None] = mapped_column(SecretJSON)
    track_b_config: Mapped[dict[str, Any] | None] = mapped_column(SecretJSON)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("agent_type", "scope_id", name="unique_agent_scope"),)


# ── 7. repo_freshness ────────────────────────────────────────────────────────


class RepoFreshness(Base):
    """Per-repo extraction state for incremental runs and staleness detection."""

    __tablename__ = "repo_freshness"

    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    last_commit_indexed: Mapped[str | None] = mapped_column(Text)
    last_indexed_at: Mapped[datetime | None] = mapped_column()
    index_status: Mapped[str] = mapped_column(Text, nullable=False, default="not_started")
    component_cursor: Mapped[str | None] = mapped_column(Text)
    checkpoint_phase: Mapped[str | None] = mapped_column(Text)
    checkpoint_file_cursor: Mapped[str | None] = mapped_column(Text)
    files_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    component_count: Mapped[int] = mapped_column(Integer, default=0)
    flow_count: Mapped[int] = mapped_column(Integer, default=0)
    dependency_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_commits: Mapped[int] = mapped_column(Integer, default=0)
    freshness_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    stale_reason: Mapped[str | None] = mapped_column(Text)
    incremental_run_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    repo: Mapped[Repo] = relationship(back_populates="freshness")


# ── 7b. repo_ownership — parsed manifest metadata ─────────────────────────


class RepoOwnership(Base):
    """Per-repo structured metadata from the repo manifest.

    Carries squad ownership, compliance, and (critically) the cross-repo
    dependency graph, mined from config blocks and env files by the
    env miner. Research agents use this to scope their searches
    to the right Jira project, Confluence space, Slack channel.
    """

    __tablename__ = "repo_ownership"

    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    name: Mapped[str | None] = mapped_column(Text)
    squad: Mapped[str | None] = mapped_column(Text)
    service_tier: Mapped[str | None] = mapped_column(Text)
    data_classification: Mapped[str | None] = mapped_column(Text)
    compliance_tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    jira_project_key: Mapped[str | None] = mapped_column(Text)
    confluence_space_key: Mapped[str | None] = mapped_column(Text)
    slack_channel: Mapped[str | None] = mapped_column(Text)
    # Google Drive folder where this repo's design docs live. Written
    # only by the ``gdocs_index`` crawler's folder inference; no provider
    # sets it. NULL → the gdocs_agent falls back to repo-name keyword
    # scope.
    gdoc_folder_url: Mapped[str | None] = mapped_column(Text)
    downstream_services: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    messaging_topics: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    database_names: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    raw_manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="manifest",
    )
    last_synced_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_repo_ownership_squad", "squad"),
        Index(
            "idx_repo_ownership_jira",
            "jira_project_key",
            postgresql_where=jira_project_key.isnot(None),
        ),
    )


# ── 7c. external_source_cache — Confluence/Jira ─────────────────────────


class ConfluencePage(Base):
    """Cached Confluence page metadata per repo.

    Indexer populates this from the Atlassian MCP on a schedule; chat
    agents query here first, fall back to live MCP only if the cache is
    stale or missing for a repo.
    """

    __tablename__ = "confluence_pages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    space_key: Mapped[str] = mapped_column(Text, nullable=False)
    page_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[datetime | None] = mapped_column()
    author: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    embedding: Mapped[Any | None] = mapped_column(Vector(384))
    indexed_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_conf_repo", "repo_id"),
        Index("idx_conf_space", "space_key"),
    )


class GDocPage(Base):
    """Cached Google Drive doc metadata per repo.

    Mirror of ``ConfluencePage`` for design docs that live in Google
    Drive. Indexer (``gdocs_index.py``) populates this from the Drive
    MCP on a schedule; the per-repo gdocs agent queries here first and
    falls back to live MCP only when the cache is missing.
    """

    __tablename__ = "gdoc_pages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="application/vnd.google-apps.document",
    )
    owner_email: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[datetime | None] = mapped_column()
    labels: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    embedding: Mapped[Any | None] = mapped_column(Vector(384))
    indexed_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("repo_id", "file_id", name="uq_gdoc_repo_file"),
        Index("idx_gdoc_repo", "repo_id"),
        Index("idx_gdoc_file_id", "file_id"),
    )


class JiraIssue(Base):
    """Cached Jira issue metadata per repo."""

    __tablename__ = "jira_issues"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    issue_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(Text)
    issue_type: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    description_excerpt: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    components: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime | None] = mapped_column()
    embedding: Mapped[Any | None] = mapped_column(Vector(384))
    indexed_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_jira_repo", "repo_id"),
        Index("idx_jira_project", "project_key"),
        Index("idx_jira_updated", "updated_at"),
    )


class ExternalSyncLog(Base):
    """One row per (repo, source) sync run — cheap staleness lookup."""

    __tablename__ = "external_sync_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)  # confluence|jira|github
    last_synced_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )
    items_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("repo_id", "source", name="uq_sync_repo_source"),
        Index("idx_sync_repo", "repo_id"),
    )


# ── 7d. org_repos — org-wide catalog ───────────────────────────────────


class OrgRepo(Base):
    """Catalog row for every repo in the configured GitHub orgs.

    Populated by ``POST /catalog/sync`` which pages through
    ``gh api orgs/<org>/repos``. Enriched (best-effort) with
    squad/tier from each repo's manifest. Chat's skim-search uses
    this to find candidates when registered scope isn't enough.
    """

    __tablename__ = "org_repos"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    primary_language: Mapped[str | None] = mapped_column(Text)
    default_branch: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_fork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pushed_at: Mapped[datetime | None] = mapped_column()
    topics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    squad: Mapped[str | None] = mapped_column(Text)
    service_tier: Mapped[str | None] = mapped_column(Text)
    # Geographic region (e.g. "us", "eu"), a provider-supplied field
    # (backstage label map or generic_yaml ``fields``); NULL when
    # absent. Pooled by the region-scoped handover generator.
    region: Mapped[str | None] = mapped_column(Text)
    # Provider-supplied flag, settable only by generic_yaml. Mirrors
    # ``Repo.is_primary``; propagated by ``_auto_register_from_catalog``
    # when the catalog row gets registered as a Repo.
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    manifest_fetched_at: Mapped[datetime | None] = mapped_column()
    manifest_error: Mapped[str | None] = mapped_column(Text)
    registered_repo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repos.id", ondelete="SET NULL"),
    )
    fetched_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_org_repo_name", "name"),
        Index(
            "idx_org_repo_squad",
            "squad",
            postgresql_where=squad.isnot(None),
        ),
        Index(
            "idx_org_repo_region",
            "region",
            postgresql_where=region.isnot(None),
        ),
        Index("idx_org_repo_pushed", "pushed_at"),
    )


# ── 8. tasks ──────────────────────────────────────────────────────────────────


class ExtractionTask(Base):
    """Extraction-job lifecycle table. One row per submitted extraction.

    Named ``ExtractionTask`` so the user-facing ``Task`` name can
    mean the unit of coding-work the user asks Claude to perform
    end-to-end (Discuss → Plan → Execute → Verify → PR).
    """

    __tablename__ = "extraction_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requester_type: Mapped[str] = mapped_column(Text, nullable=False, default="human")
    requester_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    track: Mapped[str] = mapped_column(Text, nullable=False, default="A")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    consultation_logs: Mapped[list[TaskConsultationLog]] = relationship(back_populates="task")
    session_events: Mapped[list[AgentSessionEvent]] = relationship(back_populates="task")

    __table_args__ = (
        Index("idx_extraction_tasks_status", "status"),
        Index("idx_extraction_tasks_created", "created_at"),
        Index(
            "idx_extraction_tasks_requester",
            "requester_id",
            postgresql_where=requester_id.isnot(None),
        ),
    )


# ── 9. task_consultation_log ──────────────────────────────────────────────────


class TaskConsultationLog(Base):
    """Persistent intermediate results from multi-agent consultations."""

    __tablename__ = "task_consultation_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repos.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    task: Mapped[ExtractionTask] = relationship(back_populates="consultation_logs")

    __table_args__ = (
        Index("idx_tcl_task_id", "task_id"),
        Index("idx_tcl_agent_id", "agent_id"),
    )


# ── 10. llm_cost_log ─────────────────────────────────────────────────────────


class LLMCostLog(Base):
    """One row per LLM API call. Used for cost alerting and audit."""

    __tablename__ = "llm_cost_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repos.id", ondelete="SET NULL"))
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(nullable=False)
    track_b_session_overhead_usd: Mapped[float | None] = mapped_column()
    track_b_mcp_calls: Mapped[int | None] = mapped_column(Integer)
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    call_timestamp: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_llm_cost_task", "task_id"),
        Index("idx_llm_cost_repo", "repo_id", postgresql_where=repo_id.isnot(None)),
        Index("idx_llm_cost_ts", "call_timestamp"),
    )


# ── 11. agent_session_events ─────────────────────────────────────────────────


class AgentSessionEvent(Base):
    """Audit log for agent session lifecycle events."""

    __tablename__ = "agent_session_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_tasks.id", ondelete="SET NULL"),
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    track: Mapped[str] = mapped_column(Text, nullable=False, default="A")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    task: Mapped[ExtractionTask | None] = relationship(back_populates="session_events")

    __table_args__ = (
        Index("idx_ase_session", "session_id"),
        Index("idx_ase_task", "task_id", postgresql_where=task_id.isnot(None)),
        Index("idx_ase_created", "created_at"),
    )


# ── 12. conversations (chat threads) ─────────────────────────────────────────


class Conversation(Base):
    """A persistent chat thread — user asks questions, assistant answers.

    Identity is cookie/session-based for V1 (no real auth). Each conversation
    has an ordered sequence of ConversationMessage rows representing the turns.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)  # cookie-based owner
    # Identity-resolved operator email. Populated at
    # conversation create time when an authenticated principal is
    # available on the request. Audit trail + future "my conversations"
    # filter — does NOT replace ``session_id`` ownership; the session
    # cookie remains the access-control gate so existing conversations
    # created before identity existed still load.
    owner_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Stored as JSONB list of UUID strings to avoid ARRAY(Uuid) driver quirks
    scope_repo_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    scope_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"),
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Soft-delete + archive timestamps. ``archived``
    # boolean predates these and stays for backwards compat (the index
    # below still reads it). Default list/get paths now filter on
    # ``deleted_at IS NULL`` so a hard ``DELETE /conversations/{id}``
    # is no longer destructive — the row is hidden, can be restored,
    # and a separate purge step (future) is the only way to remove
    # forever.
    deleted_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # Per-conversation chat settings (walk depth,
    # confidence floor, edge directions, external-source toggles,
    # …). Stored as JSONB; the application reads through the
    # ``ChatSettings`` Pydantic schema which supplies factory
    # defaults for any missing key, so adding a new knob never
    # needs a migration.
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.turn_index",
    )

    __table_args__ = (
        Index("idx_conversations_session", "session_id", "updated_at"),
        Index("idx_conversations_archived", "archived", postgresql_where=archived.is_(False)),
    )


class ConversationMessage(Base):
    """One turn in a conversation — either a user message or assistant answer."""

    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    confidence: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    gaps: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    token_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    # "Ask next" follow-up prompt suggestions for assistant turns.
    # Each item: {prompt, rationale, repos_to_index}. Empty for user
    # turns / turns where no gaps were found.
    suggested_followups: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    cost_usd: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index", name="uq_conv_msg_turn"),
        Index("idx_conv_messages_conv", "conversation_id", "turn_index"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_role"),
    )


class ConversationResearchState(Base):
    """Per-conversation research bundle for follow-up planning.

    Persistent backup for the in-memory ``BundleCache``. The follow-up
    planner reads this on the first turn after an API restart so a
    long-running chat session doesn't lose continuity. The coordinator
    upserts a row at the end of each successful research turn.

    The ``bundle`` JSONB carries ``ResearchBundle.to_dict()``; the
    embedding lives outside it so a future indexing/sweep job can
    inspect it without unpacking the bundle.
    """

    __tablename__ = "conversation_research_state"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bundle: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    last_question_embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "idx_conversation_research_state_updated_at",
            "updated_at",
        ),
    )


# ── 13. global_rules ─────────────────────────────────────────────────────────


class GlobalRule(Base):
    """Global behavioral rules injected into all agent system prompts."""

    __tablename__ = "global_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    applies_to: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_gr_active_type", "active", "rule_type", postgresql_where=active.is_(True)),
        Index("idx_gr_priority", "priority", postgresql_where=active.is_(True)),
    )


# ── 12. entity_communities ────────────────────────────────────────────────────


class EntityCommunity(Base):
    """A Louvain-clustered group of entities with an LLM-generated summary.

    Powers "global" queries where the user asks about a whole domain or
    area and vector-top-K over individual entities under-retrieves.
    """

    __tablename__ = "entity_communities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_repo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    representative_entity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default="{}",
    )
    centroid: Mapped[Any | None] = mapped_column(Vector(384))
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
    )

    __table_args__ = (Index("idx_entity_communities_scope", "scope_repo_id"),)


# ── 14. handover_docs ─────────────────────────────────────────────


class HandoverDoc(Base):
    """One handover artifact about a scope (feature / repo / domain / region /
    team / session). Only ``session``-scoped briefs are written so far; the
    full handover-doc generation pipeline is not implemented yet."""

    __tablename__ = "handover_docs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_value: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("handover_docs.id", ondelete="SET NULL"),
        nullable=True,
    )
    fs_path: Mapped[str | None] = mapped_column(Text)
    manifest_hash: Mapped[str | None] = mapped_column(Text)
    freshness_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="fresh",
    )
    last_generated_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
    )
    generation_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    generation_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    # Soft-archival. NULL = live (default list filter shows it);
    # non-NULL = archived (hidden from default list). The on-disk file at
    # ``fs_path`` is untouched on archive — restoring just clears this
    # column.
    archived_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        nullable=True,
    )
    # tsvector full-text search column. Postgres-generated +
    # STORED — Postgres recomputes on every UPDATE/INSERT, so the
    # ORM never assigns to it. Body text isn't indexed here (lives on
    # disk under ``fs_path``); the route layers a Python body grep on
    # top for snippet generation. The baseline migration keeps the SQL
    # expression authoritative; the model mirrors it via
    # ``Computed(..., persisted=True)`` so ``Base.metadata.create_all``
    # used by the test suite produces the same column.
    search_tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(scope_value, '') || ' ' || coalesce(scope_kind, ''))",
            persisted=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('feature','repo','domain','region','team','session')",
            name="ck_handover_docs_scope_kind",
        ),
        CheckConstraint(
            "freshness_state IN ('fresh','stale','very_stale',"
            "'enriched_by_children','stale_due_to_children')",
            name="ck_handover_docs_freshness_state",
        ),
        UniqueConstraint(
            "scope_kind",
            "scope_value",
            "parent_id",
            name="uq_handover_docs_scope",
        ),
        Index("idx_handover_docs_scope_kind", "scope_kind"),
        Index(
            "idx_handover_docs_parent",
            "parent_id",
            postgresql_where=parent_id.isnot(None),
        ),
        # Partial index — the default list query filters
        # ``archived_at IS NULL`` so only live rows belong in the b-tree.
        Index(
            "idx_handover_docs_archived_at",
            "archived_at",
            postgresql_where=archived_at.is_(None),
        ),
        # GIN over the generated tsvector. Used by
        # ``EntityRepository.search_handover_docs`` for ``@@`` +
        # ``ts_rank`` ordered lookups.
        Index(
            "idx_handover_docs_search_tsv",
            "search_tsv",
            postgresql_using="gin",
        ),
    )


# ── 14b. handover_audits ───────────────────────────────────────


class HandoverAudit(Base):
    """Append-only lifecycle audit for a :class:`HandoverDoc`.

    One row per mutating event (``generated`` / ``archived`` /
    ``restored`` / ``edited`` / ``promoted``) — reads do NOT emit
    audit rows. ``actor`` carries the cookie session id for
    user-initiated events and ``"system"`` for generator-initiated
    events; NULL is allowed when the route lacks a cookie.

    Intentionally has no FK to ``handover_docs.id`` so the trail
    survives a future hard-delete of the source doc.
    """

    __tablename__ = "handover_audits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        # ``metadata`` is reserved on Declarative — store the column
        # under a different attribute name but keep the column name
        # ``metadata`` in the DB.
        "metadata",
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'generated','archived','restored','edited','promoted',"
            "'restored_version'"
            ")",
            name="ck_handover_audits_event_type",
        ),
        # (doc_id, created_at DESC) — covers the per-doc audit lookup
        # (equality on doc_id, order by created_at desc) in one index
        # hit. The baseline migration keeps the SQL ``DESC`` clause authoritative;
        # the model mirrors the column list (without DESC) so
        # ``Base.metadata.create_all`` used by the test suite produces
        # an equivalent index.
        Index(
            "idx_handover_audits_doc_created",
            "doc_id",
            "created_at",
        ),
    )


# ── 14c. handover_body_history ───────────────────────────────


class HandoverBodyHistory(Base):
    """Pre-overwrite snapshot of a :class:`HandoverDoc` body.

    One row captured *before* each overwrite of the on-disk Markdown
    (regen / edit / promote). Drives the UI diff viewer that lets an
    operator compare the current body against any previous version.

    ``event_type`` mirrors the audit vocabulary at the *point of
    overwrite* — ``pre-regen`` / ``pre-edit`` / ``pre-promote`` —
    rather than the audit log's post-event verbs (``generated`` /
    ``edited`` / ``promoted``). Subtle but useful: a snapshot is
    "the body as it stood before the next event", so naming it after
    the upcoming event keeps the UI labels honest.

    Retention is bounded at 20 rows per ``doc_id`` by the repository
    helper that writes new rows; the migration leaves the table
    unbounded so backfills can replay history without fighting a
    cap-trigger.

    Intentionally has no FK to ``handover_docs.id`` so the snapshot
    trail survives a future hard-delete of the source doc — same
    forensic-survival rationale as :class:`HandoverAudit`.
    """

    __tablename__ = "handover_body_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('pre-regen','pre-edit','pre-promote','pre-restore')",
            name="ck_handover_body_history_event_type",
        ),
        # (doc_id, created_at DESC) — covers the per-doc history
        # lookup in one index hit. The baseline migration keeps the SQL
        # ``DESC`` clause authoritative; the ORM mirrors the column
        # list (without DESC) so ``Base.metadata.create_all`` used in
        # the test suite produces an equivalent index.
        Index(
            "idx_handover_body_history_doc_created",
            "doc_id",
            "created_at",
        ),
    )


# ── 14d. handover_section_feedback (team-collab gap closure) ────────────────


class HandoverSectionFeedback(Base):
    """Per-section feedback row attached to a :class:`HandoverDoc`.

    Lets a reader flag a specific handover section as outdated /
    incorrect / unclear / missing-info / other. Doc owners surface
    these in their inbox and the per-section metadata aggregate;
    resolving a row marks it handled with timestamp + actor.

    Unlike :class:`HandoverAudit` and :class:`HandoverBodyHistory`
    (which intentionally outlive a hard-delete of the source doc),
    feedback IS content-coupled — once the doc is gone, the
    feedback is no longer addressable, so we cascade on
    ``handover_docs.id``.

    ``actor`` / ``resolved_by`` are *verified* cookie session ids
    from the route layer (mirrors the audit log convention).
    """

    __tablename__ = "handover_section_feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("handover_docs.id", ondelete="CASCADE"),
        nullable=False,
    )
    section_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        nullable=True,
    )
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('outdated','incorrect','unclear','missing_info','other')",
            name="ck_handover_section_feedback_kind",
        ),
        # Per-doc listing: equality on doc_id, order by created_at desc.
        # The baseline migration keeps the SQL ``DESC`` clause authoritative;
        # the model mirrors the column list (without DESC) so
        # ``Base.metadata.create_all`` used by the test suite produces
        # an equivalent index.
        Index(
            "idx_handover_feedback_doc",
            "doc_id",
            "created_at",
        ),
        # Partial index over open rows — supports the open-count
        # aggregate the section-metadata + inbox endpoints rely on.
        Index(
            "idx_handover_feedback_open",
            "doc_id",
            postgresql_where=resolved_at.is_(None),
        ),
    )


# ── 15. collaboration_sessions ────────────────────────────────────


class CollaborationSession(Base):
    """A stateful interaction where a requesting team brings a problem,
    the system clarifies in a loop against the handover docs, and the
    synthesizer produces a brief."""

    __tablename__ = "collaboration_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    requester: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="open",
    )
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    matched_doc_ids: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
    )

    messages: Mapped[list[CollabMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="CollabMessage.ord",
    )
    doubts: Mapped[list[CollabDoubt]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="CollabDoubt.created_at",
    )
    artifacts: Mapped[list[CollabArtifact]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="CollabArtifact.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('open','clarifying','resolved','briefed','closed')",
            name="ck_collab_sessions_state",
        ),
        Index("idx_collab_sessions_requester", "requester"),
        Index("idx_collab_sessions_state", "state"),
        Index("idx_collab_sessions_created_at", "created_at"),
    )


# ── 16. collab_messages ───────────────────────────────────────────


class CollabMessage(Base):
    """One message in a collaboration session — requester, system,
    clarifier_agent, or synthesizer_agent."""

    __tablename__ = "collab_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collaboration_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    source_citations: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default="{}",
    )
    doubt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session: Mapped[CollaborationSession] = relationship(back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "role IN ('requester','system','clarifier_agent','synthesizer_agent')",
            name="ck_collab_messages_role",
        ),
        UniqueConstraint(
            "session_id",
            "ord",
            name="uq_collab_messages_session_ord",
        ),
        Index("idx_collab_messages_session", "session_id", "ord"),
    )


# ── 17. collab_doubts ─────────────────────────────────────────────


class CollabDoubt(Base):
    """An open question raised during a collaboration session."""

    __tablename__ = "collab_doubts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collaboration_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="open",
    )
    resolved_md: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
    )

    session: Mapped[CollaborationSession] = relationship(back_populates="doubts")

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','answered_by_doc','answered_by_human','unresolved','escalated')",
            name="ck_collab_doubts_status",
        ),
        Index("idx_collab_doubts_session", "session_id"),
        Index("idx_collab_doubts_status", "status"),
    )


# ── 18. collab_artifacts ──────────────────────────────────────────


class CollabArtifact(Base):
    """A produced artifact (typically the final brief) attached to a
    collaboration session. ``fs_path`` points to the rendered Markdown
    on disk under ``docs/handovers/sessions/<session_id>/``."""

    __tablename__ = "collab_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collaboration_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    fs_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session: Mapped[CollaborationSession] = relationship(back_populates="artifacts")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('final_brief','estimate','risk_log','draft_ask_message')",
            name="ck_collab_artifacts_kind",
        ),
        Index("idx_collab_artifacts_session", "session_id"),
    )


# ── 26. research_runs ────────────────────────────────────────────────────────


class ResearchRun(Base):
    """Durable run-level state for one chat research turn.

    Powers resumability: when the API restarts mid-run, a page refreshes
    during synthesis, or a client retries a doomed POST, we don't pay
    the fan-out cost twice. The coordinator upserts at each
    phase boundary; the API uses ``status`` to detect in-flight runs
    and ``idempotency_key`` to dedup retries within a 24h window.
    """

    __tablename__ = "research_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="pending",
    )
    phase_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    final_answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 6),
        nullable=False,
        server_default="0",
    )
    saved_cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 6),
        nullable=False,
        server_default="0",
    )
    started_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','scoping','fanout','synthesizing','done','failed','cancelled')",
            name="ck_research_runs_status",
        ),
        UniqueConstraint(
            "conversation_id",
            "turn_id",
            name="uq_research_runs_conv_turn",
        ),
        Index(
            "idx_research_runs_idempotency",
            "conversation_id",
            "idempotency_key",
            "started_at",
            postgresql_where="idempotency_key IS NOT NULL",
        ),
        Index(
            "idx_research_runs_in_flight",
            "conversation_id",
            "started_at",
            postgresql_where=("status IN ('pending','scoping','fanout','synthesizing')"),
        ),
    )


# ── 27. agent_results ────────────────────────────────────────────────────────


class AgentResult(Base):
    """Per-agent cached output for one research run.

    Powers resumability: when a run is interrupted and
    retried (same idempotency_key → same research_run resumed), we
    skip every agent that already completed successfully. A 5-repo
    chat that crashed after 3 repos finishes for ~40% of the original
    cost: 2 fresh agents + synthesis.

    ``scope_hash`` is reserved for cross-run reuse (same scoped
    question asked in two different conversations) — looked up via
    ``idx_agent_results_scope_hash`` partial index over success rows.
    """

    __tablename__ = "agent_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    shard_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    scope_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 6),
        nullable=False,
        server_default="0",
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    # Semantic cache columns. ``question_embedding`` lets
    # cross-run lookups match by cosine similarity (paraphrase-tolerant)
    # rather than only on a brittle hand-computed scope_hash. Filled by
    # ``cached_agent`` on every successful agent run.
    question_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(384),
        nullable=True,
    )
    # Code-agent cache invalidation — the cached repo report is only
    # valid for the commit it was produced against. New commit on the
    # repo's HEAD → all old code rows for that repo become unreachable
    # by lookup (we filter by repo_commit_sha equality).
    repo_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-kind freshness budget, baked in at write time so the lookup
    # query is a flat ``created_at > now() - INTERVAL`` predicate.
    ttl_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="3600",
    )
    # Distinguishes a row produced by a fresh agent run (False) from a
    # row persisted as cache-hit attribution for a turn (True). The
    # cache-stats endpoint counts hits via ``was_cache_hit = true``.
    was_cache_hit: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    # When ``was_cache_hit = true``, points at the research_run whose
    # agent output we reused — for audit + UI ("this came from your
    # turn 5 yesterday").
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy_DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "agent_kind IN ('repo','jira','confluence','github','slack','external','gdocs')",
            name="ck_agent_results_kind",
        ),
        CheckConstraint(
            "status IN ('success','error','partial')",
            name="ck_agent_results_status",
        ),
        UniqueConstraint(
            "conversation_id",
            "turn_id",
            "agent_kind",
            "target_id",
            "shard_index",
            name="uq_agent_results_per_turn",
        ),
        Index(
            "idx_agent_results_scope_hash",
            "agent_kind",
            "target_id",
            "scope_hash",
            "created_at",
            postgresql_where="status = 'success'",
        ),
        Index("idx_agent_results_run_id", "research_run_id"),
        # The HNSW index over
        # ``question_embedding`` is created via raw SQL in the
        # migration (Alembic doesn't model HNSW well); the B-tree
        # below scopes the ANN query's exact-match prefix.
        Index(
            "idx_agent_results_kind_target_recent",
            "agent_kind",
            "target_id",
            "shard_index",
            "created_at",
            postgresql_where="status = 'success'",
        ),
    )


# ── 28. benchmark_questions (Tier 3 — benchmark proving ground) ────────────────────


class BenchmarkQuestion(Base):
    """Ground-truth dataset for the chat-research learning loop.

    One row per benchmark question + the operator's pre-written
    expected answer (``expected_entities``, ``expected_jira_refs``, …).
    Loaded from ``docs/benchmarks/<domain>.yml`` via the harness'
    ``ensure_questions_loaded`` step. Append-only — questions don't
    churn, so the score over time is comparable across builds.
    """

    __tablename__ = "benchmark_questions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    question_key: Mapped[str] = mapped_column(Text, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    paraphrase: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_entities: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    expected_jira_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    expected_pr_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    expected_key_facts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    tier_floor: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default="3",
    )
    expects_diagram: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    expected_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope_repo_names: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    question_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("domain", "question_key", name="uq_benchmark_question_key"),
        CheckConstraint(
            "tier_floor BETWEEN 1 AND 5",
            name="bench_q_valid_tier",
        ),
        Index("idx_benchmark_q_domain", "domain"),
    )


# ── 29. benchmark_runs ───────────────────────────────────────────────────────


class BenchmarkRun(Base):
    """One scored execution of a benchmark question.

    Each row is one chat turn the harness executed (Run A cold, Run B
    warm fresh-conv, Run C paraphrased). Stores recall/precision/
    specificity/coverage/tier-integrity along with cost, agent counts,
    and pointers back to ``research_runs`` / ``conversations`` so the
    audit chain is intact.
    """

    __tablename__ = "benchmark_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_kind: Mapped[str] = mapped_column(Text, nullable=False)
    build_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    research_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_confidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    score_recall: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )
    score_precision: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )
    score_specificity: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )
    score_coverage: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )
    score_tier_integrity: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )
    score_evidence_consistency: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )
    score_format_adherence: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="1",
    )
    score_total: Mapped[float] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default="0",
    )

    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        server_default="0",
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    agents_dispatched: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    agents_cache_hit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    facts_injected: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    edges_confirmed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    edges_contradicted: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    new_edges_inserted: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )

    cited_entity_urns: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    cited_jira_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    cited_pr_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    missing_entities: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    missing_key_facts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
    )
    raw_score_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    started_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint("run_kind IN ('A','B','C')", name="bench_run_valid_kind"),
        CheckConstraint(
            "score_total BETWEEN 0 AND 1",
            name="bench_run_valid_total",
        ),
        Index(
            "idx_benchmark_runs_question",
            "question_id",
            "run_kind",
            "started_at",
        ),
        Index(
            "idx_benchmark_runs_build",
            "build_label",
            "started_at",
            postgresql_where=text("build_label IS NOT NULL"),
        ),
    )


class AuditLog(Base):
    """Append-only security-event log (S11 section 16.1, S12 section 9.3).

    The DB enforces append-only via triggers (baseline migration). Application
    code should never UPDATE or DELETE rows; the trigger raises if you
    try. Inserts go through ``bifrost.api.audit.record_event``.

    Field semantics:
      * event_type: dotted namespace, e.g. ``auth.login.success``,
        ``auth.identity.missing``, ``admin.access.denied``,
        ``permission.denied``, ``adapter.config.changed``.
      * actor: resolved principal email if known, else ``"anonymous"``
        or ``"system"``.
      * resource: the URL path or resource URN being accessed.
      * correlation_id: from the X-Correlation-ID middleware so security
        events join cleanly to other request logs.
      * metadata: free-form JSONB for event-specific detail
        (e.g. reason text, source-permission decision, adapter name).
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    timestamp: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",  # Postgres column name; ORM attr renamed to avoid
        # the SQLAlchemy ``metadata`` reserved attribute.
        JSONB,
        nullable=False,
        server_default="{}",
    )
