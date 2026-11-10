"""Canonical confidence tier system for the Bifrost graph.

Zero-false-positive is a product-level goal: every claim Bifrost
makes must carry a provenance trail so the reader can judge (and
the LLM can weigh) whether to trust it. We enforce this at the
write layer — no graph row may be inserted without a
``source_basis`` and a confidence that matches the tier below.

## The five tiers, from highest to lowest truth

| Tier | Name              | source_type               | confidence_level | conf | Example                                                                   |
|------|-------------------|---------------------------|------------------|------|---------------------------------------------------------------------------|
| T1   | code-verified     | ``code_verified``         | ``confirmed``    | 0.95 | Phase G extracted "A calls B" from an import + call-site in source; or an audit-mode agent Grep confirmed the edge |
| T2   | config-declared   | ``manifest``, ``gradle``, ``package_json`` | ``confirmed``    | 0.88 | the manifest lists `FOO_HOST: https://foo.example.com` — the repo DECLARES this dep in a first-class file |
| T3   | metadata-inferred | ``metadata_match``        | ``inferred``     | 0.70 | Entity metadata has `internal_dependencies: [com.example.x.Y]`; matches another repo's entity by qualified-name |
| T4   | documented        | ``confluence``, ``jira``, ``github_issue`` | ``inferred``     | 0.55 | A Confluence runbook describes the flow; a Jira ticket mentions the call  |
| T5   | suggested         | ``fuzzy_match``, ``llm_guess`` | ``uncertain``    | 0.35 | Bare last-segment name match without a repo-hint; or an LLM best-guess    |

## Rules that follow

1. **Code wins.** When T1 conflicts with T4, the answer cites T1 and
   flags the docs as outdated. Never the other way around.

2. **No bare writes.** Every ``Entity`` or ``EntityRelationship``
   insert uses ``build_source_basis()`` to populate the JSONB
   ``source_basis`` list. Callers that skip this must have a strong
   reason and leave a `# TODO: add source` comment.

3. **Audit can upgrade tier.** When an audit-mode agent Grep-verifies
   a previously-inferred (T3) edge, the edge's confidence should be
   bumped to T1. (Downgrade works too: agent finds the claim false →
   ``confidence_level = "conflicting"``, conf = 0.1.)

4. **Stale = explicit.** Tier is independent of freshness. A T1 edge
   extracted 3 months ago but with remote moved forward is still
   T1 for provenance purposes, but `freshness_state=stale` flags the
   drift. Synthesis must surface both dimensions.

5. **Source_basis is a list.** Multiple sources corroborating the
   same fact strengthens trust. The aggregated confidence for an edge
   backed by {T2 manifest, T3 metadata} is higher than T2 alone —
   use ``combined_confidence()`` to compute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

# ── Tier definitions ──────────────────────────────────────────────


SourceType = Literal[
    "code_verified",  # T1 — directly observed in source
    "manifest",  # T2 — declared in a catalog/ownership manifest
    "gradle",  # T2 — declared in build.gradle
    "package_json",  # T2 — declared in package.json
    "metadata_match",  # T3 — matched via entity metadata
    "confluence",  # T4 — sourced from Confluence doc
    "jira",  # T4 — sourced from Jira ticket
    "github_issue",  # T4 — sourced from GitHub issue/PR
    "slack",  # T4 — sourced from Slack message
    "fuzzy_match",  # T5 — last-segment heuristic
    "llm_guess",  # T5 — synthesizer best-effort
]


ConfidenceLevel = Literal[
    "confirmed",  # tier 1-2
    "inferred",  # tier 3-4
    "uncertain",  # tier 5
    "conflicting",  # audit said the claim is false
    "stale",  # tier fine but freshness drifted
    "unknown",
]


# Single source of truth for tier → level + confidence.
# Callers must pass a source_type; we look up the rest.
_TIER_MAP: dict[str, tuple[ConfidenceLevel, float]] = {
    # T1
    "code_verified": ("confirmed", 0.95),
    # T2
    "manifest": ("confirmed", 0.88),
    "gradle": ("confirmed", 0.88),
    "package_json": ("confirmed", 0.88),
    # T3
    "metadata_match": ("inferred", 0.70),
    # T4
    "confluence": ("inferred", 0.55),
    "jira": ("inferred", 0.55),
    "github_issue": ("inferred", 0.55),
    "slack": ("inferred", 0.50),
    # T5
    "fuzzy_match": ("uncertain", 0.35),
    "llm_guess": ("uncertain", 0.30),
}


@dataclass
class SourceBasis:
    """One provenance entry. List of these goes into ``source_basis``."""

    source_type: SourceType
    reference: str  # URL / file:line / ticket key
    # Additional context the writer may supply:
    method: str | None = None  # "llm" | "regex" | "heuristic" | "declared"
    excerpt: str | None = None  # short quoted evidence
    matched_on: str | None = None  # what string actually matched
    tier: int | None = None  # derived, set by build_source_basis

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "reference": self.reference,
            "method": self.method,
            "excerpt": self.excerpt,
            "matched_on": self.matched_on,
            "tier": self.tier,
            "recorded_at": datetime.now(UTC).isoformat(),
        }


def tier_for(source_type: SourceType) -> int:
    """Map source_type to numeric tier (1-5)."""
    if source_type == "code_verified":
        return 1
    if source_type in ("manifest", "gradle", "package_json"):
        return 2
    if source_type == "metadata_match":
        return 3
    if source_type in ("confluence", "jira", "github_issue", "slack"):
        return 4
    return 5


def classify(
    source_type: SourceType,
) -> tuple[ConfidenceLevel, float]:
    """Return the canonical (confidence_level, confidence) for a source.

    Used as the default — a writer may override when it has stronger
    local signal (e.g., a manifest with a literal HTTPS HOST value +
    non-ambiguous name = 0.92 instead of 0.88), but the override
    should stay within the tier band (±0.05).
    """
    return _TIER_MAP[source_type]


def build_source_basis(
    source_type: SourceType,
    reference: str,
    *,
    method: str | None = None,
    excerpt: str | None = None,
    matched_on: str | None = None,
) -> list[dict[str, Any]]:
    """Construct a well-formed ``source_basis`` list for a single source.

    Returns a 1-element list (the JSONB column is list-shaped so
    future corroborating sources can be appended).
    """
    basis = SourceBasis(
        source_type=source_type,
        reference=reference,
        method=method,
        excerpt=excerpt,
        matched_on=matched_on,
        tier=tier_for(source_type),
    )
    return [basis.as_dict()]


def combined_confidence(sources: list[dict[str, Any]]) -> tuple[ConfidenceLevel, float]:
    """Aggregate confidence when multiple sources corroborate the same claim.

    Rule: pick the best tier as the floor, then add a small bump per
    corroborating lower-tier source (max +0.07 total). This rewards
    "the manifest says X AND code shows X" without overstating.
    """
    if not sources:
        return ("uncertain", 0.3)
    # Best tier = lowest tier number
    best = min(tier_for(s.get("source_type", "llm_guess")) for s in sources)
    best_source = next(
        (s for s in sources if tier_for(s.get("source_type", "llm_guess")) == best),
        sources[0],
    )
    level, base_conf = classify(best_source["source_type"])
    # Bump: +0.03 per additional corroborating source of tier ≥ best, capped at +0.07
    bump = min(0.07, 0.03 * max(0, len(sources) - 1))
    return (level, min(0.99, base_conf + bump))


# ── Policy text for LLM prompts ────────────────────────────────────


CODE_IS_TRUTH_POLICY = """\
# ─────────────────────────────────────────────────────────────────
# BIFROST CONFIDENCE + PROVENANCE POLICY (zero-false-positive)
# ─────────────────────────────────────────────────────────────────

