"""Load ground-truth questions from ``docs/benchmarks/<domain>.yml``
and upsert into the ``benchmark_questions`` table.

Append-only — questions don't churn. Re-running ``ensure_loaded(domain)``
inserts new rows for new question_keys and updates the question_text /
expected_* fields for existing keys (so an operator can refine a
question without losing its history of scored runs).
"""

from __future__ import annotations

import logging
import uuid as _uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.storage.models import BenchmarkQuestion

logger = logging.getLogger(__name__)


# ── YAML schema ───────────────────────────────────────────────────────────────
#
# Each YAML file is one document with:
#   domain: <str>          — e.g. "acme"
#   questions:
#     - key: q1-broad-service-map
#       text: "What does the DLVR team own across services?"
#       paraphrase: "Tell me about DLVR team's surface area"   (optional)
#       expected_entities: [<urn>, ...]
#       expected_jira_refs: [<jira-key>, ...]                 (optional)
#       expected_pr_refs: [<url-or-#N>, ...]                  (optional)
#       expected_key_facts:
#         - {needle: "calls CarrierRateService", why: "core dispatch path"}
#       tier_floor: 1                                         (default 3)
#       expects_diagram: true                                 (default false)
#       scope_repo_names: ["ShipmentService", "CarrierRateService"] (optional)


REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARKS_DIR = REPO_ROOT / "docs" / "benchmarks"


def yaml_path_for(domain: str) -> Path:
    """Convention: ``docs/benchmarks/<domain>.yml``."""
    return BENCHMARKS_DIR / f"{domain}.yml"


def _load_yaml(domain: str) -> dict[str, Any]:
    path = yaml_path_for(domain)
    if not path.exists():
        raise FileNotFoundError(f"benchmark file not found: {path}")
    with path.open("r") as fh:
        data = yaml.safe_load(fh) or {}
    if data.get("domain") != domain:
        raise ValueError(f"domain mismatch: file says {data.get('domain')!r}, expected {domain!r}")
    qs = data.get("questions") or []
    if not isinstance(qs, list) or not qs:
        raise ValueError(f"{path}: 'questions' must be a non-empty list")
    return data


def is_sequenced(domain: str) -> bool:
    """Return True when ``docs/benchmarks/<domain>.yml`` declares
    ``sequenced: true``. Sequenced domains run all questions in a
    single conversation (preserving prior-turn context); non-sequenced
    domains run each question in a fresh conversation.
    """
    try:
        data = _load_yaml(domain)
    except (FileNotFoundError, ValueError):
        return False
    return bool(data.get("sequenced", False))


async def ensure_loaded(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    domain: str,
) -> dict[str, int]:
    """Upsert questions from ``docs/benchmarks/<domain>.yml`` into the DB.

    Returns a small report: ``{"inserted": N, "updated": M, "skipped": K}``.
    Skipped rows are those whose YAML body is byte-identical to the DB
    row — no-op so a benchmark replay doesn't churn ``updated_at``-style
    fields (none on this table today, but defensive).

    Idempotent: safe to call at every harness start.
    """
    data = _load_yaml(domain)
    inserted = updated = skipped = 0

    async with session_factory() as session:
        for q in data["questions"]:
            key = str(q["key"])
            existing = (
                await session.execute(
                    select(BenchmarkQuestion).where(
                        BenchmarkQuestion.domain == domain,
                        BenchmarkQuestion.question_key == key,
                    )
                )
            ).scalar_one_or_none()

            payload = {
                "domain": domain,
                "question_key": key,
                "question_text": q["text"],
                "paraphrase": q.get("paraphrase"),
                "expected_entities": list(q.get("expected_entities") or []),
                "expected_jira_refs": list(q.get("expected_jira_refs") or []),
                "expected_pr_refs": list(q.get("expected_pr_refs") or []),
                "expected_key_facts": list(q.get("expected_key_facts") or []),
                "tier_floor": int(q.get("tier_floor", 3)),
                "expects_diagram": bool(q.get("expects_diagram", False)),
                "expected_format": q.get("expected_format"),
                "max_words": q.get("max_words"),
                "scope_repo_names": list(q.get("scope_repo_names") or []),
                "question_metadata": dict(q.get("metadata") or {}),
            }

            if existing is None:
                session.add(BenchmarkQuestion(id=_uuid.uuid4(), **payload))
                inserted += 1
                continue

            # Compare every payload field to the existing row's value.
            mutated = False
            for k, v in payload.items():
                if k == "question_metadata":
                    cur = existing.question_metadata
                else:
                    cur = getattr(existing, k)
                if cur != v:
                    if k == "question_metadata":
                        existing.question_metadata = v
                    else:
                        setattr(existing, k, v)
                    mutated = True
            if mutated:
                updated += 1
            else:
                skipped += 1

        await session.commit()

    logger.info(
        "benchmark_loader: domain=%s inserted=%d updated=%d skipped=%d",
        domain,
        inserted,
        updated,
        skipped,
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
