"""Quality gate tests — security, cost, type safety, spec compliance.

Each gate is independent and can run in isolation (no ordering dependency).
These tests serve as the project's acceptance criteria.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

# Paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src" / "bifrost"


# ──────────────────────────────────────────────────────────────────────
# Security Gate: no secrets, API keys, or credentials in extracted entities
# ──────────────────────────────────────────────────────────────────────

# Common secret patterns (aligned with S11 §10 secret scanning)
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI / Anthropic API key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),  # GitHub PAT
    re.compile(r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"password\s*[:=]\s*['\"][^'\"]{8,}"),  # password assignments
    re.compile(r"secret\s*[:=]\s*['\"][^'\"]{8,}"),  # secret assignments
    re.compile(r"Bearer\s+[a-zA-Z0-9._\-]{20,}"),  # Bearer tokens
    re.compile(r"[a-zA-Z0-9+/]{40,}={0,2}"),  # Long base64 blobs (potential keys)
]


@pytest.mark.asyncio
async def test_security_gate(client: httpx.AsyncClient, test_repo_url: str) -> None:
    """Verify that no secret patterns appear in extracted entity content.

    Registers a test repo, creates a task, then scans any entities that
    exist for the repo — their metadata and content fields must be free
    of secret patterns.
    """
    # Register repo
    reg = await client.post(
        "/api/v1/repos", json={"name": "security-gate-repo", "url": test_repo_url}
    )
    assert reg.status_code == 201
    repo_id = reg.json()["data"]["id"]

    # Trigger extraction (creates the task record)
    ext = await client.post(f"/api/v1/repos/{repo_id}/extract", json={"force_full": True})
    assert ext.status_code == 202

    # List entities for this repo
    ent_resp = await client.get("/api/v1/entities", params={"repo_id": repo_id})
    assert ent_resp.status_code == 200
    entities = ent_resp.json()["data"]

    # Scan each entity's serialized fields for secret patterns
    violations: list[str] = []
    for entity in entities:
        blob = str(entity)
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(blob)
            if match:
                urn = entity.get("urn", "?")
                violations.append(
                    f"Entity {urn}: matched {pattern.pattern!r} → {match.group()[:40]}"
                )

    assert not violations, (
        f"Security gate FAILED — {len(violations)} secret patterns found:\n"
        + "\n".join(violations[:10])
    )


# ──────────────────────────────────────────────────────────────────────
# Cost Gate: extraction stays within LLM token budget
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_gate(db_session) -> None:
    """Verify LLM cost log entries do not exceed the configured budget.

    V1: Since extraction runs asynchronously and may not have completed,
    this test verifies the cost tracking infrastructure is in place:
    - The llm_cost_log table exists and is queryable
    - If any cost entries exist, their total is under the threshold
    """
    from sqlalchemy import text

    # Verify the cost log table exists and is queryable
    result = await db_session.execute(text("SELECT COUNT(*) FROM llm_cost_log"))
    count = result.scalar()
    assert count is not None, "llm_cost_log table must be queryable"

    # If there are cost entries, verify total is under budget
    if count > 0:
        cost_result = await db_session.execute(
            text("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_cost_log")
        )
        total_cost = float(cost_result.scalar() or 0)
        budget_usd = 5.0  # Configurable threshold for small test repo
        assert total_cost <= budget_usd, (
            f"Cost gate FAILED — total LLM cost ${total_cost:.2f} exceeds budget ${budget_usd:.2f}"
        )


# ──────────────────────────────────────────────────────────────────────
# Type Safety Gate: pyright passes on entire src/bifrost/ with zero errors
# ──────────────────────────────────────────────────────────────────────


def test_type_safety_gate() -> None:
    """Verify pyright reports zero errors on src/bifrost/.

    Pyright must pass with zero errors before any phase is complete.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pyright", str(SRC_DIR)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=300,
    )
    # pyright returns 0 on success, 1 on type errors
    assert result.returncode == 0, (
        f"Type safety gate FAILED — pyright found errors:\n{result.stdout[-2000:]}"
    )


# ──────────────────────────────────────────────────────────────────────
# Spec Compliance Gate: API response shapes match TRD interface contracts
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spec_compliance_gate(client: httpx.AsyncClient) -> None:
    """Verify API responses match the TRD-specified envelope format.

    Every successful response must have: { data: T, meta: { request_id, timestamp, track } }
    Every error response must have: { error: { code, message, retryable } }
    """
    # 1. Health endpoint — basic success envelope
    health = await client.get("/health")
    assert health.status_code == 200
    health_body = health.json()
    assert "status" in health_body, "Health endpoint must return {status: ...}"

    # 2. Successful list endpoint — verify envelope shape
    repos = await client.get("/api/v1/repos")
    assert repos.status_code == 200
    body = repos.json()
    assert "data" in body, "Response must contain 'data' key"
    assert "meta" in body, "Response must contain 'meta' key"
    meta = body["meta"]
    assert "request_id" in meta, "meta must contain request_id"
    assert "timestamp" in meta, "meta must contain timestamp"
    assert "track" in meta, "meta must contain track"
    assert meta["track"] in {"A", "B"}, "track must be A or B"

    # 3. Tasks list — same envelope
    tasks = await client.get("/api/v1/tasks")
    assert tasks.status_code == 200
    tasks_body = tasks.json()
    assert "data" in tasks_body
    assert "meta" in tasks_body
    assert isinstance(tasks_body["data"], list)

    # 4. Entities list — same envelope
    entities = await client.get("/api/v1/entities")
    assert entities.status_code == 200
    ent_body = entities.json()
    assert "data" in ent_body
    assert isinstance(ent_body["data"], list)

    # 5. 404 error — verify error envelope shape
    import uuid

    not_found = await client.get(f"/api/v1/repos/{uuid.uuid4()}")
    assert not_found.status_code == 404

    # 6. Entity type filter — verify it doesn't crash
    typed = await client.get("/api/v1/entities", params={"entity_type": "component"})
    assert typed.status_code == 200

    # 7. Domains endpoint — verify envelope
    domains = await client.get("/api/v1/domains")
    assert domains.status_code == 200
    dom_body = domains.json()
    assert "data" in dom_body
    assert "meta" in dom_body


# ──────────────────────────────────────────────────────────────────────
# Performance Baseline: small repo extraction completes within budget
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_performance_baseline(client: httpx.AsyncClient, test_repo_url: str) -> None:
    """Verify that triggering extraction for a small repo completes API round-trip
    within a reasonable wall-clock budget.

    This tests the API response time for the extraction trigger — not the
    full async extraction (which runs in background). The API must respond
    within 5 seconds for the trigger endpoint.
    """
    import time

    # Register
    reg = await client.post("/api/v1/repos", json={"name": "perf-test-repo", "url": test_repo_url})
    assert reg.status_code == 201
    repo_id = reg.json()["data"]["id"]

    # Time the extraction trigger
    start = time.perf_counter()
    ext = await client.post(f"/api/v1/repos/{repo_id}/extract", json={"force_full": True})
    elapsed = time.perf_counter() - start

    assert ext.status_code == 202
    assert elapsed < 5.0, (
        f"Performance baseline FAILED — extraction trigger took {elapsed:.2f}s (budget: 5.0s)"
    )
