"""Tests for the offline ``gdocs_index`` crawler.

Exercises the Drive cache-population path WITHOUT spawning real
``claude -p`` subprocesses — ``index_gdocs_for_org`` accepts an
``indexer`` callable so tests can inject a stub that returns whatever
shape we want.

Coverage:

* upsert path is idempotent (run twice → same row count, no dupes,
  field updates land on second run)
* 3-or-more docs sharing a parent folder → ownership row gets the
  inferred folder URL (under-threshold cases do not)
* repos with no entities are skipped (``IndexStats.skipped_empty++``)
* the 2-year recency cutoff filters out stale docs
* ExternalSyncLog entry is recorded for each processed repo
"""

from __future__ import annotations

import os
import uuid as _uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bifrost.research.gdocs_index import (
    DEFAULT_FOLDER_INFERENCE_THRESHOLD,
    IndexStats,
    _infer_shared_folder,
    _RepoIndexResult,
    index_gdocs_for_org,
)
from bifrost.storage.models import (
    Base,
    Entity,
    ExternalSyncLog,
    GDocPage,
    Repo,
    RepoOwnership,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost_test",
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker]:
    """Per-test engine + factory. Mirrors the pattern from
    ``test_idempotency.py`` so connections bind to the test's loop.

    NB: the conftest session-scoped engine has already
    ``create_all``-d the schema. If a previous run created
    ``external_sync_log`` BEFORE this PR added the
    ``uq_sync_repo_source`` constraint to the ORM model, the existing
    table won't have the constraint — so ``ON CONFLICT`` would 409.
    To make the fixture self-healing we explicitly drop+recreate the
    cache tables here.
    """
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # Drop tables we own + dependencies so create_all re-applies
        # any constraint changes the model declares. CASCADE because
        # external_sync_log + gdoc_pages have FKs to repos.
        await conn.execute(text("DROP TABLE IF EXISTS external_sync_log CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS gdoc_pages CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS repo_ownership CASCADE"))
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)
    yield sf
    # Clean rows the test wrote so a re-run starts clean.
    async with eng.begin() as conn:
        for tbl in (
            "gdoc_pages",
            "external_sync_log",
            "entities",
            "repo_ownership",
            "repos",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    await eng.dispose()


@pytest.fixture
def fake_settings() -> Any:
    class _Settings:
        agent_model = "claude-sonnet-4-6"

    return _Settings()


# ── Helpers ──────────────────────────────────────────────────────────


async def _make_repo(
    factory: async_sessionmaker,
    *,
    name: str,
    with_entity: bool = True,
) -> Repo:
    async with factory() as session:
        rid = _uuid.uuid4()
        repo = Repo(
            id=rid,
            urn=f"bifrost://repo/{name}",
            name=name,
            url=f"https://github.com/example/{name}",
        )
        session.add(repo)
        await session.flush()

        if with_entity:
            session.add(
                Entity(
                    id=_uuid.uuid4(),
                    entity_type="component",
                    urn=f"bifrost://repo/{name}/component/Foo",
                    name="Foo",
                    scope_repo_id=rid,
                    knowledge_tier=1,
                    confidence=0.8,
                    confidence_level="confirmed",
                    source_basis=[],
                    freshness_state="fresh",
                )
            )
        await session.commit()
        # Re-fetch to detach from session.
        repo_obj = (await session.execute(select(Repo).where(Repo.id == rid))).scalar_one()
        # eager-evaluate fields the runner reads
        _ = (repo_obj.id, repo_obj.name, repo_obj.urn)
        return repo_obj


def _doc(
    *,
    file_id: str,
    title: str = "Design Doc",
    parent_folder_id: str | None = None,
    parent_folder_url: str | None = None,
    last_modified: str | None = None,
    body_excerpt: str = "",
    summary: str = "",
    mime_type: str = "application/vnd.google-apps.document",
) -> dict[str, Any]:
    # Default to a recent ISO timestamp with explicit Z (Drive's
    # canonical form). The crawler's ``_parse_iso`` normalizes to
    # naive UTC, matching the ``last_modified`` column shape.
    out: dict[str, Any] = {
        "file_id": file_id,
        "title": title,
        "url": f"https://docs.google.com/document/d/{file_id}",
        "mime_type": mime_type,
        "owner_email": "alice@example.com",
        "summary": summary,
        "body_excerpt": body_excerpt,
        "last_modified": last_modified or (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "labels": [],
    }
    if parent_folder_id is not None:
        out["parent_folder_id"] = parent_folder_id
    if parent_folder_url is not None:
        out["parent_folder_url"] = parent_folder_url
    return out


def _make_stub_indexer(docs_by_repo: dict[str, list[dict]]):
    """Return an ``indexer`` callable matching ``_index_one_repo``'s
    signature. Looks up ``docs_by_repo[repo.name]`` and runs the same
    filter+inference logic the real indexer does on parsed JSON."""

    async def _stub(*, repo, settings, cutoff, max_docs, folder_threshold):
        raw = list(docs_by_repo.get(repo.name, []))[:max_docs]
        # Mirror the real filter (mimetype + recency).
        filtered: list[dict] = []
        for d in raw:
            if (
                d.get("mime_type", "application/vnd.google-apps.document")
                != "application/vnd.google-apps.document"
            ):
                continue
            from bifrost.research.gdocs_index import _parse_iso

            lm = _parse_iso(str(d.get("last_modified") or ""))
            if lm is not None and lm < cutoff:
                continue
            filtered.append(d)
        inferred = _infer_shared_folder(filtered, threshold=folder_threshold)
        return _RepoIndexResult(
            repo_id=repo.id,
            repo_name=repo.name,
            docs=filtered,
            inferred_folder_url=inferred,
        )

    return _stub


# ── Pure-function unit tests ─────────────────────────────────────────


def test_infer_shared_folder_meets_threshold() -> None:
    docs = [
        {"file_id": "a", "parent_folder_id": "F1"},
        {"file_id": "b", "parent_folder_id": "F1"},
        {"file_id": "c", "parent_folder_id": "F1"},
        {"file_id": "d", "parent_folder_id": "F2"},
    ]
    folder = _infer_shared_folder(docs, threshold=3)
    assert folder == "https://drive.google.com/drive/folders/F1"


def test_infer_shared_folder_below_threshold_returns_none() -> None:
    docs = [
        {"file_id": "a", "parent_folder_id": "F1"},
        {"file_id": "b", "parent_folder_id": "F1"},
        {"file_id": "c", "parent_folder_id": "F2"},
    ]
    assert _infer_shared_folder(docs, threshold=3) is None


def test_infer_shared_folder_prefers_provided_url() -> None:
    docs = [
        {
            "file_id": "a",
            "parent_folder_id": "F1",
            "parent_folder_url": "https://drive.google.com/drive/u/0/folders/F1?x=1",
        },
        {"file_id": "b", "parent_folder_id": "F1"},
        {"file_id": "c", "parent_folder_id": "F1"},
    ]
    folder = _infer_shared_folder(docs, threshold=3)
    assert folder == "https://drive.google.com/drive/u/0/folders/F1?x=1"


def test_infer_shared_folder_empty_docs() -> None:
    assert _infer_shared_folder([], threshold=3) is None


# ── Integration tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_is_idempotent(factory, fake_settings) -> None:
    repo = await _make_repo(factory, name="orders")
    docs = [
        _doc(file_id="f1", title="Orders Design"),
        _doc(file_id="f2", title="Orders RFC"),
    ]
    stub = _make_stub_indexer({"orders": docs})

    s1 = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=stub,
    )
    assert s1.repos_processed == 1
    assert s1.docs_indexed == 2

    # Second run with the same payload — must not duplicate rows.
    s2 = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=stub,
    )
    assert s2.docs_indexed == 2

    async with factory() as session:
        rows = (
            (await session.execute(select(GDocPage).where(GDocPage.repo_id == repo.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        ids = sorted(r.file_id for r in rows)
        assert ids == ["f1", "f2"]


@pytest.mark.asyncio
async def test_upsert_updates_changed_fields(factory, fake_settings) -> None:
    """Second pass with a changed title must overwrite the cached row."""
    await _make_repo(factory, name="orders")
    first = [_doc(file_id="f1", title="Original Title", summary="v1")]
    second = [_doc(file_id="f1", title="Updated Title", summary="v2")]

    await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"orders": first}),
    )
    await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"orders": second}),
    )

    async with factory() as session:
        row = (await session.execute(select(GDocPage).where(GDocPage.file_id == "f1"))).scalar_one()
        assert row.title == "Updated Title"
        assert row.summary == "v2"


