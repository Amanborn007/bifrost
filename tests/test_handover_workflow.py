"""Integration tests for the team-collaboration workflow endpoints.

Covers the new V1 surface added to close the team-collaboration gap:

  * ``POST /handover-docs/{id}/section-feedback`` (submit)
  * ``GET  /handover-docs/{id}/section-feedback`` (list, default
    open-only; ``include_resolved=true`` for the full history)
  * ``POST /handover-docs/{id}/section-feedback/{fid}/resolve``
  * ``POST /handover-docs/{id}/restore-version``
  * ``GET  /handover-docs/inbox``
  * ``GET  /handover-docs/{id}/referencing-sessions``
  * ``GET  /handover-docs/{id}/section-metadata`` (smoke only — the
    existing metadata-merge logic is exercised via the inbox tests)

Real PostgreSQL via the shared ``db_session`` fixture; httpx ASGI
client mirrors the pattern in ``tests/test_api_routes.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api._cookie_signing import mint_signed_cookie, parse_signed_cookie
from bifrost.api.app import create_app
from bifrost.api.deps import get_repository
from bifrost.storage.models import (
    CollaborationSession,
    HandoverBodyHistory,
    HandoverDoc,
)
from bifrost.storage.repository import EntityRepository

# ── Shared fixtures ─────────────────────────────────────────────────


@pytest.fixture
def app(db_session: AsyncSession):
    """Create app with the shared test DB session injected."""
    application = create_app()

    async def _override_repo() -> AsyncGenerator[EntityRepository]:
        repo = EntityRepository(db_session)
        yield repo
        # conftest handles rollback per-test.

    application.dependency_overrides[get_repository] = _override_repo
    return application


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Requested-With": "bifrost-ui"},
    ) as ac:
        yield ac


@pytest.fixture
async def seed_handover_doc(
    db_session: AsyncSession,
    tmp_path: Path,
) -> HandoverDoc:
    """Seed one ``HandoverDoc`` with a real on-disk Markdown body so
    the restore-version test has a file to overwrite."""
    fs_path = tmp_path / "handover.md"
    fs_path.write_text("# Original body\n\nHello.\n", encoding="utf-8")
    doc = HandoverDoc(
        id=uuid.uuid4(),
        scope_kind="repo",
        scope_value="checkout-service",
        fs_path=str(fs_path),
        freshness_state="fresh",
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


@pytest.fixture
def signed_cookie() -> tuple[str, str]:
    """Return ``(cookie_value, session_id)`` for a freshly-minted
    signed cookie. Used so the test's ``actor`` is deterministic and
    the resolve / inbox flows can assert on it."""
    cookie = mint_signed_cookie()
    sid = parse_signed_cookie(cookie)
    assert sid is not None
    return cookie, sid


# ── Section feedback ────────────────────────────────────────────────


class TestSectionFeedback:
    async def test_submit_then_list_then_resolve(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
        signed_cookie: tuple[str, str],
    ) -> None:
        """Submit feedback → list (default open) shows it → resolve →
        list (default) hides it → list (include_resolved=true) shows
        it as resolved."""
        cookie_value, session_id = signed_cookie
        cookies = {"bifrost_session": cookie_value}

        # 1. Submit.
        submit_resp = await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
            json={
                "section_key": "architecture",
                "kind": "outdated",
                "note": "Mentions Redis but we moved to ElastiCache.",
            },
            cookies=cookies,
        )
        assert submit_resp.status_code == 201, submit_resp.text
        created = submit_resp.json()["data"]
        assert created["section_key"] == "architecture"
        assert created["kind"] == "outdated"
        assert created["note"].startswith("Mentions Redis")
        assert created["actor"] == session_id
        assert created["resolved_at"] is None
        assert created["resolved_by"] is None
        feedback_id = created["id"]

        # 2. List (open by default) — the row should be there.
        list_resp = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
        )
        assert list_resp.status_code == 200
        rows = list_resp.json()["data"]
        assert len(rows) == 1
        assert rows[0]["id"] == feedback_id
        assert rows[0]["resolved_at"] is None

        # 3. Resolve.
        resolve_resp = await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback/{feedback_id}/resolve",
            cookies=cookies,
        )
        assert resolve_resp.status_code == 200, resolve_resp.text
        resolved = resolve_resp.json()["data"]
        assert resolved["resolved_at"] is not None
        assert resolved["resolved_by"] == session_id

        # 4. Default list now empty (open-only).
        list_open = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
        )
        assert list_open.status_code == 200
        assert list_open.json()["data"] == []

        # 5. Full history surfaces the resolved row.
        list_all = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback?include_resolved=true",
        )
        assert list_all.status_code == 200
        all_rows = list_all.json()["data"]
        assert len(all_rows) == 1
        assert all_rows[0]["id"] == feedback_id
        assert all_rows[0]["resolved_at"] is not None

    async def test_submit_unknown_doc_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        bogus = uuid.uuid4()
        resp = await client.post(
            f"/api/v1/handover-docs/{bogus}/section-feedback",
            json={
                "section_key": "architecture",
                "kind": "incorrect",
                "note": None,
            },
        )
        assert resp.status_code == 404

    async def test_resolve_wrong_doc_returns_404(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """A feedback row attached to doc A must NOT be resolvable
        via the URL of doc B (ownership check)."""
        # Submit on doc A.
        submit_resp = await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
            json={"section_key": "architecture", "kind": "unclear"},
        )
        assert submit_resp.status_code == 201
        feedback_id = submit_resp.json()["data"]["id"]

        # Seed a SECOND handover doc for the cross-doc resolve attempt.
        other_path = tmp_path / "other.md"
        other_path.write_text("# Other\n", encoding="utf-8")
        other = HandoverDoc(
            id=uuid.uuid4(),
            scope_kind="repo",
            scope_value="other-service",
            fs_path=str(other_path),
            freshness_state="fresh",
        )
        db_session.add(other)
        await db_session.flush()

        resp = await client.post(
            f"/api/v1/handover-docs/{other.id}/section-feedback/{feedback_id}/resolve",
        )
        assert resp.status_code == 404


# ── Restore from body history ────────────────────────────────────────


class TestRestoreVersion:
    async def test_bogus_history_id_returns_404(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
    ) -> None:
        """A restore-version call with a history_id that doesn't
        exist (or doesn't belong to this doc) must 404 — not 500,
        not silent succeed."""
        bogus_history = uuid.uuid4()
        resp = await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/restore-version",
            json={"history_id": str(bogus_history)},
        )
        assert resp.status_code == 404

    async def test_restore_replaces_body_and_audits(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
        db_session: AsyncSession,
        signed_cookie: tuple[str, str],
    ) -> None:
        """Seed a snapshot, hit restore-version, and assert: (a) the
        on-disk body matches the snapshot, (b) a pre-restore snapshot
        of the prior body now exists, and (c) an audit row of type
        ``restored_version`` was emitted."""
        cookie_value, session_id = signed_cookie
        cookies = {"bifrost_session": cookie_value}

        # Seed a body-history snapshot directly via the ORM so we
        # don't depend on the regen path.
        snapshot_body = "# Older body\n\nThis is what we want back.\n"
        snapshot = HandoverBodyHistory(
            id=uuid.uuid4(),
            doc_id=seed_handover_doc.id,
            body_md=snapshot_body,
            event_type="pre-edit",
        )
        db_session.add(snapshot)
        await db_session.flush()

        # Confirm baseline: current on-disk body is the seeded one.
        fs_path = Path(seed_handover_doc.fs_path)  # type: ignore[arg-type]
        original_disk = fs_path.read_text(encoding="utf-8")
        assert "Original body" in original_disk

        resp = await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/restore-version",
            json={"history_id": str(snapshot.id)},
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        # The response body_md is the restored content.
        assert "Older body" in resp.json()["data"]["body_md"]
        # Disk reflects the restore.
        assert "Older body" in fs_path.read_text(encoding="utf-8")

        # A pre-restore snapshot of the prior body is now in history.
        history_resp = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/history",
        )
        assert history_resp.status_code == 200
        events = [entry["event_type"] for entry in history_resp.json()["data"]]
        assert "pre-restore" in events

        # An audit row was emitted with the right event type + actor.
        audit_resp = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/audit",
        )
        assert audit_resp.status_code == 200
        audit_rows = audit_resp.json()["data"]
        assert any(
            r["event_type"] == "restored_version" and r["actor"] == session_id for r in audit_rows
        )


# ── Inbox ────────────────────────────────────────────────────────────


class TestInbox:
    async def test_anon_returns_empty(self, client: AsyncClient) -> None:
        """No cookie → no actor → empty list. The route must NOT
        500, must NOT enumerate every doc."""
        resp = await client.get("/api/v1/handover-docs/inbox")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    async def test_inbox_returns_correct_counts(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
        db_session: AsyncSession,
        signed_cookie: tuple[str, str],
    ) -> None:
        """The inbox surfaces a doc the actor has touched, and the
        per-row counters reflect the seeded fixtures (one open
        feedback, one referencing session)."""
        cookie_value, session_id = signed_cookie
        cookies = {"bifrost_session": cookie_value}

        # 1. Touch the doc by submitting feedback as our actor — that
        # also writes a feedback row, but feedback alone doesn't
        # produce an audit signal. So we manually emit an audit row
        # via the repo to seed the "touched by actor" relation.
        repo = EntityRepository(db_session)
        await repo.record_handover_audit(
            doc_id=seed_handover_doc.id,
            event_type="edited",
            actor=session_id,
            metadata={
                "scope_kind": seed_handover_doc.scope_kind,
                "scope_value": seed_handover_doc.scope_value,
            },
        )
        await db_session.flush()

        # 2. Add ONE open feedback row.
        await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
            json={"section_key": "architecture", "kind": "outdated"},
            cookies=cookies,
        )

        # 3. Add ONE collab session that matched this handover doc.
        sess = CollaborationSession(
            id=uuid.uuid4(),
            requester="alice",
            requirement_text="ship X",
            state="open",
            matched_doc_ids=[str(seed_handover_doc.id)],
        )
        db_session.add(sess)
        await db_session.flush()

        # 4. Hit the inbox.
        resp = await client.get(
            "/api/v1/handover-docs/inbox",
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == str(seed_handover_doc.id)
        assert row["scope_kind"] == "repo"
        assert row["scope_value"] == "checkout-service"
        assert row["open_feedback_count"] == 1
        assert row["referencing_sessions_count"] == 1
        # No doubts seeded → loopback returns 0 candidates.
        assert row["doubt_candidates_count"] == 0


# ── Referencing sessions ─────────────────────────────────────────────


class TestReferencingSessions:
    async def test_returns_sessions_matching_doc(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
        db_session: AsyncSession,
    ) -> None:
        """Two sessions — one matches, one doesn't. Endpoint must
        return only the matching one."""
        match = CollaborationSession(
            id=uuid.uuid4(),
            requester="bob",
            requirement_text="match me",
            state="open",
            matched_doc_ids=[str(seed_handover_doc.id)],
        )
        unmatch = CollaborationSession(
            id=uuid.uuid4(),
            requester="carol",
            requirement_text="unrelated",
            state="open",
            matched_doc_ids=[str(uuid.uuid4())],
        )
        db_session.add_all([match, unmatch])
        await db_session.flush()

        resp = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/referencing-sessions",
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]
        ids = [r["id"] for r in rows]
        assert str(match.id) in ids
        assert str(unmatch.id) not in ids
        # Shape: each row carries the small subset.
        row = next(r for r in rows if r["id"] == str(match.id))
        assert row["requester"] == "bob"
        assert row["requirement_text"] == "match me"
        assert row["state"] == "open"
        assert row["doubt_count_open"] == 0

    async def test_unknown_doc_returns_empty_list(
        self,
        client: AsyncClient,
    ) -> None:
        bogus = uuid.uuid4()
        resp = await client.get(
            f"/api/v1/handover-docs/{bogus}/referencing-sessions",
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == []


# ── Section metadata (smoke) ─────────────────────────────────────────


class TestSectionMetadata:
    async def test_metadata_aggregates_feedback(
        self,
        client: AsyncClient,
        seed_handover_doc: HandoverDoc,
    ) -> None:
        """Adding two feedback rows (one open, one resolved) on the
        same section_key must surface as one map entry with
        feedback_open_count=1."""
        # First feedback (will be resolved).
        first = await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
            json={"section_key": "ownership", "kind": "missing_info"},
        )
        first_id = first.json()["data"]["id"]
        await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback/{first_id}/resolve",
        )

        # Second feedback (stays open).
        await client.post(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-feedback",
            json={"section_key": "ownership", "kind": "outdated"},
        )

        resp = await client.get(
            f"/api/v1/handover-docs/{seed_handover_doc.id}/section-metadata",
        )
        assert resp.status_code == 200, resp.text
        meta = resp.json()["data"]
        assert "ownership" in meta
        assert meta["ownership"]["feedback_open_count"] == 1
        # last_feedback_at is set; last_regenerated_at is None
        # (no audit row → no regen signal).
        assert meta["ownership"]["last_feedback_at"] is not None
        assert meta["ownership"]["last_regenerated_at"] is None
