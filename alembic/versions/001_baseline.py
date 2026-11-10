"""Initial schema.

Revision ID: 001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "agent_configs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.Text(), server_default=sa.text("'''repo'''"), nullable=False),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("track_a_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("track_b_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_configs")),
        sa.UniqueConstraint(
            "agent_type", "scope_id", name=op.f("uq_agent_configs_unique_agent_scope")
        ),
    )

    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("source_ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(op.f("idx_audit_log_actor"), "audit_log", ["actor"])
    op.execute(
        "CREATE INDEX idx_audit_log_correlation_id ON audit_log USING btree (correlation_id) WHERE (correlation_id IS NOT NULL)"
    )
    op.create_index(op.f("idx_audit_log_event_type"), "audit_log", ["event_type"])
    op.execute('CREATE INDEX idx_audit_log_timestamp ON audit_log USING btree ("timestamp")')

    op.create_table(
        "benchmark_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("question_key", sa.Text(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column(
            "paraphrase",
            sa.Text(),
            nullable=True,
            comment="Optional paraphrase used for Run C — semantic-robustness test. NULL means no paraphrase variant exists.",
        ),
        sa.Column(
            "expected_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="List of entity URN strings the answer MUST cite for full recall. Drawn from the entity_relationships graph.",
        ),
        sa.Column(
            "expected_jira_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="List of Jira issue keys the answer should mention.",
        ),
        sa.Column(
            "expected_pr_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="List of GitHub PR URLs/numbers the answer should cite.",
        ),
        sa.Column(
            "expected_key_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="List of {needle, why} dicts. ``needle`` is a string the scorer searches for in the answer text. ``why`` is the operator's note on what fact the needle anchors.",
        ),
        sa.Column(
            "tier_floor",
            sa.SmallInteger(),
            server_default=sa.text("'3'::smallint"),
            nullable=False,
            comment="Highest tier (lowest number) the answer's claims should land at. Questions about live code expect T1; ops questions can settle for T3.",
        ),
        sa.Column(
            "expects_diagram",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="When true, the coverage scorer expects at least one Mermaid HLD/LLD block in the answer.",
        ),
        sa.Column(
            "scope_repo_names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Repos the harness should pin scope_repo_ids to before asking. Empty = let the router pick.",
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "expected_format",
            sa.Text(),
            nullable=True,
            comment="Intended answer format: dossier (default — full deep answer), concise (≤300 words, bullets, no source-coverage block), executive (≤200 words, action-oriented), single_decision (≤100 words, one decision + rationale + owner). NULL means dossier.",
        ),
        sa.Column(
            "max_words",
            sa.Integer(),
            nullable=True,
            comment="Hard cap on answer word count for format_adherence scoring. Defaults to format-derived value when NULL (concise=300, executive=200, single_decision=100).",
        ),
        sa.CheckConstraint(
            "((expected_format IS NULL) OR (expected_format = ANY (ARRAY['dossier'::text, 'concise'::text, 'executive'::text, 'single_decision'::text])))",
            name=op.f("ck_benchmark_questions_bench_q_valid_format"),
        ),
        sa.CheckConstraint(
            "((tier_floor >= 1) AND (tier_floor <= 5))",
            name=op.f("ck_benchmark_questions_bench_q_valid_tier"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_questions")),
        sa.UniqueConstraint("domain", "question_key", name=op.f("uq_benchmark_question_key")),
    )
    op.create_index(op.f("idx_benchmark_q_domain"), "benchmark_questions", ["domain"])

    op.create_table(
        "collab_asks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "asker_email",
            sa.Text(),
            nullable=False,
            comment="Identity-resolved email of the engineer asking.",
        ),
        sa.Column(
            "question",
            sa.Text(),
            nullable=False,
            comment="The actual question. Free text; the Slack draft is generated from this + scope context.",
        ),
        sa.Column(
            "scope_repo_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Repo UUIDs the question is scoped to.",
        ),
        sa.Column(
            "target_squad",
            sa.Text(),
            nullable=True,
            comment="Auto-resolved owning squad (mode of RepoOwnership.squad across scope_repo_ids). NULL when no signal — UI prompts for manual override.",
        ),
        sa.Column(
            "target_slack_channel",
            sa.Text(),
            nullable=True,
            comment="Auto-resolved Slack channel for the squad.",
        ),
        sa.Column(
            "target_jira_project",
            sa.Text(),
            nullable=True,
            comment="Auto-resolved Jira project key — surfaced in the draft so the answerer knows where to file a ticket if the ask becomes work.",
        ),
        sa.Column(
            "slack_draft_md",
            sa.Text(),
            nullable=True,
            comment="LLM-drafted Slack message (Haiku). Operator reviews + edits before sending; we never auto-fire to Slack.",
        ),
        sa.Column(
            "slack_thread_url",
            sa.Text(),
            nullable=True,
            comment="Set when the operator marks the ask as 'sent to Slack' — the URL of the actual posted thread.",
        ),
        sa.Column(
            "answer_conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="If the answer was captured in a Bifrost chat conversation (the common case via the 'Discuss' button), points at that conv. NULL when the answer lives only in Slack.",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'''drafted'''"),
            nullable=False,
            comment="drafted | routed | sent | answered | escalated | cancelled",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = ANY (ARRAY['drafted'::text, 'routed'::text, 'sent'::text, 'answered'::text, 'escalated'::text, 'cancelled'::text]))",
            name=op.f("ck_collab_asks_ck_collab_asks_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collab_asks")),
    )
    op.create_index(op.f("idx_collab_asks_asker"), "collab_asks", ["asker_email", "created_at"])
    op.execute(
        "CREATE INDEX idx_collab_asks_status ON collab_asks USING btree (status) WHERE (status <> ALL (ARRAY['answered'::text, 'cancelled'::text]))"
    )
    op.execute(
        "CREATE INDEX idx_collab_asks_target_squad ON collab_asks USING btree (target_squad) WHERE (target_squad IS NOT NULL)"
    )

    op.create_table(
        "collaboration_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("requester", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("requirement_text", sa.Text(), nullable=False),
        sa.Column(
            "matched_doc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state = ANY (ARRAY['open'::text, 'clarifying'::text, 'resolved'::text, 'briefed'::text, 'closed'::text]))",
            name=op.f("ck_collaboration_sessions_ck_collab_sessions_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collaboration_sessions")),
    )
    op.create_index(
        op.f("idx_collab_sessions_created_at"), "collaboration_sessions", ["created_at"]
    )
    op.create_index(op.f("idx_collab_sessions_requester"), "collaboration_sessions", ["requester"])
    op.create_index(op.f("idx_collab_sessions_state"), "collaboration_sessions", ["state"])

    op.create_table(
        "domains",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("urn", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domains")),
        sa.UniqueConstraint("urn", name=op.f("uq_domains_urn")),
    )
    op.create_index(op.f("idx_domains_urn"), "domains", ["urn"], unique=True)

    op.create_table(
        "extraction_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("scope_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("budget_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "requester_type", sa.Text(), server_default=sa.text("'''human'''"), nullable=False
        ),
        sa.Column("requester_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'''received'''"), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("track", sa.Text(), server_default=sa.text("'''A'''"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(op.f("idx_extraction_tasks_created"), "extraction_tasks", ["created_at"])
    op.execute(
        "CREATE INDEX idx_extraction_tasks_requester ON extraction_tasks USING btree (requester_id) WHERE (requester_id IS NOT NULL)"
    )
    op.create_index(op.f("idx_extraction_tasks_status"), "extraction_tasks", ["status"])

    op.create_table(
        "global_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("rule_type", sa.Text(), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("applies_to", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_global_rules")),
    )
    op.execute(
        "CREATE INDEX idx_gr_active_type ON global_rules USING btree (active, rule_type) WHERE (active = true)"
    )
    op.execute(
        "CREATE INDEX idx_gr_priority ON global_rules USING btree (priority) WHERE (active = true)"
    )

    op.create_table(
        "handover_audits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(event_type = ANY (ARRAY['generated'::text, 'archived'::text, 'restored'::text, 'edited'::text, 'promoted'::text, 'restored_version'::text]))",
            name=op.f("ck_handover_audits_ck_handover_audits_event_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handover_audits")),
    )
    op.execute(
        "CREATE INDEX idx_handover_audits_doc_created ON handover_audits USING btree (doc_id, created_at DESC)"
    )

    op.create_table(
        "handover_body_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(event_type = ANY (ARRAY['pre-regen'::text, 'pre-edit'::text, 'pre-promote'::text, 'pre-restore'::text]))",
            name=op.f("ck_handover_body_history_ck_handover_body_history_event_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handover_body_history")),
    )
    op.execute(
        "CREATE INDEX idx_handover_body_history_doc_created ON handover_body_history USING btree (doc_id, created_at DESC)"
    )

    op.create_table(
        "handover_docs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_value", sa.Text(), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fs_path", sa.Text(), nullable=True),
        sa.Column("manifest_hash", sa.Text(), nullable=True),
        sa.Column("freshness_state", sa.Text(), server_default=sa.text("'fresh'"), nullable=False),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("generation_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english'::regconfig, ((COALESCE(scope_value, ''::text) || ' '::text) || COALESCE(scope_kind, ''::text)))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(freshness_state = ANY (ARRAY['fresh'::text, 'stale'::text, 'very_stale'::text, 'enriched_by_children'::text, 'stale_due_to_children'::text]))",
            name=op.f("ck_handover_docs_ck_handover_docs_freshness_state"),
        ),
        sa.CheckConstraint(
            "(scope_kind = ANY (ARRAY['feature'::text, 'repo'::text, 'domain'::text, 'region'::text, 'team'::text, 'session'::text]))",
            name=op.f("ck_handover_docs_ck_handover_docs_scope_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["handover_docs.id"],
            name=op.f("fk_handover_docs_parent_id_handover_docs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handover_docs")),
        sa.UniqueConstraint(
            "scope_kind", "scope_value", "parent_id", name=op.f("uq_handover_docs_scope")
        ),
    )
    op.execute(
        "CREATE INDEX idx_handover_docs_archived_at ON handover_docs USING btree (archived_at) WHERE (archived_at IS NULL)"
    )
    op.execute(
        "CREATE INDEX idx_handover_docs_parent ON handover_docs USING btree (parent_id) WHERE (parent_id IS NOT NULL)"
    )
    op.create_index(op.f("idx_handover_docs_scope_kind"), "handover_docs", ["scope_kind"])
    op.execute("CREATE INDEX idx_handover_docs_search_tsv ON handover_docs USING gin (search_tsv)")

    op.create_table(
        "agent_session_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("track", sa.Text(), server_default=sa.text("'''A'''"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["extraction_tasks.id"],
            name=op.f("fk_agent_session_events_task_id_tasks"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_session_events")),
    )
    op.create_index(op.f("idx_ase_created"), "agent_session_events", ["created_at"])
    op.create_index(op.f("idx_ase_session"), "agent_session_events", ["session_id"])
    op.execute(
        "CREATE INDEX idx_ase_task ON agent_session_events USING btree (task_id) WHERE (task_id IS NOT NULL)"
    )

    op.create_table(
        "collab_artifacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("fs_path", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = ANY (ARRAY['final_brief'::text, 'estimate'::text, 'risk_log'::text, 'draft_ask_message'::text]))",
            name=op.f("ck_collab_artifacts_ck_collab_artifacts_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collaboration_sessions.id"],
            name=op.f("fk_collab_artifacts_session_id_collaboration_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collab_artifacts")),
    )
    op.create_index(op.f("idx_collab_artifacts_session"), "collab_artifacts", ["session_id"])

    op.create_table(
        "collab_doubts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("resolved_md", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = ANY (ARRAY['open'::text, 'answered_by_doc'::text, 'answered_by_human'::text, 'unresolved'::text, 'escalated'::text]))",
            name=op.f("ck_collab_doubts_ck_collab_doubts_status"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collaboration_sessions.id"],
            name=op.f("fk_collab_doubts_session_id_collaboration_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collab_doubts")),
    )
    op.create_index(op.f("idx_collab_doubts_session"), "collab_doubts", ["session_id"])
    op.create_index(op.f("idx_collab_doubts_status"), "collab_doubts", ["status"])

    op.create_table(
        "collab_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column(
            "source_citations",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("doubt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(role = ANY (ARRAY['requester'::text, 'system'::text, 'clarifier_agent'::text, 'synthesizer_agent'::text]))",
            name=op.f("ck_collab_messages_ck_collab_messages_role"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collaboration_sessions.id"],
            name=op.f("fk_collab_messages_session_id_collaboration_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collab_messages")),
        sa.UniqueConstraint("session_id", "ord", name=op.f("uq_collab_messages_session_ord")),
    )
    op.create_index(op.f("idx_collab_messages_session"), "collab_messages", ["session_id", "ord"])

    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "scope_repo_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("scope_domain_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "settings_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "owner_email",
            sa.Text(),
            nullable=True,
            comment="Identity-resolved operator email at conversation-create time. NULL for pre-identity rows. session_id remains the access-control owner.",
        ),
        sa.ForeignKeyConstraint(
            ["scope_domain_id"],
            ["domains.id"],
            name=op.f("fk_conversations_scope_domain_id_domains"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.execute(
        "CREATE INDEX idx_conversations_archived ON conversations USING btree (archived) WHERE (archived = false)"
    )
    op.execute(
        "CREATE INDEX idx_conversations_live ON conversations USING btree (session_id, updated_at) WHERE (deleted_at IS NULL)"
    )
    op.execute(
        "CREATE INDEX idx_conversations_owner_email ON conversations USING btree (owner_email, updated_at DESC) WHERE (owner_email IS NOT NULL)"
    )
    op.create_index(
        op.f("idx_conversations_session"), "conversations", ["session_id", "updated_at"]
    )

    op.create_table(
        "handover_section_feedback",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("section_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = ANY (ARRAY['outdated'::text, 'incorrect'::text, 'unclear'::text, 'missing_info'::text, 'other'::text]))",
            name=op.f("ck_handover_section_feedback_ck_handover_section_feedback_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["handover_docs.id"],
            name=op.f("fk_handover_section_feedback_doc_id_handover_docs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handover_section_feedback")),
    )
    op.execute(
        "CREATE INDEX idx_handover_feedback_doc ON handover_section_feedback USING btree (doc_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_handover_feedback_open ON handover_section_feedback USING btree (doc_id) WHERE (resolved_at IS NULL)"
    )

    op.create_table(
        "repos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("urn", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("local_clone_path", sa.Text(), nullable=True),
        sa.Column(
            "adapter_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_repos_domain_id_domains"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repos")),
        sa.UniqueConstraint("urn", name=op.f("uq_repos_urn")),
    )
    op.execute(
        "CREATE INDEX idx_repos_domain ON repos USING btree (domain_id) WHERE (domain_id IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_repos_domain_primary ON repos USING btree (domain_id, is_primary) WHERE ((domain_id IS NOT NULL) AND is_primary)"
    )
    op.create_index(op.f("idx_repos_urn"), "repos", ["urn"], unique=True)

    op.create_table(
        "confluence_pages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_key", sa.Text(), nullable=False),
        sa.Column("page_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("body_excerpt", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.DateTime(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("indexed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_confluence_pages_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confluence_pages")),
        sa.UniqueConstraint("repo_id", "page_id", name=op.f("uq_conf_repo_page")),
    )
    op.create_index(op.f("idx_conf_repo"), "confluence_pages", ["repo_id"])
    op.create_index(op.f("idx_conf_space"), "confluence_pages", ["space_key"])
    op.execute(
        "CREATE INDEX idx_confluence_embedding ON confluence_pages USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_confluence_pages_expires_at ON confluence_pages USING btree (expires_at) WHERE (expires_at IS NOT NULL)"
    )

    op.create_table(
        "conversation_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "gaps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "token_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("cost_usd", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "suggested_followups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(role = ANY (ARRAY['user'::text, 'assistant'::text]))",
            name=op.f("ck_conversation_messages_ck_role"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
        sa.UniqueConstraint("conversation_id", "turn_index", name=op.f("uq_conv_msg_turn")),
    )
    op.create_index(
        op.f("idx_conv_messages_conv"), "conversation_messages", ["conversation_id", "turn_index"]
    )

    op.create_table(
        "conversation_research_state",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bundle", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "last_question_embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_research_state_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_conversation_research_state")),
    )
    op.create_index(
        op.f("idx_conversation_research_state_updated_at"),
        "conversation_research_state",
        ["updated_at"],
    )

    op.create_table(
        "entity_communities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("entity_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "representative_entity_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("centroid", Vector(384), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["scope_repo_id"],
            ["repos.id"],
            name=op.f("fk_entity_communities_scope_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_communities")),
    )
    op.execute(
        "CREATE INDEX idx_entity_communities_centroid ON entity_communities USING hnsw (centroid vector_cosine_ops)"
    )
    op.create_index(op.f("idx_entity_communities_scope"), "entity_communities", ["scope_repo_id"])

    op.create_table(
        "external_sync_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("items_indexed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_external_sync_log_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_sync_log")),
        sa.UniqueConstraint("repo_id", "source", name=op.f("uq_sync_repo_source")),
    )
    op.create_index(op.f("idx_sync_repo"), "external_sync_log", ["repo_id"])

    op.create_table(
        "gdoc_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "file_id",
            sa.Text(),
            nullable=False,
            comment="Drive file id — stable, primary identifier",
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "mime_type",
            sa.Text(),
            server_default=sa.text("'application/vnd.google-apps.document'"),
            nullable=False,
        ),
        sa.Column("owner_email", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "body_excerpt",
            sa.Text(),
            nullable=True,
            comment="First ~2k chars of the doc body for keyword match",
        ),
        sa.Column("last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_gdoc_pages_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gdoc_pages")),
        sa.UniqueConstraint("repo_id", "file_id", name=op.f("uq_gdoc_repo_file")),
    )
    op.execute(
        "CREATE INDEX idx_gdoc_embedding ON gdoc_pages USING hnsw (embedding vector_cosine_ops) WHERE (embedding IS NOT NULL)"
    )
    op.create_index(op.f("idx_gdoc_file_id"), "gdoc_pages", ["file_id"])
    op.create_index(op.f("idx_gdoc_repo"), "gdoc_pages", ["repo_id"])

    op.create_table(
        "jira_issues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_key", sa.Text(), nullable=False),
        sa.Column("issue_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("issue_type", sa.Text(), nullable=True),
        sa.Column("priority", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "components",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("indexed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_jira_issues_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jira_issues")),
        sa.UniqueConstraint("repo_id", "issue_key", name=op.f("uq_jira_repo_issue")),
    )
    op.execute(
        "CREATE INDEX idx_jira_embedding ON jira_issues USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_jira_issues_expires_at ON jira_issues USING btree (expires_at) WHERE (expires_at IS NOT NULL)"
    )
    op.create_index(op.f("idx_jira_project"), "jira_issues", ["project_key"])
    op.create_index(op.f("idx_jira_repo"), "jira_issues", ["repo_id"])
    op.create_index(op.f("idx_jira_updated"), "jira_issues", ["updated_at"])

    op.create_table(
        "knowledge_text_cache",
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_knowledge_text_cache_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("path", name=op.f("pk_knowledge_text_cache")),
    )
    op.execute(
        "CREATE INDEX idx_ktc_path_prefix ON knowledge_text_cache USING btree (path text_pattern_ops)"
    )
    op.create_index(op.f("idx_ktc_repo"), "knowledge_text_cache", ["repo_id"])

    op.create_table(
        "llm_cost_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", postgresql.DOUBLE_PRECISION(), nullable=False),
        sa.Column("track_b_session_overhead_usd", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column("track_b_mcp_calls", sa.Integer(), nullable=True),
        sa.Column("skipped", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("call_timestamp", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_llm_cost_log_repo_id_repos"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_cost_log")),
    )
    op.execute(
        "CREATE INDEX idx_llm_cost_repo ON llm_cost_log USING btree (repo_id) WHERE (repo_id IS NOT NULL)"
    )
    op.create_index(op.f("idx_llm_cost_task"), "llm_cost_log", ["task_id"])
    op.create_index(op.f("idx_llm_cost_ts"), "llm_cost_log", ["call_timestamp"])

    op.create_table(
        "org_repos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("primary_language", sa.Text(), nullable=True),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_fork", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_private", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stars", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "topics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("squad", sa.Text(), nullable=True),
        sa.Column("service_tier", sa.Text(), nullable=True),
        sa.Column("manifest_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("manifest_error", sa.Text(), nullable=True),
        sa.Column("registered_repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("region", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["registered_repo_id"],
            ["repos.id"],
            name=op.f("fk_org_repos_registered_repo_id_repos"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_repos")),
        sa.UniqueConstraint("owner", "name", name=op.f("uq_org_repo_ownername")),
    )
    op.create_index(op.f("idx_org_repo_name"), "org_repos", ["name"])
    op.create_index(op.f("idx_org_repo_pushed"), "org_repos", ["pushed_at"])
    op.execute(
        "CREATE INDEX idx_org_repo_region ON org_repos USING btree (region) WHERE (region IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_org_repo_squad ON org_repos USING btree (squad) WHERE (squad IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_org_repos_squad_primary ON org_repos USING btree (squad, is_primary) WHERE ((squad IS NOT NULL) AND is_primary)"
    )

    op.create_table(
        "repo_freshness",
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_commit_indexed", sa.Text(), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "index_status", sa.Text(), server_default=sa.text("'''not_started'''"), nullable=False
        ),
        sa.Column("component_cursor", sa.Text(), nullable=True),
        sa.Column("checkpoint_phase", sa.Text(), nullable=True),
        sa.Column("checkpoint_file_cursor", sa.Text(), nullable=True),
        sa.Column("files_analyzed", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("component_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("flow_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("dependency_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("pending_commits", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column(
            "freshness_state", sa.Text(), server_default=sa.text("'''unknown'''"), nullable=False
        ),
        sa.Column("stale_reason", sa.Text(), nullable=True),
        sa.Column("incremental_run_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_repo_freshness_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("repo_id", name=op.f("pk_repo_freshness")),
    )

    op.create_table(
        "repo_ownership",
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("squad", sa.Text(), nullable=True),
        sa.Column("service_tier", sa.Text(), nullable=True),
        sa.Column("data_classification", sa.Text(), nullable=True),
        sa.Column(
            "compliance_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("jira_project_key", sa.Text(), nullable=True),
        sa.Column("confluence_space_key", sa.Text(), nullable=True),
        sa.Column("slack_channel", sa.Text(), nullable=True),
        sa.Column(
            "downstream_services",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "messaging_topics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "database_names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "raw_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), server_default=sa.text("'manifest'"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "gdoc_folder_url",
            sa.Text(),
            nullable=True,
            comment="Google Drive folder URL where this repo's design docs live. Sourced from the repo manifest's gdoc key or the offline gdocs_index crawler.",
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_repo_ownership_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("repo_id", name=op.f("pk_repo_ownership")),
    )
    op.execute(
        "CREATE INDEX idx_repo_ownership_jira ON repo_ownership USING btree (jira_project_key) WHERE (jira_project_key IS NOT NULL)"
    )
    op.create_index(op.f("idx_repo_ownership_squad"), "repo_ownership", ["squad"])

    op.create_table(
        "research_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "phase_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("final_answer_text", sa.Text(), nullable=True),
        sa.Column("total_cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("saved_cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = ANY (ARRAY['pending'::text, 'scoping'::text, 'fanout'::text, 'synthesizing'::text, 'done'::text, 'failed'::text, 'cancelled'::text]))",
            name=op.f("ck_research_runs_ck_research_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_research_runs_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_runs")),
        sa.UniqueConstraint("conversation_id", "turn_id", name=op.f("uq_research_runs_conv_turn")),
    )
    op.execute(
        "CREATE INDEX idx_research_runs_idempotency ON research_runs USING btree (conversation_id, idempotency_key, started_at) WHERE (idempotency_key IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_research_runs_in_flight ON research_runs USING btree (conversation_id, started_at) WHERE (status = ANY (ARRAY['pending'::text, 'scoping'::text, 'fanout'::text, 'synthesizing'::text]))"
    )

    op.create_table(
        "task_consultation_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_task_consultation_log_repo_id_repos"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["extraction_tasks.id"],
            name=op.f("fk_task_consultation_log_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_consultation_log")),
    )
    op.create_index(op.f("idx_tcl_agent_id"), "task_consultation_log", ["agent_id"])
    op.create_index(op.f("idx_tcl_task_id"), "task_consultation_log", ["task_id"])

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'draft_intent'"), nullable=False),
        sa.Column("created_from", sa.Text(), server_default=sa.text("'direct'"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("jira_key", sa.Text(), nullable=True),
        sa.Column("intent_text", sa.Text(), nullable=False),
        sa.Column(
            "acceptance_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("worktree_path", sa.Text(), nullable=True),
        sa.Column("branch_name", sa.Text(), nullable=True),
        sa.Column(
            "clarifications_pending", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("phases_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("phases_done", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "budget_ask_backs_max", sa.Integer(), server_default=sa.text("10"), nullable=False
        ),
        sa.Column(
            "budget_cost_max_usd", sa.Numeric(10, 4), server_default=sa.text("25.0"), nullable=False
        ),
        sa.Column(
            "budget_extractions_max", sa.Integer(), server_default=sa.text("3"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("jira_enrichment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "owner_email",
            sa.Text(),
            nullable=True,
            comment="Identity-resolved email of the engineer who owns this task. Populated at create time from the authenticated principal (or from conversations.owner_email for chat-spawned tasks). Drives the collaboration primitives' per-engineer rollups.",
        ),
        sa.CheckConstraint(
            "(created_from = ANY (ARRAY['chat'::text, 'jira'::text, 'direct'::text]))",
            name=op.f("ck_briefs_brief_valid_source"),
        ),
        sa.CheckConstraint(
            "(state = ANY (ARRAY['draft_intent'::text, 'discussing'::text, 'discuss_done'::text, 'drafting_plan'::text, 'plan_ready'::text, 'executing'::text, 'verifying'::text, 'success'::text, 'blocked'::text, 'cancelled'::text, 'pr_merged'::text]))",
            name=op.f("ck_briefs_brief_valid_state"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_briefs_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefs")),
    )
    op.execute(
        "CREATE INDEX idx_briefs_conv ON tasks USING btree (conversation_id) WHERE (conversation_id IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_briefs_jira ON tasks USING btree (jira_key) WHERE (jira_key IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_tasks_archived ON tasks USING btree (archived_at) WHERE (archived_at IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_tasks_owner_email ON tasks USING btree (owner_email, updated_at DESC) WHERE (owner_email IS NOT NULL)"
    )
    op.create_index(op.f("idx_tasks_state"), "tasks", ["state"])

    op.create_table(
        "agent_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("agent_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("shard_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("scope_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("question_embedding", Vector(384), nullable=True),
        sa.Column("repo_commit_sha", sa.Text(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), server_default=sa.text("3600"), nullable=False),
        sa.Column("was_cache_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(agent_kind = ANY (ARRAY['repo'::text, 'jira'::text, 'confluence'::text, 'github'::text, 'slack'::text, 'external'::text, 'gdocs'::text]))",
            name=op.f("ck_agent_results_ck_agent_results_kind"),
        ),
        sa.CheckConstraint(
            "(status = ANY (ARRAY['success'::text, 'error'::text, 'partial'::text]))",
            name=op.f("ck_agent_results_ck_agent_results_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_agent_results_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_runs.id"],
            name=op.f("fk_agent_results_research_run_id_research_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["research_runs.id"],
            name=op.f("fk_agent_results_source_run_id_research_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_results")),
        sa.UniqueConstraint(
            "conversation_id",
            "turn_id",
            "agent_kind",
            "target_id",
            "shard_index",
            name=op.f("uq_agent_results_per_turn"),
        ),
    )
    op.execute(
        "CREATE INDEX idx_agent_results_kind_target_recent ON agent_results USING btree (agent_kind, target_id, shard_index, created_at DESC) WHERE (status = 'success'::text)"
    )
    op.create_index(op.f("idx_agent_results_run_id"), "agent_results", ["research_run_id"])
    op.execute(
        "CREATE INDEX idx_agent_results_scope_hash ON agent_results USING btree (agent_kind, target_id, scope_hash, created_at) WHERE (status = 'success'::text)"
    )
    op.execute(
        "CREATE INDEX idx_agent_results_semantic ON agent_results USING hnsw (question_embedding vector_cosine_ops) WHERE ((status = 'success'::text) AND (question_embedding IS NOT NULL))"
    )

    op.create_table(
        "benchmark_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_kind",
            sa.Text(),
            nullable=False,
            comment="'A' = cold cache, 'B' = warm cache fresh conv, 'C' = warm cache paraphrased question.",
        ),
        sa.Column(
            "build_label",
            sa.Text(),
            nullable=True,
            comment="Free-form label so a future commit can be compared against today's run (e.g., 'baseline-run-1').",
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column(
            "answer_confidence",
            sa.Text(),
            nullable=True,
            comment="confirmed | inferred | uncertain | conflicting | …",
        ),
        sa.Column(
            "score_recall",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="0..1 — fraction of expected_entities cited",
        ),
        sa.Column(
            "score_precision",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="0..1 — fraction of cited entities in expected_entities or one-hop adjacent",
        ),
        sa.Column(
            "score_specificity",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="0..1 — fraction of body claims with file:line/PR/Jira ref",
        ),
        sa.Column(
            "score_coverage",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="0..1 — required sections present (HLD/LLD diagrams, Source coverage, etc.)",
        ),
        sa.Column(
            "score_tier_integrity",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="0..1 — citations land at expected tier or higher",
        ),
        sa.Column(
            "score_total",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="weighted blend of the above; 0..1.",
        ),
        sa.Column(
            "cost_usd", sa.Numeric(10, 4), server_default=sa.text("'0'::numeric"), nullable=False
        ),
        sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("agents_dispatched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "agents_cache_hit",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Number of per-source agents that hit the agent_results semantic cache (Tier 1 of the cost-saving stack).",
        ),
        sa.Column(
            "facts_injected",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Number of ``Known facts`` bullets the read path (graph_facts) injected into agent prompts. Tier 2.",
        ),
        sa.Column("edges_confirmed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("edges_contradicted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("new_edges_inserted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cited_entity_urns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "cited_jira_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "cited_pr_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "missing_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="expected_entities the answer failed to cite — recall miss list.",
        ),
        sa.Column(
            "missing_key_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "raw_score_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Per-component sub-scores so the UI can show 'recall: 8/12 entities cited' rather than just '0.667'.",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "score_evidence_consistency",
            sa.Numeric(4, 3),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
            comment="0..1 — 1 if headline confidence matches the body's evidence coverage; 0 if 'confirmed' headline appears alongside gap markers like 'not routed', 'search failed', 'exact caller unknown'.",
        ),
        sa.Column(
            "score_format_adherence",
            sa.Numeric(4, 3),
            server_default=sa.text("'1'::numeric"),
            nullable=False,
            comment="0..1 — answer respects the question's expected_format. Defaults to 1 when no format expectation is set.",
        ),
        sa.CheckConstraint(
            "(run_kind = ANY (ARRAY['A'::text, 'B'::text, 'C'::text]))",
            name=op.f("ck_benchmark_runs_bench_run_valid_kind"),
        ),
        sa.CheckConstraint(
            "((score_total >= (0)::numeric) AND (score_total <= (1)::numeric))",
            name=op.f("ck_benchmark_runs_bench_run_valid_total"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_benchmark_runs_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["benchmark_questions.id"],
            name=op.f("fk_benchmark_runs_question_id_benchmark_questions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_runs.id"],
            name=op.f("fk_benchmark_runs_research_run_id_research_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_runs")),
    )
    op.execute(
        "CREATE INDEX idx_benchmark_runs_build ON benchmark_runs USING btree (build_label, started_at) WHERE (build_label IS NOT NULL)"
    )
    op.create_index(
        op.f("idx_benchmark_runs_question"),
        "benchmark_runs",
        ["question_id", "run_kind", "started_at"],
    )

    op.create_table(
        "conversation_task_links",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("link_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), server_default=sa.text("'user'"), nullable=False),
        sa.CheckConstraint(
            "(created_by = ANY (ARRAY['user'::text, 'system'::text, 'agent'::text]))",
            name=op.f("ck_conversation_brief_links_link_valid_creator"),
        ),
        sa.CheckConstraint(
            "(link_type = ANY (ARRAY['spawned_brief'::text, 'spawned_chat'::text, 'reference'::text, 'correction'::text]))",
            name=op.f("ck_conversation_brief_links_link_valid_type"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_conversation_brief_links_brief_id_briefs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_brief_links_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["link_message_id"],
            ["conversation_messages.id"],
            name=op.f("fk_conversation_brief_links_link_message_id_conversatio_c2e2"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id", "task_id", "link_type", name=op.f("pk_conv_brief_link")
        ),
    )
    op.create_index(
        op.f("idx_conv_task_links_conv"), "conversation_task_links", ["conversation_id"]
    )
    op.create_index(op.f("idx_conv_task_links_task"), "conversation_task_links", ["task_id"])

    op.create_table(
        "entities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("urn", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=True),
        sa.Column("scope_repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_domain_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "knowledge_tier",
            sa.SmallInteger(),
            server_default=sa.text("'1'::smallint"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column(
            "confidence",
            postgresql.DOUBLE_PRECISION(),
            server_default=sa.text("'0.5'::double precision"),
            nullable=False,
        ),
        sa.Column(
            "confidence_level", sa.Text(), server_default=sa.text("'''uncertain'''"), nullable=False
        ),
        sa.Column(
            "source_basis",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "freshness_state", sa.Text(), server_default=sa.text("'''unknown'''"), nullable=False
        ),
        sa.Column("last_refreshed", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision))",
            name=op.f("ck_entities_ck_entities_valid_confidence"),
        ),
        sa.CheckConstraint(
            "(confidence_level = ANY (ARRAY['confirmed'::text, 'inferred'::text, 'stale'::text, 'uncertain'::text, 'unknown'::text, 'conflicting'::text]))",
            name=op.f("ck_entities_ck_entities_valid_confidence_level"),
        ),
        sa.CheckConstraint(
            "(freshness_state = ANY (ARRAY['fresh'::text, 'stale'::text, 'unknown'::text]))",
            name=op.f("ck_entities_ck_entities_valid_freshness"),
        ),
        sa.CheckConstraint(
            "((knowledge_tier >= 0) AND (knowledge_tier <= 3))",
            name=op.f("ck_entities_ck_entities_valid_knowledge_tier"),
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["entity_communities.id"],
            name=op.f("fk_entities_community_id_entity_communities"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["scope_domain_id"],
            ["domains.id"],
            name=op.f("fk_entities_scope_domain_id_domains"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["scope_repo_id"],
            ["repos.id"],
            name=op.f("fk_entities_scope_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        sa.UniqueConstraint("urn", name=op.f("uq_entities_urn")),
    )
    op.create_index(op.f("idx_entities_community"), "entities", ["community_id"])
    op.execute(
        "CREATE INDEX idx_entities_embedding ON entities USING hnsw (embedding vector_cosine_ops)"
    )
    op.create_index(
        op.f("idx_entities_freshness"), "entities", ["freshness_state", "last_refreshed"]
    )
    op.execute("CREATE INDEX idx_entities_metadata ON entities USING gin (metadata)")
    op.create_index(op.f("idx_entities_qualified_name"), "entities", ["qualified_name"])
    op.execute(
        "CREATE INDEX idx_entities_scope_domain ON entities USING btree (scope_domain_id) WHERE (scope_domain_id IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_entities_scope_repo ON entities USING btree (scope_repo_id) WHERE (scope_repo_id IS NOT NULL)"
    )
    op.create_index(op.f("idx_entities_type"), "entities", ["entity_type"])

    op.create_table(
        "session_task_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collaboration_sessions.id"],
            name=op.f("fk_session_task_links_session_id_collaboration_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_session_task_links_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_task_links")),
        sa.UniqueConstraint("session_id", "task_id", name=op.f("uq_session_task_link")),
    )
    op.create_index(op.f("idx_session_task_links_session"), "session_task_links", ["session_id"])
    op.create_index(op.f("idx_session_task_links_task"), "session_task_links", ["task_id"])

    op.create_table(
        "task_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_brief_events_brief_id_briefs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_events")),
    )
    op.create_index(op.f("idx_events_brief"), "task_events", ["task_id", "at"])

    op.create_table(
        "task_jira_audit",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "jira_key",
            sa.Text(),
            nullable=True,
            comment="Target Jira key. NULL on a failed create when the key wasn't issued.",
        ),
        sa.Column(
            "action", sa.Text(), nullable=False, comment="create | transition | comment | retract"
        ),
        sa.Column(
            "before_state",
            sa.Text(),
            nullable=True,
            comment="Bifrost task state at the moment of the write — lets retract reverse a stale transition without re-fetching.",
        ),
        sa.Column(
            "after_state",
            sa.Text(),
            nullable=True,
            comment="Bifrost state the action attempted to put it in.",
        ),
        sa.Column(
            "jira_state_before",
            sa.Text(),
            nullable=True,
            comment="Jira workflow state name pre-transition.",
        ),
        sa.Column(
            "jira_state_after",
            sa.Text(),
            nullable=True,
            comment="Jira workflow state name post-transition.",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Full input payload (summary, description, comment body, transition id) for replay/debug.",
        ),
        sa.Column(
            "mcp_response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="MCP tool response (Atlassian's confirmation).",
        ),
        sa.Column(
            "ok",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="False when the MCP call returned an error.",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "actor",
            sa.Text(),
            server_default=sa.text("'''system'''"),
            nullable=False,
            comment="who initiated — 'user' | 'system' | 'pr_observer'",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "retracted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when this audit row is reversed by a subsequent retract. Original row stays so the action history is complete; the follow-up retract row links back via payload.retracts_audit_id.",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_task_jira_audit_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_jira_audit")),
    )
    op.execute(
        "CREATE INDEX idx_task_jira_audit_jira ON task_jira_audit USING btree (jira_key) WHERE (jira_key IS NOT NULL)"
    )
    op.create_index(op.f("idx_task_jira_audit_task"), "task_jira_audit", ["task_id", "created_at"])

    op.create_table(
        "task_phases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'drafting_discuss'"), nullable=False),
        sa.Column("current_step", sa.Text(), server_default=sa.text("'discuss'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(current_step = ANY (ARRAY['discuss'::text, 'plan'::text, 'execute'::text, 'verify'::text, 'done'::text]))",
            name=op.f("ck_brief_phases_phase_valid_step"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_brief_phases_brief_id_briefs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_phases")),
        sa.UniqueConstraint("task_id", "order_index", name=op.f("uq_brief_phase_order")),
    )
    op.create_index(op.f("idx_phases_task"), "task_phases", ["task_id"])

    op.create_table(
        "agent_judgements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_kind", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column(
            "cited_tiers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "cited_urns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correction_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(outcome = ANY (ARRAY['pending'::text, 'confirmed_by_user'::text, 'corrected_by_user'::text, 'confirmed_by_verify'::text, 'contradicted_by_verify'::text]))",
            name=op.f("ck_agent_judgements_judgement_valid_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_agent_judgements_brief_id_briefs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_agent_judgements_phase_id_brief_phases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_judgements")),
    )
    op.create_index(
        op.f("idx_judgements_brief_kind"), "agent_judgements", ["task_id", "agent_kind"]
    )

    op.create_table(
        "entity_relationships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rel_type", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            postgresql.DOUBLE_PRECISION(),
            server_default=sa.text("'0.5'::double precision"),
            nullable=False,
        ),
        sa.Column(
            "source_basis",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "freshness_state", sa.Text(), server_default=sa.text("'''fresh'''"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(source_id <> target_id)",
            name=op.f("ck_entity_relationships_ck_entity_relationships_no_self_ec28"),
        ),
        sa.CheckConstraint(
            "((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision))",
            name=op.f("ck_entity_relationships_ck_entity_relationships_valid_c_befd"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["entities.id"],
            name=op.f("fk_entity_relationships_source_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["entities.id"],
            name=op.f("fk_entity_relationships_target_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_relationships")),
        sa.UniqueConstraint("source_id", "target_id", "rel_type", name=op.f("uq_rel_unique_edge")),
    )
    op.create_index(op.f("idx_rel_source"), "entity_relationships", ["source_id"])
    op.create_index(op.f("idx_rel_source_type"), "entity_relationships", ["source_id", "rel_type"])
    op.create_index(op.f("idx_rel_target"), "entity_relationships", ["target_id"])
    op.create_index(op.f("idx_rel_type"), "entity_relationships", ["rel_type"])

    op.create_table(
        "task_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("body_md", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column(
            "parsed",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("produced_by_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = ANY (ARRAY['context'::text, 'research'::text, 'plan'::text, 'summary'::text, 'verify'::text]))",
            name=op.f("ck_brief_artifacts_artifact_valid_kind"),
        ),
        sa.CheckConstraint(
            "(state = ANY (ARRAY['draft'::text, 'approved'::text, 'stale'::text, 'invalidated'::text]))",
            name=op.f("ck_brief_artifacts_artifact_valid_state"),
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_brief_artifacts_phase_id_brief_phases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_artifacts")),
    )
    op.create_index(op.f("idx_artifacts_phase_kind"), "task_artifacts", ["phase_id", "kind"])

    op.create_table(
        "task_assumptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("retracted_by_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(source_type = ANY (ARRAY['user_stated'::text, 'code_verified'::text, 'manifest'::text, 'gradle'::text, 'package_json'::text, 'metadata_match'::text, 'confluence'::text, 'jira'::text, 'github_issue'::text, 'slack'::text, 'fuzzy_match'::text, 'llm_guess'::text, 'research'::text]))",
            name=op.f("ck_brief_assumptions_assumption_valid_source"),
        ),
        sa.CheckConstraint(
            "(state = ANY (ARRAY['active'::text, 'retracted'::text]))",
            name=op.f("ck_brief_assumptions_assumption_valid_state"),
        ),
        sa.CheckConstraint(
            "((tier >= 1) AND (tier <= 5))", name=op.f("ck_brief_assumptions_assumption_valid_tier")
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["task_artifacts.id"],
            name=op.f("fk_brief_assumptions_artifact_id_brief_artifacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retracted_by_artifact_id"],
            ["task_artifacts.id"],
            name=op.f("fk_brief_assumptions_retracted_by_artifact_id_brief_artifacts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_assumptions")),
        sa.UniqueConstraint("artifact_id", "key", name=op.f("uq_artifact_assumption_key")),
    )
    op.create_index(op.f("idx_assumptions_artifact"), "task_assumptions", ["artifact_id"])

    op.create_table(
        "task_clarifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("blocks_state", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(source = ANY (ARRAY['agent'::text, 'user'::text, 'system'::text]))",
            name=op.f("ck_brief_clarifications_clar_valid_source"),
        ),
        sa.CheckConstraint(
            "(state = ANY (ARRAY['open'::text, 'answered'::text, 'skipped'::text, 'superseded'::text]))",
            name=op.f("ck_brief_clarifications_clar_valid_state"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_brief_clarifications_brief_id_briefs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["origin_artifact_id"],
            ["task_artifacts.id"],
            name=op.f("fk_brief_clarifications_origin_artifact_id_brief_artifacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_brief_clarifications_phase_id_brief_phases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_clarifications")),
    )
    op.execute(
        "CREATE INDEX idx_clar_brief_open ON task_clarifications USING btree (task_id) WHERE (state = 'open'::text)"
    )

    op.create_table(
        "task_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("derived_from_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("plan_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "files",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "urns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("action_body", sa.Text(), nullable=False),
        sa.Column("verify_cmd", sa.Text(), nullable=True),
        sa.Column("done_criterion", sa.Text(), nullable=False),
        sa.Column(
            "confidence_basis",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("wave", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "depends_on",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "assumption_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("commit_msg", sa.Text(), nullable=True),
        sa.Column("output_log", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = ANY (ARRAY['pending'::text, 'approved'::text, 'executing'::text, 'completed'::text, 'failed'::text, 'rejected'::text]))",
            name=op.f("ck_brief_plans_plan_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["derived_from_artifact_id"],
            ["task_artifacts.id"],
            name=op.f("fk_brief_plans_derived_from_artifact_id_brief_artifacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_brief_plans_phase_id_brief_phases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_plans")),
    )
    op.create_index(op.f("idx_plans_phase"), "task_plans", ["phase_id"])
    op.create_index(op.f("idx_plans_status"), "task_plans", ["status"])

    op.create_table(
        "confidence_deltas",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("new_confidence", sa.Numeric(3, 2), nullable=False),
        sa.Column("old_level", sa.Text(), nullable=True),
        sa.Column("new_level", sa.Text(), nullable=False),
        sa.Column("old_tier", sa.Integer(), nullable=True),
        sa.Column("new_tier", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assumption_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_by", sa.Text(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(reason = ANY (ARRAY['task_verify_pass'::text, 'task_verify_fail'::text, 'brief_verify_pass'::text, 'brief_verify_fail'::text, 'correction_retraction'::text, 're_extraction'::text, 'audit_confirmed'::text, 'audit_contradicted'::text]))",
            name=op.f("ck_confidence_deltas_delta_valid_reason"),
        ),
        sa.CheckConstraint(
            "(target_kind = ANY (ARRAY['entity'::text, 'relationship'::text]))",
            name=op.f("ck_confidence_deltas_delta_valid_target"),
        ),
        sa.ForeignKeyConstraint(
            ["assumption_id"],
            ["task_assumptions.id"],
            name=op.f("fk_confidence_deltas_assumption_id_brief_assumptions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_confidence_deltas_brief_id_briefs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_confidence_deltas_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["task_plans.id"],
            name=op.f("fk_confidence_deltas_plan_id_brief_plans"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_runs.id"],
            name=op.f("fk_confidence_deltas_research_run_id_research_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confidence_deltas")),
    )
    op.execute(
        "CREATE INDEX idx_deltas_brief ON confidence_deltas USING btree (task_id) WHERE (task_id IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_deltas_conv ON confidence_deltas USING btree (conversation_id) WHERE (conversation_id IS NOT NULL)"
    )
    op.create_index(op.f("idx_deltas_target"), "confidence_deltas", ["target_kind", "target_id"])

    op.create_table(
        "extraction_triggers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("clarification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "scope_repos",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("mode", sa.Text(), server_default=sa.text("'shallow'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(mode = ANY (ARRAY['shallow'::text, 'deep'::text]))",
            name=op.f("ck_extraction_triggers_extr_valid_mode"),
        ),
        sa.CheckConstraint(
            "(trigger_kind = ANY (ARRAY['auto_manifest_only'::text, 'ask_back_gap'::text, 'user_requested'::text, 'scheduled'::text]))",
            name=op.f("ck_extraction_triggers_extr_valid_trigger"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_extraction_triggers_brief_id_briefs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["clarification_id"],
            ["task_clarifications.id"],
            name=op.f("fk_extraction_triggers_clarification_id_brief_clarifications"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_task_id"],
            ["extraction_tasks.id"],
            name=op.f("fk_extraction_triggers_extraction_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_extraction_triggers_phase_id_brief_phases"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_triggers")),
    )
    op.execute(
        "CREATE INDEX idx_extr_brief ON extraction_triggers USING btree (task_id) WHERE (task_id IS NOT NULL)"
    )

    op.create_table(
        "task_assumption_uses",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assumption_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["task_artifacts.id"],
            name=op.f("fk_brief_assumption_uses_artifact_id_brief_artifacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assumption_id"],
            ["task_assumptions.id"],
            name=op.f("fk_brief_assumption_uses_assumption_id_brief_assumptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id", "assumption_id", name=op.f("pk_assumption_use")),
    )
    op.create_index(op.f("idx_uses_assumption"), "task_assumption_uses", ["assumption_id"])

    op.create_table(
        "task_verify_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("user_notes", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = ANY (ARRAY['pending'::text, 'pass'::text, 'fail'::text, 'skipped'::text]))",
            name=op.f("ck_brief_verify_items_verify_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_brief_verify_items_phase_id_brief_phases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["task_plans.id"],
            name=op.f("fk_brief_verify_items_plan_id_brief_plans"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brief_verify_items")),
    )
    op.create_index(op.f("idx_verify_phase"), "task_verify_items", ["phase_id"])

    op.create_table(
        "ask_backs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_kind", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("answered_from", sa.Text(), nullable=False),
        sa.Column("triggered_extraction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("research_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "research_cost",
            sa.Numeric(10, 4),
            server_default=sa.text("'0'::numeric"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("answer_summary", sa.Text(), nullable=True),
        sa.Column("answer_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(answered_from = ANY (ARRAY['graph_only'::text, 'research_pipeline'::text, 'extraction_then_research'::text, 'user'::text, 'budget_exceeded'::text]))",
            name=op.f("ck_ask_backs_askback_valid_source"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_ask_backs_brief_id_briefs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"],
            ["task_phases.id"],
            name=op.f("fk_ask_backs_phase_id_brief_phases"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_extraction_id"],
            ["extraction_triggers.id"],
            name=op.f("fk_ask_backs_triggered_extraction_id_extraction_triggers"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ask_backs")),
    )
    op.create_index(op.f("idx_askbacks_brief"), "ask_backs", ["task_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_prevent_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only; UPDATE/DELETE blocked'
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update
        BEFORE UPDATE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION audit_log_prevent_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_delete
        BEFORE DELETE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION audit_log_prevent_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_prevent_mutation()")
    op.drop_table("ask_backs")
    op.drop_table("task_verify_items")
    op.drop_table("task_assumption_uses")
    op.drop_table("extraction_triggers")
    op.drop_table("confidence_deltas")
    op.drop_table("task_plans")
    op.drop_table("task_clarifications")
    op.drop_table("task_assumptions")
    op.drop_table("task_artifacts")
    op.drop_table("entity_relationships")
    op.drop_table("agent_judgements")
    op.drop_table("task_phases")
    op.drop_table("task_jira_audit")
    op.drop_table("task_events")
    op.drop_table("session_task_links")
    op.drop_table("entities")
    op.drop_table("conversation_task_links")
    op.drop_table("benchmark_runs")
    op.drop_table("agent_results")
    op.drop_table("tasks")
    op.drop_table("task_consultation_log")
    op.drop_table("research_runs")
    op.drop_table("repo_ownership")
    op.drop_table("repo_freshness")
    op.drop_table("org_repos")
    op.drop_table("llm_cost_log")
    op.drop_table("knowledge_text_cache")
    op.drop_table("jira_issues")
    op.drop_table("gdoc_pages")
    op.drop_table("external_sync_log")
    op.drop_table("entity_communities")
    op.drop_table("conversation_research_state")
    op.drop_table("conversation_messages")
    op.drop_table("confluence_pages")
    op.drop_table("repos")
    op.drop_table("handover_section_feedback")
    op.drop_table("conversations")
    op.drop_table("collab_messages")
    op.drop_table("collab_doubts")
    op.drop_table("collab_artifacts")
    op.drop_table("agent_session_events")
    op.drop_table("handover_docs")
    op.drop_table("handover_body_history")
    op.drop_table("handover_audits")
    op.drop_table("global_rules")
    op.drop_table("extraction_tasks")
    op.drop_table("domains")
    op.drop_table("collaboration_sessions")
    op.drop_table("collab_asks")
    op.drop_table("benchmark_questions")
    op.drop_table("audit_log")
    op.drop_table("agent_configs")
    op.execute("DROP EXTENSION IF EXISTS vector")
