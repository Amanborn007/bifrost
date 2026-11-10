"""Tests for the retention helpers added to auto_archive.

The cache-purge and worktree-GC paths run as part of the hourly tick.
These tests exercise the worktree GC against a temp filesystem (no
DB) and confirm sweep_retention degrades gracefully when one of the
two pieces fails.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bifrost.actions.auto_archive import (
    RetentionResult,
    sweep_expired_caches,
    sweep_old_worktrees,
    sweep_retention,
)


class TestSweepExpiredCaches:
    """SQL DELETE issued for both cache tables; total rowcount returned."""

    @pytest.mark.asyncio
    async def test_returns_combined_rowcount(self) -> None:
        session = MagicMock()
        # Two DELETEs (one per table); 3 rows + 5 rows.
        result1 = MagicMock()
        result1.rowcount = 3
        result2 = MagicMock()
        result2.rowcount = 5
        session.execute = AsyncMock(side_effect=[result1, result2])
        session.commit = AsyncMock()

        total = await sweep_expired_caches(session)

        assert total == 8
        assert session.execute.await_count == 2
        # Each DELETE should reference expires_at < NOW().
        for call in session.execute.await_args_list:
            sql_text = str(call.args[0])
            assert "expires_at" in sql_text
            assert "DELETE FROM" in sql_text
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_rows_no_commit(self) -> None:
        """Avoid an unnecessary commit when nothing was deleted."""
        session = MagicMock()
        zero = MagicMock()
        zero.rowcount = 0
        session.execute = AsyncMock(side_effect=[zero, zero])
        session.commit = AsyncMock()

        total = await sweep_expired_caches(session)

        assert total == 0
        session.commit.assert_not_awaited()


class TestSweepOldWorktrees:
    """Worktrees on disk must be rm -rf'd; the tasks row's worktree_path
    cleared atomically in the same transaction."""

    @pytest.mark.asyncio
    async def test_removes_existing_worktree(
        self,
        tmp_path: Path,
    ) -> None:
        # Set up a fake worktree on disk
        worktree = tmp_path / "task-deadbeef"
        worktree.mkdir()
        (worktree / "scratch.py").write_text("# fake")
        (worktree / ".git").mkdir()

        session = MagicMock()
        # SELECT returns one row: (task_id, worktree_path)
        select_result = MagicMock()
        select_result.all = MagicMock(
            return_value=[("11111111-1111-1111-1111-111111111111", str(worktree))],
        )
        session.execute = AsyncMock(side_effect=[select_result, MagicMock()])
        session.commit = AsyncMock()

        removed = await sweep_old_worktrees(session)

        assert str(worktree) in removed
        # Worktree gone from disk
        assert not worktree.exists()
        # UPDATE tasks SET worktree_path = NULL was issued
        assert session.execute.await_count == 2
        update_call = session.execute.await_args_list[1]
        assert "worktree_path = NULL" in str(update_call.args[0])
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_already_missing_worktree(
        self,
        tmp_path: Path,
    ) -> None:
        """If the path is already gone (e.g. operator deleted it
        manually), still clear the column — but don't fail."""
        ghost = tmp_path / "already-gone"
        # NOT mkdir'd

        session = MagicMock()
        select_result = MagicMock()
        select_result.all = MagicMock(
            return_value=[("aaaa", str(ghost))],
        )
        session.execute = AsyncMock(side_effect=[select_result, MagicMock()])
        session.commit = AsyncMock()

        removed = await sweep_old_worktrees(session)

        # Nothing physically removed — but the row was cleared.
        assert removed == []
        # UPDATE was still issued because we have a cleared_id
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_no_rows_no_update(self) -> None:
        """Empty SELECT means no rows to clear and no UPDATE."""
        session = MagicMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=[])
        session.execute = AsyncMock(return_value=select_result)
        session.commit = AsyncMock()

        removed = await sweep_old_worktrees(session)

        assert removed == []
        # Only the initial SELECT, no UPDATE
        assert session.execute.await_count == 1
        session.commit.assert_not_awaited()


class TestSweepRetention:
    """The combined function must isolate failures so cache + worktree
    are independent."""

    @pytest.mark.asyncio
    async def test_returns_dataclass_with_both_counts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch both helpers so we can assert wiring without DB.
        async def _fake_cache(_session: object) -> int:
            return 4

        async def _fake_wt(
            _session: object,
            *,
            older_than_days: int = 7,
        ) -> list[str]:
            return ["/tmp/wt1"]

        monkeypatch.setattr(
            "bifrost.actions.auto_archive.sweep_expired_caches",
            _fake_cache,
        )
        monkeypatch.setattr(
            "bifrost.actions.auto_archive.sweep_old_worktrees",
            _fake_wt,
        )

        result = await sweep_retention(MagicMock())

        assert isinstance(result, RetentionResult)
        assert result.cache_rows == 4
        assert result.worktrees_removed == ["/tmp/wt1"]
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_cache_failure_does_not_block_worktree(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _broken_cache(_session: object) -> int:
            raise RuntimeError("DB unreachable")

        async def _fake_wt(
            _session: object,
            *,
            older_than_days: int = 7,
        ) -> list[str]:
            return ["/tmp/wt-survived"]

        monkeypatch.setattr(
            "bifrost.actions.auto_archive.sweep_expired_caches",
            _broken_cache,
        )
        monkeypatch.setattr(
            "bifrost.actions.auto_archive.sweep_old_worktrees",
            _fake_wt,
        )

        result = await sweep_retention(MagicMock())

        assert result.cache_rows == 0
        assert result.worktrees_removed == ["/tmp/wt-survived"]
        assert any("cache" in e for e in result.errors)
