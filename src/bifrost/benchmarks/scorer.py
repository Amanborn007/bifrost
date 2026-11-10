"""Pure scoring functions for the benchmark harness.

Five components, blended into one ``score_total``:

  * **recall** — did the answer cite enough of the expected entities?
  * **precision** — were the citations on-topic (no hallucinated URNs)?
  * **specificity** — are claims grounded in file:line / Jira / PR refs
    rather than vague phrasing?
  * **coverage** — required sections present (HLD/LLD diagrams when
    expected, "Source coverage" block, etc.).
  * **tier_integrity** — citations land at the expected tier or higher
    (T1=code beats T4=slack).

No DB, no LLM, no async. The harness ``runner`` calls these with the
fields it already collected and persists the result. Tests live in
``tests/test_benchmark_scorer.py``.

The scoring is intentionally conservative — it would rather under-credit
a real win than over-credit a flowery answer that lacks citations.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# ── Component score weights ───────────────────────────────────────────────────
#
# Recall + specificity dominate because they're the operator's main
# complaints when a chat answer is bad ("you didn't cite X" / "this
# reads like a wikipedia entry, where are the file:line refs"). The
# Two more axes: ``evidence_consistency``
# catches "headline=confirmed but body says 'not routed'" — the main
# trust-breaker — and ``format_adherence`` catches "1500-word dossier
# when the user asked for one-page".
#
# Weighting: evidence_consistency carries real
# weight (0.15) because over-claiming under thin evidence is the
# specific failure mode in multi-turn benchmark runs.
DEFAULT_WEIGHTS: dict[str, float] = {
    "recall": 0.25,
    "precision": 0.15,
    "specificity": 0.20,
    "coverage": 0.10,
    "tier_integrity": 0.05,
    "evidence_consistency": 0.15,
    "format_adherence": 0.10,
}


# Phrases the synthesizer emits when its evidence coverage admits a gap.
# Mirror of ``coordinator._EVIDENCE_GAP_NEEDLES`` — kept in sync. Lives
# here as a copy (rather than imported) so the scorer stays
# dependency-free of coordinator internals.
_GAP_NEEDLES: tuple[str, ...] = (
    "search failed",
    "lookup failed",
    "request failed",
    "unavailable this turn",
    "timed out",
    "not routed",
    "not in scope",
    "not walked",
    "couldn't determine",
    "could not determine",
    "exact caller unknown",
    "exact upstream caller unknown",
    "documented but not code-verified",
    "treat as speculative",
    "first commit/pr not actually located",
    "no code agent ran",
    "no signal from",
)


# Format-derived word caps when ``expected_format`` is set but
# ``max_words`` is left blank in the YAML. Tunable via the YAML's
# explicit ``max_words`` field per question.
_FORMAT_DEFAULT_MAX_WORDS: dict[str, int] = {
    "concise": 300,
    "executive": 200,
    "single_decision": 100,
}


# Section headings the synthesizer emits in dossier mode that should
# NOT appear in concise/executive answers. Used by format_adherence
# to penalize "concise" answers that still include the verbose blocks.
_DOSSIER_ONLY_SECTIONS: tuple[str, ...] = (
    "### source coverage",
    "### caveats & staleness",
    "### cross-source alignment",
    "### gaps & unknowns",
)


# Regexes for citation detection in answer text. Conservative — we'd
# rather miss a citation than count a coincidental string match.
_URN_RE = re.compile(r"urn:bifrost:[a-zA-Z0-9_/:.\-]+")
_FILE_LINE_RE = re.compile(r"\b[a-zA-Z0-9_./\-]+\.[a-zA-Z]{1,5}:\d+(?:-\d+)?\b")
_JIRA_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_PR_NUM_RE = re.compile(
    r"(?:(?<=\s)|(?<=^))(?:PR\s?#?|#)\d{2,6}\b",
    re.IGNORECASE,
)
_PR_URL_RE = re.compile(r"github\.com/[\w.\-]+/[\w.\-]+/pull/\d+")


@dataclass
class ScoreInput:
    """All scorer inputs — what the runner collected from one turn.

    Treat as immutable. Lists pass through as-is; the scorer
    de-duplicates internally.
    """

    answer_text: str
    answer_confidence: str | None = None
    cited_urns: list[str] = field(default_factory=list)
    expected_entities: list[str] = field(default_factory=list)
    expected_jira_refs: list[str] = field(default_factory=list)
    expected_pr_refs: list[str] = field(default_factory=list)
    expected_key_facts: list[dict[str, Any]] = field(default_factory=list)
    tier_floor: int = 3
    expects_diagram: bool = False
    # Optional: 1-hop adjacency map URN→{neighbour_URNs}. When provided,
    # precision counts a one-hop neighbour as adjacent (not a hallucination).
    one_hop_neighbours: dict[str, set[str]] = field(default_factory=dict)
    # Format-adherence expectations:
    expected_format: str | None = None  # dossier|concise|executive|single_decision
    max_words: int | None = None  # hard cap; format-derived if None
    # Bundle-side audit signal — the runner can pre-compute "did any
    # per-source agent admit a coverage gap?" and pass it in. When True,
    # evidence_consistency penalizes a "confirmed" headline regardless
    # of whether the answer body surfaced the gap explicitly.
    bundle_has_gap: bool = False


@dataclass
class ScoreOutput:
    """Component scores + the metadata the UI needs to drill down."""

    recall: float
    precision: float
    specificity: float
    coverage: float
    tier_integrity: float
    evidence_consistency: float
    format_adherence: float
    total: float
    cited_entity_urns: list[str]
    cited_jira_refs: list[str]
    cited_pr_refs: list[str]
    missing_entities: list[str]
    missing_key_facts: list[dict[str, Any]]
    breakdown: dict[str, Any]

    def as_db_row(self) -> dict[str, Any]:
        """Shape ready to splat into ``BenchmarkRun(**this)``."""
        return {
            "score_recall": round(self.recall, 3),
            "score_precision": round(self.precision, 3),
            "score_specificity": round(self.specificity, 3),
            "score_coverage": round(self.coverage, 3),
            "score_tier_integrity": round(self.tier_integrity, 3),
            "score_evidence_consistency": round(self.evidence_consistency, 3),
            "score_format_adherence": round(self.format_adherence, 3),
            "score_total": round(self.total, 3),
            "cited_entity_urns": self.cited_entity_urns,
            "cited_jira_refs": self.cited_jira_refs,
            "cited_pr_refs": self.cited_pr_refs,
            "missing_entities": self.missing_entities,
            "missing_key_facts": self.missing_key_facts,
            "raw_score_breakdown": self.breakdown,
        }


# ── Citation extraction ──────────────────────────────────────────────────────


def _extract_urns(text: str) -> list[str]:
    """Return the unique URNs appearing in ``text``, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _URN_RE.finditer(text or ""):
        urn = m.group(0).rstrip(".,);")
        if urn not in seen:
            seen.add(urn)
            out.append(urn)
    return out