@pytest.mark.asyncio
async def test_folder_inference_promotes_ownership(
    factory,
    fake_settings,
) -> None:
    """3+ docs in the same folder → repo_ownership.gdoc_folder_url set."""
    repo = await _make_repo(factory, name="checkout")
    docs = [
        _doc(file_id="a", parent_folder_id="FOLDER123"),
        _doc(file_id="b", parent_folder_id="FOLDER123"),
        _doc(file_id="c", parent_folder_id="FOLDER123"),
        _doc(file_id="d", parent_folder_id="OTHERFOLDER"),
    ]
    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"checkout": docs}),
        folder_inference_threshold=DEFAULT_FOLDER_INFERENCE_THRESHOLD,
    )
    assert stats.folders_inferred == 1

    async with factory() as session:
        own = (
            await session.execute(select(RepoOwnership).where(RepoOwnership.repo_id == repo.id))
        ).scalar_one()
        assert own.gdoc_folder_url == "https://drive.google.com/drive/folders/FOLDER123"


@pytest.mark.asyncio
async def test_folder_inference_below_threshold_no_ownership(
    factory,
    fake_settings,
) -> None:
    """Only 2 docs sharing a folder → no ownership update."""
    await _make_repo(factory, name="auth")
    docs = [
        _doc(file_id="a", parent_folder_id="F1"),
        _doc(file_id="b", parent_folder_id="F1"),
        _doc(file_id="c", parent_folder_id="F2"),
    ]
    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"auth": docs}),
        folder_inference_threshold=3,
    )
    assert stats.folders_inferred == 0

    async with factory() as session:
        own_row = (await session.execute(select(RepoOwnership))).scalar_one_or_none()
        # Either no ownership row OR the column is null (the indexer
        # only creates the row when it has a folder to put on it).
        assert own_row is None or own_row.gdoc_folder_url is None


