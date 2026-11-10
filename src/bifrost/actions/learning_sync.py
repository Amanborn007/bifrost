"""Portable learning-weights export/import for cross-instance sync.

The problem
-----------
Each Bifrost instance accumulates its own learned confidence — the
``confidence_deltas`` audit trail and the resulting per-target
confidence values. Operators running separate instances want to *share*
that learning: an instance that has confirmed "service X owns table Y"
many times should be able to hand that signal to a fresh instance.

What a "weights bundle" is
--------------------------
The learned weights are the confidence state per knowledge target. We
export them keyed by the **URN** (entities) or the **(source_urn,
rel_type, target_urn)** triple (relationships), because local row UUIDs
differ between instances — only the URN is stable across them. Each
entry carries:

* the *current* learned confidence on the source instance,
* how many distinct confirmations backed it (so the importer can weigh
  it), and
* provenance (which reasons drove it).

Plus bundle-level metadata: a schema version, the source instance id,
and an ``exported_at`` timestamp.

Why import is safe (NOT a destructive replace)
-----------------------------------------------
Import does **not** overwrite local ``entities.confidence``. Instead
each bundle entry is fed through the SAME governance path the rest of
the system uses — :func:`bifrost.actions.learning.record_delta`. That
means every imported weight is subject to the existing invariants:

* **Noise floor** — an imported value within ±0.03 of the local value
  is recorded for audit but never moves the target.
* **Upgrade needs 2 independent confirmations** — an imported weight is
  recorded with NO local origin (no ``task_id``, no ``conversation_id``),
  so a peer's signal alone can never clear the upgrade gate: it queues as
  ``pending_upgrade`` and only promotes once a *local* confirmation (a
  real task or conversation origin) agrees. One peer can therefore never
  unilaterally ratchet a local entity up — it can only stage evidence.
* **Downgrades apply immediately** — a peer reporting *lower* confidence
  pulls the local value down at once, matching local downgrade
  governance (rotting data is worse than uncertain data).

Conflict handling (same target, divergent confidence): the importer
emits a delta proposing the *peer's* confidence; ``record_delta`` then
decides — pending-upgrade (no origin → never auto-promotes), downgrade
(immediate), or noise — using the local value as the baseline.
Divergence is therefore resolved by the same rules that resolve any two
local signals, never by clobbering.

Idempotency: re-importing the same bundle is safe. A re-import of an
unchanged peer value is either noise (Δ<floor against the local value)
or another origin-less ``pending_upgrade`` that — lacking any distinct
task/conversation origin — cannot advance the upgrade gate. No new write
to the target results from re-importing. A peer reporting a *lower*
value applies once; a second identical import is then within the noise
floor of the already-lowered local value, so it no-ops.

Scope note: this module only reads/writes through ``record_delta`` and
direct SELECTs. It does NOT create a parallel knowledge store — the
belief-DAG / learning tables remain the single source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions import learning as _learning

logger = logging.getLogger(__name__)

# Bump when the bundle wire-format changes incompatibly.
BUNDLE_SCHEMA_VERSION = 1


@dataclass
class WeightEntry:
    """One portable learned weight, keyed by stable URN(s)."""

    target_kind: str  # "entity" | "relationship"
    confidence: float  # current learned confidence on source
    confirmations: int  # distinct confirming origins behind it
    reasons: list[str]  # distinct delta reasons (provenance)
    # Entity targets:
    urn: str | None = None
    # Relationship targets:
    source_urn: str | None = None
    rel_type: str | None = None
    target_urn: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "target_kind": self.target_kind,
            "confidence": self.confidence,
            "confirmations": self.confirmations,
            "reasons": self.reasons,
        }
        if self.target_kind == "entity":
            d["urn"] = self.urn
        else:
            d["source_urn"] = self.source_urn
            d["rel_type"] = self.rel_type
            d["target_urn"] = self.target_urn
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WeightEntry:
        return cls(
            target_kind=d["target_kind"],
            confidence=float(d["confidence"]),
            confirmations=int(d.get("confirmations", 0)),
            reasons=list(d.get("reasons", [])),
            urn=d.get("urn"),
            source_urn=d.get("source_urn"),
            rel_type=d.get("rel_type"),
            target_urn=d.get("target_urn"),
        )


@dataclass
class ImportReport:
    """Outcome of merging a bundle."""

    total: int = 0
    applied_up: int = 0
    applied_down: int = 0
    pending: int = 0
    noise: int = 0
    skipped_unresolved: int = 0  # URN not present locally
    skipped_malformed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "applied_up": self.applied_up,
            "applied_down": self.applied_down,
            "pending": self.pending,
            "noise": self.noise,
            "skipped_unresolved": self.skipped_unresolved,
            "skipped_malformed": self.skipped_malformed,
        }


# ── Export ───────────────────────────────────────────────────────────


async def export_learning_weights(
    session: AsyncSession,
    *,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Build a portable, versioned weights bundle from the local DB.

    Reads the *current* learned confidence per target plus the count of
    distinct confirming origins (tasks + conversations) and the distinct
    delta reasons. Keyed by stable URN(s), never local UUIDs.

    Only targets that have at least one ``confidence_deltas`` row are
    exported — i.e. things the instance has actually *learned* something
    about, not the entire untouched graph.
    """
    # Entities that have learning deltas, joined to their stable URN.
    ent_rows = (
        await session.execute(
            text(
                """
                SELECT
                    e.urn,
                    e.confidence,
                    COUNT(DISTINCT cd.task_id)
                        + COUNT(DISTINCT cd.conversation_id) AS confirmations,
                    ARRAY_AGG(DISTINCT cd.reason) AS reasons
                FROM confidence_deltas cd
                JOIN entities e ON e.id = cd.target_id
                WHERE cd.target_kind = 'entity'
                GROUP BY e.urn, e.confidence
                ORDER BY e.urn
                """
            )
        )
    ).all()

    # Relationships that have learning deltas, joined to the stable
    # (source_urn, rel_type, target_urn) triple.
    rel_rows = (
        await session.execute(
            text(
                """
                SELECT
                    se.urn AS source_urn,
                    r.rel_type,
                    te.urn AS target_urn,
                    r.confidence,
                    COUNT(DISTINCT cd.task_id)
                        + COUNT(DISTINCT cd.conversation_id) AS confirmations,
                    ARRAY_AGG(DISTINCT cd.reason) AS reasons
                FROM confidence_deltas cd
                JOIN entity_relationships r ON r.id = cd.target_id
                JOIN entities se ON se.id = r.source_id
                JOIN entities te ON te.id = r.target_id
                WHERE cd.target_kind = 'relationship'
                GROUP BY se.urn, r.rel_type, te.urn, r.confidence
                ORDER BY se.urn, r.rel_type, te.urn
                """
            )
        )
    ).all()

    weights: list[dict[str, Any]] = []
    for urn, conf, confirmations, reasons in ent_rows:
        weights.append(
            WeightEntry(
                target_kind="entity",
                urn=urn,
                confidence=float(conf),
                confirmations=int(confirmations or 0),
                reasons=sorted(r for r in (reasons or []) if r),
            ).to_dict()
        )
    for src_urn, rel_type, tgt_urn, conf, confirmations, reasons in rel_rows:
        weights.append(
            WeightEntry(
                target_kind="relationship",
                source_urn=src_urn,
                rel_type=rel_type,
                target_urn=tgt_urn,
                confidence=float(conf),
                confirmations=int(confirmations or 0),
                reasons=sorted(r for r in (reasons or []) if r),
            ).to_dict()
        )

    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_instance_id": instance_id or "unknown",
        "exported_at": datetime.now(UTC).isoformat(),
        "weight_count": len(weights),
        "weights": weights,
    }


