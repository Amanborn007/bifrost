"""Small SQLAlchemy helpers shared across storage and research code."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Result


def affected_rows(result: Result[Any]) -> int:
    """Rows touched by an INSERT/UPDATE/DELETE; 0 when the driver reports none.

    ``AsyncSession.execute`` is typed as ``Result``, but DML returns a
    ``CursorResult`` which is the type that carries ``rowcount``.
    """
    return cast("CursorResult[Any]", result).rowcount or 0