@pytest.mark.asyncio
async def test_folder_inference_does_not_clobber_existing(
    factory,
    fake_settings,
) -> None:
    """Ownership-manifest ownership wins — crawler shouldn't overwrite a folder
    URL that's already set."""
    repo = await _make_repo(factory, name="inventory")
    async with factory() as session:
        session.add(
            RepoOwnership(
                repo_id=repo.id,
                gdoc_folder_url="https://drive.google.com/drive/folders/MANIFEST_FOLDER",
                source="catalog-info.yaml",
            )
        )
        await session.commit()

    docs = [
        _doc(file_id="a", parent_folder_id="CRAWLER_FOLDER"),
        _doc(file_id="b", parent_folder_id="CRAWLER_FOLDER"),
        _doc(file_id="c", parent_folder_id="CRAWLER_FOLDER"),
    ]
    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"inventory": docs}),
    )
    assert stats.folders_inferred == 0  # row existed + had a value

    async with factory() as session:
        own = (
            await session.execute(select(RepoOwnership).where(RepoOwnership.repo_id == repo.id))
        ).scalar_one()
        assert own.gdoc_folder_url == "https://drive.google.com/drive/folders/MANIFEST_FOLDER"


@pytest.mark.asyncio
async def test_skips_repos_with_no_entities(factory, fake_settings) -> None:
    """A repo with zero entities is skipped (no Drive call, no rows)."""
    await _make_repo(factory, name="empty-repo", with_entity=False)
    await _make_repo(factory, name="full-repo", with_entity=True)

    docs_by_repo = {
        "empty-repo": [_doc(file_id="x")],
        "full-repo": [_doc(file_id="y")],
    }
    calls: list[str] = []

    async def _spy_stub(*, repo, settings, cutoff, max_docs, folder_threshold):
        calls.append(repo.name)
        return _RepoIndexResult(
            repo_id=repo.id,
            repo_name=repo.name,
            docs=docs_by_repo.get(repo.name, []),
        )

    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_spy_stub,
    )
    assert calls == ["full-repo"]  # empty-repo never invoked the indexer
    assert stats.repos_processed == 1
    assert stats.skipped_empty == 1
    assert stats.docs_indexed == 1