# ── Import ───────────────────────────────────────────────────────────


async def _resolve_entity_id(session: AsyncSession, urn: str) -> str | None:
    row = (
        await session.execute(text("SELECT id FROM entities WHERE urn = :u"), {"u": urn})
    ).first()
    return str(row[0]) if row else None


async def _resolve_relationship_id(
    session: AsyncSession, source_urn: str, rel_type: str, target_urn: str
) -> str | None:
    row = (
        await session.execute(
            text(
                """
                SELECT r.id
                FROM entity_relationships r
                JOIN entities se ON se.id = r.source_id
                JOIN entities te ON te.id = r.target_id
                WHERE se.urn = :s AND r.rel_type = :rt AND te.urn = :t
                """
            ),
            {"s": source_urn, "rt": rel_type, "t": target_urn},
        )
    ).first()
    return str(row[0]) if row else None


async def import_learning_weights(
    session: AsyncSession,
    bundle: dict[str, Any],
    *,
    actor: str | None = None,
) -> ImportReport:
    """Merge a weights bundle through the existing governance path.

    Every entry becomes a :func:`learning.record_delta` call proposing
    the peer's confidence — so noise floor, the 2-confirmation upgrade
    gate, and immediate-downgrade all apply. Never overwrites local
    confidence directly. Idempotent: re-importing the same bundle does
    not advance any target (see module docstring).

    The caller's transaction is NOT committed here — the surrounding
    request/session owns commit/rollback, matching record_delta's
    contract (it row-locks the target FOR UPDATE until commit).
    """
    version = int(bundle.get("schema_version", 0))
    if version != BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported bundle schema_version {version} "
            f"(this build understands {BUNDLE_SCHEMA_VERSION})"
        )

    source_instance = str(bundle.get("source_instance_id") or "unknown")
    report = ImportReport()

    for raw in bundle.get("weights", []):
        report.total += 1
        try:
            entry = WeightEntry.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            report.skipped_malformed += 1
            continue

        if entry.target_kind == "entity":
            if not entry.urn:
                report.skipped_malformed += 1
                continue
            target_id = await _resolve_entity_id(session, entry.urn)
        elif entry.target_kind == "relationship":
            if not (entry.source_urn and entry.rel_type and entry.target_urn):
                report.skipped_malformed += 1
                continue
            target_id = await _resolve_relationship_id(
                session, entry.source_urn, entry.rel_type, entry.target_urn
            )
        else:
            report.skipped_malformed += 1
            continue

        if target_id is None:
            # The peer knows about a target this instance has never seen.
            # We do NOT fabricate entities — that would be a parallel
            # knowledge store. Skip; a future extraction may surface it.
            report.skipped_unresolved += 1
            continue

        # Decide direction by reason so the DB CHECK + downgrade
        # governance line up. record_delta itself recomputes direction
        # from the local baseline, but the reason must be coherent.
        read = await _learning._read_target(
            session,
            entry.target_kind,
            target_id,  # type: ignore[arg-type]
        )
        if read is None:
            report.skipped_unresolved += 1
            continue
        local_conf = read[0]
        proposed = Decimal(str(entry.confidence))
        reason = "audit_contradicted" if proposed < local_conf else "audit_confirmed"

        # NO local origin (task_id / conversation_id both None): a peer's
        # signal can never clear the 2-confirmation upgrade gate on its
        # own — it stages as pending_upgrade until a real local origin
        # agrees. Downgrades and noise floor still apply. This is what
        # keeps import non-destructive and idempotent.
        result = await _learning.record_delta(
            session,
            target_kind=entry.target_kind,  # type: ignore[arg-type]
            target_id=target_id,
            proposed_confidence=proposed,
            reason=reason,  # type: ignore[arg-type]
            actor=actor or f"sync:{source_instance}",
        )

        if result.direction == "noop":
            report.noise += 1
        elif result.applied_new_confidence is None:
            report.pending += 1
        elif result.direction == "down":
            report.applied_down += 1
        else:
            report.applied_up += 1

    return report
