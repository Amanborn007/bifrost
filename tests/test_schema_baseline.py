"""Initial schema.

Runs ``alembic upgrade head --sql`` (offline, no database) and inspects the DDL it emits.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests import leak_terms

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"

TABLES = {
    "agent_configs",
    "agent_judgements",
    "agent_results",
    "agent_session_events",
    "ask_backs",
    "audit_log",
    "benchmark_questions",
    "benchmark_runs",
    "collab_artifacts",
    "collab_asks",
    "collab_doubts",
    "collab_messages",
    "collaboration_sessions",
    "confidence_deltas",
    "confluence_pages",
    "conversation_messages",
    "conversation_research_state",
    "conversation_task_links",
    "conversations",
    "domains",
    "entities",
    "entity_communities",
    "entity_relationships",
    "external_sync_log",
    "extraction_tasks",
    "extraction_triggers",
    "gdoc_pages",
    "global_rules",
    "handover_audits",
    "handover_body_history",
    "handover_docs",
    "handover_section_feedback",
    "jira_issues",
    "knowledge_text_cache",
    "llm_cost_log",
    "org_repos",
    "repo_freshness",
    "repo_ownership",
    "repos",
    "research_runs",
    "session_task_links",
    "task_artifacts",
    "task_assumption_uses",
    "task_assumptions",
    "task_clarifications",
    "task_consultation_log",
    "task_events",
    "task_jira_audit",
    "task_phases",
    "task_plans",
    "task_verify_items",
    "tasks",
}


@pytest.fixture(scope="module")
def baseline_sql() -> str:
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/offline_only",
    }
    env.pop("BIFROST_DATABASE_URL", None)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"alembic upgrade head --sql failed:\n{proc.stderr[-2000:]}"
    return proc.stdout


def _table_ddl(sql: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\)", sql, re.DOTALL)
    assert match, f"no CREATE TABLE {table} in the emitted DDL"
    return match.group(1)


def test_exactly_one_migration_file() -> None:
    files = sorted(p.name for p in VERSIONS.glob("*.py"))
    assert files == ["001_baseline.py"]


def test_single_head_is_revision_001_with_no_parent() -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["001"]
    assert script.get_revision("001").down_revision is None


def test_ddl_has_no_banned_names(baseline_sql: str) -> None:
    assert [ln for ln in baseline_sql.splitlines() if leak_terms.matches(ln)] == []


def test_table_set_is_pinned(baseline_sql: str) -> None:
    created = set(re.findall(r"CREATE TABLE (\w+) \(", baseline_sql))
    created.discard("alembic_version")
    assert created == TABLES
    assert len(TABLES) == 52


def test_repo_ownership_columns(baseline_sql: str) -> None:
    ddl = _table_ddl(baseline_sql, "repo_ownership")
    assert re.search(r"raw_manifest JSONB DEFAULT '\{\}'::jsonb NOT NULL", ddl)
    assert re.search(r"source TEXT DEFAULT 'manifest' NOT NULL", ddl)


def test_org_repos_manifest_columns(baseline_sql: str) -> None:
    ddl = _table_ddl(baseline_sql, "org_repos")
    assert "manifest_fetched_at" in ddl
    assert "manifest_error" in ddl


def test_check_constraints_use_neutral_values(baseline_sql: str) -> None:
    assert "'team'" in _table_ddl(baseline_sql, "handover_docs")
    assert "'auto_manifest_only'" in _table_ddl(baseline_sql, "extraction_triggers")
    assert "'manifest'" in _table_ddl(baseline_sql, "task_assumptions")