Every claim in your answer MUST be judgable for trust by the reader.
Follow this hierarchy — it's the Bifrost product promise:

## Tier hierarchy (highest → lowest truth)

  T1 code-verified  — observed directly in source (Phase G extract or
                       audit-mode Grep). Cite this whenever you can.
  T2 config-declared — the manifest / gradle / package.json declares the
                       dependency. Strong second-best.
  T3 metadata-inferred — entity metadata has a matching qualified
                         name. Reasonable but NOT proven — phrase as
                         "appears to" / "likely" / "declared".
  T4 documented      — Confluence/Jira/Slack says so. SUPPORTING only.
                       Useful for intent/rationale but NOT for facts.
  T5 suggested       — fuzzy name match or LLM inference. Avoid unless
                       nothing else covers the question; always caveat.

## Rules

1. **Code is the tie-breaker.** When evidence tiers disagree (e.g.,
   Confluence says "FF calls OS" but code audit shows no HTTP client
   from FF to OS), cite the code verdict as fact and flag the doc as
   potentially outdated in "Cross-source alignment".

2. **Phrase claims to match their tier.**
   - T1: "FulfillmentService calls `SupplierClient.check_stock`
     at `src/…/stock_check.py:300` [^1]"
   - T2: "the manifest declares `STOCK_GATEWAY_HOST`, so FF depends on
     stock-gateway at runtime [^2]"
   - T3: "FF appears to reference `stock-gateway`'s
     `StockLookupHandler` based on metadata imports [^3]"
   - T4: "Per the Stock Check Runbook Confluence
     page, this call flows via the item checker [^4] — not confirmed in code this
     run"
   - T5: Avoid. If you must, say "likely" and caveat explicitly.

3. **Evidence block carries tier labels.** Every Evidence-block URN
   and every Audit entry in your input is tagged with a source_type
   and confidence. Use those — do not invent higher confidence than
   the evidence supports.

4. **Caveats section must list every T4-only claim.** If the only
   evidence for a claim is documentation (no code, no manifest), it goes
   in Caveats explicitly: "X is documented but not code-verified
   this run."

5. **Never cite a tier-5 claim as fact.** If the subgraph only has
   fuzzy matches for part of the answer, say "we couldn't verify —
   see Gaps & unknowns" and move on.

# ─────────────────────────────────────────────────────────────────
"""
"""Inject into every synthesis/summarization prompt where claims are
made from graph evidence. Keeps the tier language consistent so the
LLM's output can be audited by the tier numbers it uses."""
