"""Service-graph collection must skip forks and archived repos from the org catalog."""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql

from bifrost.cli.config import BifrostSettings
from bifrost.manifest.graph_apply import collect_manifests
from bifrost.storage.models import OrgRepo


class _Result:
    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return []


class _Session:
    def __init__(self, seen: list[Any]) -> None:
        self._seen = seen

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        self._seen.append(stmt)
        return _Result()


async def test_catalog_query_excludes_forks_and_archived() -> None:
    seen: list[Any] = []
    await collect_manifests(
        lambda: _Session(seen),  # type: ignore[arg-type]
        include_catalog=True,
        settings=BifrostSettings(),
    )
    catalog = [s for s in seen if any(d.get("entity") is OrgRepo for d in s.column_descriptions)]
    assert len(catalog) == 1
    sql = str(catalog[0].compile(dialect=postgresql.dialect()))
    assert "org_repos.is_fork IS false" in sql
    assert "org_repos.archived IS false" in sql