def _extract_jira(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _JIRA_RE.finditer(text or ""):
        ref = m.group(0)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _extract_pr_refs(text: str) -> list[str]:
    """Pulls both ``#1234`` shortforms and full GitHub PR URLs."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PR_URL_RE.finditer(text or ""):
        ref = m.group(0)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    for m in _PR_NUM_RE.finditer(text or ""):
        ref = m.group(0)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _extract_file_lines(text: str) -> list[str]:
    """``file.ext:line`` and ``file.ext:line-line`` references."""
    return list({m.group(0) for m in _FILE_LINE_RE.finditer(text or "")})


# ── Component scoring ────────────────────────────────────────────────────────


def _entity_name_from_urn(urn: str) -> str | None:
    """Pull the entity name out of a Bifrost URN.

    ``urn:bifrost:service:ShipmentService`` → ``ShipmentService``
    ``urn:bifrost:repo/ShipmentService/component/OrderRouter`` → ``OrderRouter``
    """
    if urn.startswith("urn:bifrost:service:"):
        return urn.split("urn:bifrost:service:", 1)[1] or None
    if urn.startswith("urn:bifrost:repo/"):
        tail = urn.split("urn:bifrost:repo/", 1)[1]
        if "/component/" in tail:
            return tail.rsplit("/component/", 1)[1] or None
        if "/flow/" in tail:
            return tail.rsplit("/flow/", 1)[1] or None
        # bare repo URN — name is the repo segment
        return tail.split("/", 1)[0] or None
    return None


def _compute_recall(
    cited: Iterable[str],
    expected: Iterable[str],
    *,
    answer_text: str = "",
) -> tuple[float, list[str]]:
    """Fraction of expected URNs cited; returns (score, missing list).

    Two ways an expected URN counts as "cited":
      1. The exact URN string is in ``cited`` (the citation list).
      2. The URN's entity name appears in the answer body — handles
         the common case where the synthesizer mentions ``ShipmentService`` in
         prose with a footnote ref but the footnote uses a non-URN
         reference (e.g., a repo path or PR URL). The reviewer scores
         these answers as having "cited" the entity; the scorer
         should agree.

    Empty ``expected`` => score 1.0 (vacuously satisfied).
    """
    expected_set = set(expected)
    if not expected_set:
        return 1.0, []
    cited_set = set(cited)

    # Build a name-match index from the answer body for option 2.
    body = answer_text or ""
    body_lower = body.lower()

    hits: set[str] = set()
    for urn in expected_set:
        if urn in cited_set:
            hits.add(urn)
            continue
        name = _entity_name_from_urn(urn)
        if name and len(name) >= 3:
            # Word-boundary-ish match: name surrounded by non-word
            # characters (spaces, punctuation) avoids accidental
            # substring matches inside other identifiers.
            name_l = name.lower()
            for i in range(len(body_lower)):
                idx = body_lower.find(name_l, i)
                if idx == -1:
                    break
                left = body_lower[idx - 1] if idx > 0 else " "
                right = (
                    body_lower[idx + len(name_l)] if idx + len(name_l) < len(body_lower) else " "
                )
                # name lives in our identifier set; allow alpha-num
                # adjacency only when it's a fragment of THE SAME
                # camelCase word — heuristically: check that
                # surrounding chars are not alpha. Simpler safe rule:
                # treat as match unless surrounded by alpha-letters
                # (which would mean the name is part of a longer
                # identifier).
                if not (left.isalpha() or right.isalpha()):
                    hits.add(urn)
                    break
                i = idx + 1
    missing = sorted(expected_set - hits)
    return len(hits) / len(expected_set), missing


def _compute_precision(
    cited: Iterable[str],
    expected: Iterable[str],
    *,
    one_hop: dict[str, set[str]],
) -> float:
    """Fraction of cited URNs that are on-topic.

    Originally: hits / total_cited. A smoke test revealed
    this punishes deep-dossier answers that legitimately cite many
    components (e.g., 72 URNs in a service-map answer). The expected
    set is small (4 URNs); requiring ALL cited URNs to be in it
    produces 0.028 precision for an answer that's actually correct.

    Revised rule: a cited URN is "on-topic" if it's in the expected
    set, one-hop adjacent in the graph, OR if it shares a repo prefix
    with any expected URN. The repo-prefix relaxation handles the
    common case where the question lists service URNs but the answer
    cites components that live in those repos (legitimate depth, not
    hallucination).

    Returns 0.0 only when there are no cited URNs at all (answer not
    grounded). When cited URNs are present, precision is the on-topic
    fraction of the cited list.
    """
    cited_list = list(cited)
    if not cited_list:
        return 0.0
    expected_set = set(expected)
    adjacent: set[str] = set()
    for urn in expected_set:
        adjacent |= one_hop.get(urn, set())
    on_topic = expected_set | adjacent

    # Repo-prefix relaxation: derive {repo prefixes} from expected
    # URNs and credit any cited URN that starts with one. This treats
    # "urn:bifrost:service:ShipmentService" + "urn:bifrost:repo/ShipmentService/component/X"
    # as on-topic siblings of the same repo.
    repo_prefixes: set[str] = set()
    for u in expected_set:
        # Extract the repo segment from URNs of the form
        # urn:bifrost:service:<repo> or urn:bifrost:repo/<repo>/...
        if u.startswith("urn:bifrost:service:"):
            repo_prefixes.add(u.split("urn:bifrost:service:", 1)[1])
        elif u.startswith("urn:bifrost:repo/"):
            tail = u.split("urn:bifrost:repo/", 1)[1]
            repo_prefixes.add(tail.split("/", 1)[0])

    def _on_topic(u: str) -> bool:
        if u in on_topic:
            return True
        for prefix in repo_prefixes:
            if u == f"urn:bifrost:service:{prefix}" or u.startswith(f"urn:bifrost:repo/{prefix}/"):
                return True
        return False

    hits = sum(1 for u in cited_list if _on_topic(u))
    return hits / len(cited_list)


def _compute_specificity(
    answer_text: str,
    *,
    cited_urns_count: int,
    cited_jira_count: int,
    cited_pr_count: int,
) -> tuple[float, dict[str, int]]:
    """Heuristic for "how grounded is this answer?"

    Counts citation density. We use the body's bullet/sentence count as
    the denominator: an answer with N body claims and M citation hits
    scores M/N (capped at 1.0). 50+ refs in a 12-bullet answer doesn't
    score >1; a 12-bullet answer with 6 refs scores 0.5.
    """
    body = answer_text or ""
    # Count "claims" — rough proxy via sentence + bullet markers
    sentences = max(
        1,
        sum(1 for _ in re.finditer(r"[.!?](?=\s|$)", body))
        + sum(1 for _ in re.finditer(r"^\s*[-*•]\s", body, re.MULTILINE)),
    )
    file_lines = len(_extract_file_lines(body))
    refs_total = cited_urns_count + cited_jira_count + cited_pr_count + file_lines
    raw = refs_total / sentences
    return min(1.0, raw), {
        "sentences_proxy": sentences,
        "urn_refs": cited_urns_count,
        "jira_refs": cited_jira_count,
        "pr_refs": cited_pr_count,
        "file_line_refs": file_lines,
        "total_refs": refs_total,
    }


def _compute_coverage(
    answer_text: str,
    *,
    expected_key_facts: list[dict[str, Any]],
    expects_diagram: bool,
) -> tuple[float, list[dict[str, Any]]]:
    """Required structural elements + key-fact needles found?

    Two contributions, weighted equally:
      * Diagram presence (if expected): a Mermaid block must exist.
      * Key-fact recall: each {needle, why} dict's needle string is
        case-insensitive substring-searched in the answer body. Missing
        needles populate the second return value (the audit log shows
        operators *which* facts the system missed).
    """
    body = answer_text or ""
    components: list[float] = []

    if expects_diagram:
        has_diagram = bool(re.search(r"```mermaid\b", body, re.IGNORECASE))
        components.append(1.0 if has_diagram else 0.0)

    missing_facts: list[dict[str, Any]] = []
    if expected_key_facts:
        body_lower = body.lower()
        hit = 0
        for fact in expected_key_facts:
            needle = str(fact.get("needle", "")).strip().lower()
            if needle and needle in body_lower:
                hit += 1
            elif needle:
                missing_facts.append(dict(fact))
        components.append(hit / len(expected_key_facts))

    if not components:
        return 1.0, []  # nothing required → trivially covered
    return sum(components) / len(components), missing_facts


_TIER_LEVEL_MAP = {
    "confirmed": 1,
    "inferred": 3,
    "stale": 4,
    "uncertain": 5,
    "conflicting": 4,
    "unknown": 5,
}


def _compute_evidence_consistency(
    *,
    answer_text: str,
    answer_confidence: str | None,
    bundle_has_gap: bool,
) -> tuple[float, dict[str, Any]]:
    """Headline confidence vs body coverage gap.

    Penalty cases (score = 0):
      * ``answer_confidence == 'confirmed'`` AND any gap-marker phrase
        appears in the answer body.
      * ``answer_confidence == 'confirmed'`` AND the runner pre-computed
        ``bundle_has_gap=True`` (one of the per-source agents errored
        out, even if the synthesizer didn't surface it).

    Otherwise score 1.0.

    A confident answer built on a bundle with a known gap is the
    most damaging trust failure, so it scores 0.
    """
    debug: dict[str, Any] = {
        "answer_confidence": answer_confidence,
        "bundle_has_gap": bundle_has_gap,
        "matched_gap": None,
    }
    if answer_confidence != "confirmed":
        # Lower-tier headlines are vacuously consistent
        return 1.0, debug

    body_l = (answer_text or "").lower()
    matched: str | None = None
    for needle in _GAP_NEEDLES:
        if needle in body_l:
            matched = needle
            break

    if matched or bundle_has_gap:
        debug["matched_gap"] = matched
        return 0.0, debug
    return 1.0, debug


def _word_count(text: str) -> int:
    """Cheap whitespace-split word count. Good enough for a length cap."""
    return len((text or "").split())


def _compute_format_adherence(
    *,
    answer_text: str,
    expected_format: str | None,
    max_words: int | None,
) -> tuple[float, dict[str, Any]]:
    """Did the answer respect the question's format expectation?

    No expectation set (``expected_format is None``) → score 1.0.

    For concise/executive/single_decision:
      * Word cap: ``max_words`` if provided, else format-derived
        default. Penalty scales linearly: at the cap → 1.0, at 2×
        cap → 0.0, between → linear.
      * Section penalty: each dossier-only section (Source coverage,
        Caveats, Cross-source alignment, Gaps & unknowns) found in the
        body subtracts 0.25 (capped at 1.0 total penalty).

    The failure this catches: a "one-page, no fluff" ask answered
    with a long dossier carrying every dossier-only section.
    """
    debug: dict[str, Any] = {
        "expected_format": expected_format,
        "max_words_used": None,
        "actual_words": _word_count(answer_text),
        "dossier_sections_found": [],
    }
    if not expected_format or expected_format == "dossier":
        return 1.0, debug

    cap = max_words or _FORMAT_DEFAULT_MAX_WORDS.get(expected_format)
    if cap is None:
        # Unknown format — vacuously satisfied (forward-compat)
        return 1.0, debug
    debug["max_words_used"] = cap

    actual = debug["actual_words"]
    if actual <= cap:
        length_score = 1.0
    elif actual >= 2 * cap:
        length_score = 0.0
    else:
        # Linear interpolation between cap and 2×cap
        length_score = max(0.0, 1.0 - (actual - cap) / cap)

    body_l = (answer_text or "").lower()
    dossier_hits: list[str] = []
    for section in _DOSSIER_ONLY_SECTIONS:
        if section in body_l:
            dossier_hits.append(section)
    debug["dossier_sections_found"] = dossier_hits

    section_penalty = min(1.0, 0.25 * len(dossier_hits))
    score = max(0.0, length_score - section_penalty)
    return score, debug


def _compute_tier_integrity(
    *,
    answer_confidence: str | None,
    tier_floor: int,
    answer_text: str = "",
) -> float:
    """1.0 if the answer's confidence_level is at the expected tier or
    higher; partial credit when an honest downgrade was warranted.

    ``tier_floor`` is interpreted as "this question expects T<floor> or
    better". An answer flagged ``confirmed`` (T1) for a T1-floor
    question scores 1.0.

    Smoke-test finding: when evidence_consistency
    correctly downgrades ``confirmed → inferred``, the previous
    binary rule scored ``tier_integrity = 0`` — double-penalizing a
    correct fix. New rule: when the answer body legitimately admits
    coverage gaps (gap-marker phrases present), being one tier below
    the floor scores **0.5 (honest downgrade)** instead of 0.

    Two tiers below the floor still scores 0 — that's actual under-
    confidence, not honest hedging.
    """
    if not answer_confidence:
        # No confidence reported → can't judge. Score middle so an
        # answer-shape regression doesn't crash the metric to 0.
        return 0.5
    actual_tier = _TIER_LEVEL_MAP.get(answer_confidence, 5)
    if actual_tier <= tier_floor:
        return 1.0
    body_l = (answer_text or "").lower()
    has_gap_marker = any(needle in body_l for needle in _GAP_NEEDLES)
    if has_gap_marker and actual_tier == tier_floor + 2:
        # confirmed (T1) → inferred (T3) is a 2-tier drop but the
        # honest "I admitted the gap" partial credit still applies
        # because confirmed↔inferred is the most common evidence-
        # consistency downgrade path.
        return 0.5
    if has_gap_marker and actual_tier <= tier_floor + 2:
        return 0.5
    return 0.0


# ── Public entry point ───────────────────────────────────────────────────────


def score_answer(
    inp: ScoreInput,
    *,
    weights: dict[str, float] | None = None,
) -> ScoreOutput:
    """Run all five components, blend by ``weights``, return the row.

    The harness calls this once per turn; the result inserts directly
    into ``benchmark_runs``. Weights default to ``DEFAULT_WEIGHTS`` and
    can be overridden when the user wants to retro-score the same row
    with different priorities (UI experiment).
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    # ── extract citations from BOTH the answer text and the runner's
    # ``cited_urns`` list (the runner pulls them from the synthesizer's
    # ``sources`` field). Union — answers cite via inline URNs OR
    # source-list footnotes; we want both reflected.
    cited_urns_text = _extract_urns(inp.answer_text)
    cited_urns_all = list(dict.fromkeys([*inp.cited_urns, *cited_urns_text]))

    cited_jira = _extract_jira(inp.answer_text)
    cited_pr = _extract_pr_refs(inp.answer_text)

    recall, missing_entities = _compute_recall(
        cited_urns_all,
        inp.expected_entities,
        answer_text=inp.answer_text,
    )
    precision = _compute_precision(
        cited_urns_all,
        inp.expected_entities,
        one_hop=inp.one_hop_neighbours,
    )
    specificity, spec_breakdown = _compute_specificity(
        inp.answer_text,
        cited_urns_count=len(cited_urns_all),
        cited_jira_count=len(cited_jira),
        cited_pr_count=len(cited_pr),
    )
    coverage, missing_facts = _compute_coverage(
        inp.answer_text,
        expected_key_facts=inp.expected_key_facts,
        expects_diagram=inp.expects_diagram,
    )
    tier_integrity = _compute_tier_integrity(
        answer_confidence=inp.answer_confidence,
        tier_floor=inp.tier_floor,
        answer_text=inp.answer_text,
    )
    evidence_consistency, evi_breakdown = _compute_evidence_consistency(
        answer_text=inp.answer_text,
        answer_confidence=inp.answer_confidence,
        bundle_has_gap=inp.bundle_has_gap,
    )
    format_adherence, fmt_breakdown = _compute_format_adherence(
        answer_text=inp.answer_text,
        expected_format=inp.expected_format,
        max_words=inp.max_words,
    )

    total = (
        w["recall"] * recall
        + w["precision"] * precision
        + w["specificity"] * specificity
        + w["coverage"] * coverage
        + w["tier_integrity"] * tier_integrity
        + w["evidence_consistency"] * evidence_consistency
        + w["format_adherence"] * format_adherence
    )

    breakdown = {
        "weights": w,
        "specificity": spec_breakdown,
        "evidence_consistency": evi_breakdown,
        "format_adherence": fmt_breakdown,
        "expected_entities_n": len(inp.expected_entities),
        "expected_facts_n": len(inp.expected_key_facts),
        "answer_chars": len(inp.answer_text or ""),
    }

    return ScoreOutput(
        recall=recall,
        precision=precision,
        specificity=specificity,
        coverage=coverage,
        tier_integrity=tier_integrity,
        evidence_consistency=evidence_consistency,
        format_adherence=format_adherence,
        total=total,
        cited_entity_urns=cited_urns_all,
        cited_jira_refs=cited_jira,
        cited_pr_refs=cited_pr,
        missing_entities=missing_entities,
        missing_key_facts=missing_facts,
        breakdown=breakdown,
    )