@pytest.mark.asyncio
async def test_recency_cutoff_filters_stale_docs(factory, fake_settings) -> None:
    """Docs older than the recency cutoff are dropped."""
    await _make_repo(factory, name="stale-repo")
    cutoff_days = 730  # 2 years
    fresh = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    stale = (datetime.now(UTC) - timedelta(days=cutoff_days + 60)).isoformat()

    docs = [
        _doc(file_id="fresh-doc", last_modified=fresh),
        _doc(file_id="stale-doc", last_modified=stale),
    ]
    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"stale-repo": docs}),
        recency_cutoff_days=cutoff_days,
    )
    assert stats.docs_indexed == 1

    async with factory() as session:
        rows = (await session.execute(select(GDocPage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].file_id == "fresh-doc"


@pytest.mark.asyncio
async def test_max_repos_caps_processing(factory, fake_settings) -> None:
    """``max_repos=N`` should bound the work to the first N repos."""
    for n in ("a-repo", "b-repo", "c-repo"):
        await _make_repo(factory, name=n)

    seen: list[str] = []

    async def _spy(*, repo, settings, cutoff, max_docs, folder_threshold):
        seen.append(repo.name)
        return _RepoIndexResult(
            repo_id=repo.id,
            repo_name=repo.name,
            docs=[],
        )

    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_spy,
        max_repos=2,
    )
    assert len(seen) == 2
    assert stats.repos_processed == 2


@pytest.mark.asyncio
async def test_records_external_sync_log(factory, fake_settings) -> None:
    """Each processed repo gets a `gdocs` ExternalSyncLog entry."""
    repo = await _make_repo(factory, name="audit")
    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_make_stub_indexer({"audit": [_doc(file_id="x")]}),
    )
    assert stats.docs_indexed == 1

    async with factory() as session:
        row = (
            await session.execute(
                select(ExternalSyncLog)
                .where(ExternalSyncLog.repo_id == repo.id)
                .where(ExternalSyncLog.source == "gdocs")
            )
        ).scalar_one()
        assert row.items_indexed == 1
        assert row.error is None


@pytest.mark.asyncio
async def test_indexer_error_recorded_not_raised(factory, fake_settings) -> None:
    """If the per-repo runner returns an error, the orchestration keeps
    going for other repos and surfaces the error in IndexStats."""
    await _make_repo(factory, name="broken")
    await _make_repo(factory, name="happy")

    async def _maybe_fail(*, repo, settings, cutoff, max_docs, folder_threshold):
        if repo.name == "broken":
            return _RepoIndexResult(
                repo_id=repo.id,
                repo_name=repo.name,
                error="MCP timeout",
            )
        return _RepoIndexResult(
            repo_id=repo.id,
            repo_name=repo.name,
            docs=[_doc(file_id="happy-1")],
        )

    stats = await index_gdocs_for_org(
        session_factory=factory,
        settings=fake_settings,
        indexer=_maybe_fail,
    )
    assert stats.repos_processed == 2
    assert stats.docs_indexed == 1
    assert any("broken" in e for e in stats.errors)


# ── IndexStats ──────────────────────────────────────────────────────


def test_index_stats_to_dict() -> None:
    s = IndexStats(
        repos_processed=3,
        docs_indexed=10,
        folders_inferred=1,
        skipped_empty=2,
        errors=["x: oops"],
    )
    d = s.to_dict()
    assert d == {
        "repos_processed": 3,
        "docs_indexed": 10,
        "folders_inferred": 1,
        "skipped_empty": 2,
        "errors": ["x: oops"],
    }
