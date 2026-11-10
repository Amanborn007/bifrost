"""The benchmark domain list tells the UI which domains are sequenced fixtures."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.routes.admin import benchmarks_list_domains
from bifrost.storage.models import BenchmarkQuestion

TAG = uuid.uuid4().hex[:8]


async def test_domains_expose_sequenced_flag(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq, plain = f"seq-{TAG}", f"plain-{TAG}"
    for domain in (seq, plain):
        db_session.add(
            BenchmarkQuestion(domain=domain, question_key="q1", question_text="What owns X?")
        )
    await db_session.flush()
    monkeypatch.setattr("bifrost.api.routes.admin.is_sequenced", lambda d: d == seq)

    env = await benchmarks_list_domains(db_session)

    by_domain = {r["domain"]: r for r in env.data}
    assert by_domain[seq]["sequenced"] is True
    assert by_domain[plain]["sequenced"] is False
