"""Schemas for collaboration session endpoints.

This module covers the *interaction* loop: a requester opens a session
with a free-text requirement, the system clarifies in a back-and-
forth (driven later by the clarifier_agent), the synthesizer emits
a brief artifact at close. Everything is persisted in
``collaboration_sessions``, ``collab_messages``, ``collab_doubts``,
``collab_artifacts``.

Brief / handover-doc generation (the substrate side) lands later;
those schemas will live in a sibling module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ── Doubts ─────────────────────────────────────────────────────────

DoubtStatus = Literal[
    "open",
    "answered_by_doc",
    "answered_by_human",
    "unresolved",
    "escalated",
]


class CollabDoubtData(BaseModel):
    id: str
    session_id: str
    question: str
    status: DoubtStatus
    resolved_md: str | None = None
    source_ref: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class ResolveDoubtRequest(BaseModel):
    status: DoubtStatus
    resolved_md: str | None = None
    source_ref: str | None = None


# ── Messages ───────────────────────────────────────────────────────

MessageRole = Literal[
    "requester",
    "system",
    "clarifier_agent",
    "synthesizer_agent",
]


class CollabMessageData(BaseModel):
    id: str
    session_id: str
    ord: int
    role: MessageRole
    content_md: str
    source_citations: list[str] = []
    doubt_id: str | None = None
    created_at: datetime


class AppendCollabMessageRequest(BaseModel):
    # Cap the user message length
    # so a slow-loris client can't pin a worker on a 10MB POST body.
    content_md: str = Field(min_length=1, max_length=50_000)
    # Reserved for future use — the current version ignores incoming citations from
    # the requester. The clarifier/synthesizer agents are the only
    # writers that produce real citations.
    source_citations: list[str] = []
    # Optional: link this message to an existing doubt it resolves.
    doubt_id: str | None = None


# ── Artifacts ──────────────────────────────────────────────────────

ArtifactKind = Literal[
    "final_brief",
    "estimate",
    "risk_log",
    "draft_ask_message",
]


class CollabArtifactData(BaseModel):
    id: str
    session_id: str
    kind: ArtifactKind
    fs_path: str
    created_at: datetime


# ── Sessions ───────────────────────────────────────────────────────

SessionState = Literal[
    "open",
    "clarifying",
    "resolved",
    "briefed",
    "closed",
]


class CollabSessionSummary(BaseModel):
    id: str
    requester: str
    state: SessionState
    requirement_text: str
    matched_doc_ids: list[str] = []
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    message_count: int = 0
    open_doubt_count: int = 0
    linked_task_count: int = 0
    linked_task_keys: list[str] = []  # jira_key values for linked tasks


class CollabSessionDetail(CollabSessionSummary):
    messages: list[CollabMessageData] = []
    doubts: list[CollabDoubtData] = []
    artifacts: list[CollabArtifactData] = []
    # When a ``final_brief`` artifact exists on disk, the API
    # eagerly reads its Markdown content here so the UI can render
    # the brief inline (via the existing Prose component) without
    # a second round-trip or exposing arbitrary filesystem reads.
    # ``null`` means: no brief artifact yet, or the file is missing
    # / unreadable (e.g., operator deleted it manually).
    brief_md: str | None = None


class CreateCollabSessionRequest(BaseModel):
    # Matches typical interactive-feature-request length; rejects
    # multi-MB payloads at the FastAPI boundary.
    requirement_text: str = Field(min_length=1, max_length=20_000)
    # Optional explicit requester; defaults to the cookie session id.
    requester: str | None = Field(default=None, max_length=200)


class CreateCollabSessionData(BaseModel):
    id: str


class CloseCollabSessionRequest(BaseModel):
    # When true, accept any remaining open doubts as ``unresolved``
    # and proceed to the brief. Otherwise we 409 with the open count.
    accept_open_doubts_as_risks: bool = False


# ── Handover docs (repo scope) ─────────────────────────────────────

HandoverScopeKind = Literal[
    "feature",
    "repo",
    "domain",
    "region",
    "team",
    "session",
]


HandoverFreshness = Literal[
    "fresh",
    "stale",
    "very_stale",
    "enriched_by_children",
    "stale_due_to_children",
]


class HandoverDocSummary(BaseModel):
    id: str
    scope_kind: HandoverScopeKind
    scope_value: str
    parent_id: str | None = None
    fs_path: str | None = None
    freshness_state: HandoverFreshness
    last_generated_at: datetime | None = None
    last_validated_at: datetime | None = None
    generation_cost_usd: float | None = None
    # Non-null when the doc has been soft-archived. The
    # default list endpoint hides archived rows; the UI's "Show
    # archived" toggle adds ``include_archived=true`` so users can
    # restore.
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class HandoverDocDetail(HandoverDocSummary):
    # Eagerly-read body of the on-disk handover ``index.md``; ``null``
    # if the file is missing or unreadable.
    body_md: str | None = None


class HandoverSearchHit(HandoverDocSummary):
    """One result of a free-text search across handover docs. Carries the same summary fields plus a relevance
    ``score`` (3 points for a scope_value match, 1 per body_md
    occurrence capped at 20) and a ``snippet`` of up to 200 chars
    around the first body match. ``snippet`` is ``None`` when the
    match was scope-value-only or the body was unreadable."""

    score: float
    snippet: str | None = None


class GenerateRepoHandoverRequest(BaseModel):
    repo_id: str = Field(min_length=1)


class GenerateRepoHandoverData(BaseModel):
    doc_id: str
    fs_path: str
    title: str
    used_fallback: bool


# Domain + team scopes share the same response shape as
# the repo-scope generator. We keep a single ``GenerateHandoverData``
# alias so future scopes (region / feature) can plug in without a new
# data-class explosion.
GenerateHandoverData = GenerateRepoHandoverData


class GenerateDomainHandoverRequest(BaseModel):
    domain_id: str = Field(min_length=1)


class GenerateTeamHandoverRequest(BaseModel):
    squad: str = Field(min_length=1)


class GenerateRegionHandoverRequest(BaseModel):
    region: str = Field(min_length=1)


# ── Doubt candidates / loop-back ─────────────────────


class DoubtCandidateData(BaseModel):
    """One cluster of recurring unresolved doubts that the matched
    handover doesn't currently answer. Surface in the UI so a human
    can promote the cluster's question into the handover (the
    promotion flow)."""

    representative_question: str
    occurrence_count: int
    session_ids: list[str]
    first_seen_at: datetime
    last_seen_at: datetime


class PromoteCandidateRequest(BaseModel):
    """Append a Q+A entry to the handover's body so the next
    clarifier run can resolve future occurrences as ``answered_by_doc``
    with this answer cited. The promotion is human-authored: the
    requester is asserting the team's ground-truth answer to a
    recurring question, not asking the model to guess.

    Length caps: question is a
    headline so 4K is plenty; answer_md mirrors the chat-message cap.
    """

    question: str = Field(min_length=1, max_length=4_000)
    answer_md: str = Field(min_length=1, max_length=50_000)


class PatchHandoverBodyRequest(BaseModel):
    """Replace the on-disk Markdown body of a handover doc
    with human-edited content. Used by the in-UI Markdown editor —
    the requester is asserting a new ground-truth body, not asking
    the model to regenerate. ``last_validated_at`` bumps; the
    generation timestamps + freshness state are left untouched.

    Length cap: 200K covers the
    largest realistic handover body (multi-repo regional docs run
    ~30-50K post-distillation), with headroom; rejects anything
    larger before it lands in Postgres TOAST.
    """

    body_md: str = Field(min_length=1, max_length=200_000)


# ── Audit log ────────────────────────────────────────

HandoverAuditEventType = Literal[
    "generated",
    "archived",
    "restored",
    "edited",
    "promoted",
    "restored_version",
]


class HandoverAuditEntry(BaseModel):
    """One lifecycle event for a handover doc — generate / archive /
    restore / edit / promote. Surfaced via
    ``GET /handover-docs/{doc_id}/audit`` (newest first, capped at
    100). Reads do NOT emit audit rows; this is write-only fan-out.
    """

    id: str
    doc_id: str
    event_type: HandoverAuditEventType
    # ``"system"`` for generator-initiated events (the most common
    # path), the cookie session id for user-initiated route calls,
    # ``None`` if the route lacked a session cookie.
    actor: str | None = None
    # Free-form payload — emitters can add ``scope_kind`` /
    # ``scope_value`` / ``body_len_before`` / ``body_len_after`` /
    # ``question_len`` / ``answer_len`` etc. Unknown keys are
    # surfaced verbatim so future emitters don't need a schema bump.
    metadata: dict = {}
    created_at: datetime


# ── Body history ────────────────────────────────────

HandoverBodyHistoryEventType = Literal[
    "pre-regen",
    "pre-edit",
    "pre-promote",
    "pre-restore",
]


class HandoverBodyHistoryEntry(BaseModel):
    """One pre-overwrite snapshot of a handover doc's Markdown body.

    Surfaced via ``GET /handover-docs/{doc_id}/history`` (newest
    first, capped at 20). Drives the in-UI diff viewer that lets an
    operator compare the current body against any past version.

    The ``event_type`` is the *upcoming* event the snapshot was
    captured before — ``pre-regen`` / ``pre-edit`` / ``pre-promote``
    — i.e. "the body as it stood right before someone hit regen /
    edit / promote". Pairs with the post-event audit-log vocabulary
    by timestamp correlation.
    """

    id: str
    doc_id: str
    event_type: HandoverBodyHistoryEventType
    body_md: str
    created_at: datetime


# ── Section feedback / inbox / referencing (team-collab gap closure) ───

# The five canonical kinds — a reader uses these to categorise *why*
# a section needs attention. Mirrored in the migration's CHECK
# constraint and the ORM model so all three layers agree.
SectionFeedbackKind = Literal[
    "outdated",
    "incorrect",
    "unclear",
    "missing_info",
    "other",
]


class HandoverFeedbackEntry(BaseModel):
    """One section-feedback row attached to a handover doc.

    Surfaced via ``GET /handover-docs/{doc_id}/section-feedback`` and
    on creation via ``POST /handover-docs/{doc_id}/section-feedback``.
    ``actor`` / ``resolved_by`` carry the verified cookie session id;
    ``None`` when the route lacked a cookie.
    """

    id: str
    doc_id: str
    section_key: str
    kind: SectionFeedbackKind
    note: str | None = None
    actor: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime


class SubmitFeedbackRequest(BaseModel):
    """Body for ``POST /handover-docs/{doc_id}/section-feedback``.

    Length caps mirror the rest of the V1 surface: a
    section_key is a stable handle (8 canonical keys today, room for
    growth), and the note is short prose — 2K is plenty for "this
    section says we use Postgres but we migrated to DynamoDB last
    quarter".
    """

    section_key: str = Field(min_length=1, max_length=100)
    kind: SectionFeedbackKind
    note: str | None = Field(default=None, max_length=2000)


class RestoreVersionRequest(BaseModel):
    """Body for ``POST /handover-docs/{doc_id}/restore-version`` —
    revert the doc body to a specific past snapshot. ``history_id``
    must reference a row from ``handover_body_history`` whose
    ``doc_id`` matches the URL's ``{doc_id}``; otherwise the route
    404s. The current body is snapshotted (event_type='pre-restore')
    BEFORE the overwrite so the restore itself is reversible.
    """

    history_id: str = Field(min_length=1)


class ReferencingSessionEntry(BaseModel):
    """One collaboration session that referenced a handover doc.

    Surfaced via ``GET /handover-docs/{doc_id}/referencing-sessions``
    so a doc owner can see who's been asking about their doc. Order
    is ``created_at DESC``; the field shape is a deliberate subset of
    :class:`CollabSessionSummary` — the doc owner doesn't need every
    detail, just the "is this active?" + "what was the ask?" signal.
    """

    id: str
    requester: str
    requirement_text: str
    state: str
    doubt_count_open: int
    created_at: datetime
    last_activity_at: datetime


class HandoverInboxEntry(BaseModel):
    """One row in a doc owner's inbox.

    Returned by ``GET /handover-docs/inbox``; shows the docs the
    current cookie session has touched recently (own / edited),
    enriched with the three aggregates that drive "what needs my
    attention": open feedback count, referencing session count,
    doubt candidate count.

    ``last_touched_at`` is the most-recent audit-log timestamp where
    the current actor appears, surfaced as the ordering key.
    """

    id: str
    scope_kind: str
    scope_value: str
    freshness_state: str
    open_feedback_count: int
    referencing_sessions_count: int
    doubt_candidates_count: int
    last_touched_at: datetime


class SectionMetadataEntry(BaseModel):
    """Aggregates per section_key for a handover doc.

    Surfaced via ``GET /handover-docs/{doc_id}/section-metadata`` so
    the UI can show per-section "last regenerated" / "open feedback"
    badges without three round-trips per section. All three fields
    are nullable: a section that's never been regenerated and has no
    feedback returns ``None`` / ``0`` / ``None``.
    """

    last_regenerated_at: datetime | None = None
    feedback_open_count: int = 0
    last_feedback_at: datetime | None = None


# ── Cost estimate ────────────────────────────────────


class CostEstimateData(BaseModel):
    """Pre-flight cost estimate for a handover generation.

    Returned by the ``GET .../estimate`` endpoints so the UI can show
    the user what the call will cost before they commit. The same
    estimate is recomputed at generate-time and compared against
    ``settings.handover_max_cost_usd``; if it exceeds the ceiling AND
    the caller didn't pass ``?force=true``, the generate call 409s
    with this body, prompting an explicit override.
    """

    estimated_usd: float
    reasoning: str
    repos_to_generate: int
    will_use_tree: bool
    # When set, the estimate exceeds the configured ceiling. The UI
    # uses this to decide whether to prompt for an explicit override.
    over_ceiling: bool = False
    ceiling_usd: float
