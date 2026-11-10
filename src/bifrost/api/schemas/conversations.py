"""Schemas for conversation (chat thread) endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, model_validator


class SourceCitation(BaseModel):
    """Wire-shape for a single citation in an assistant message.

    ``name`` was historically required, which broke deserialization of
    legacy ``conversation_messages.sources`` rows that stored only
    ``{'urn': '...'}``. Those entries pre-date the synthesis layer that
    began emitting full citation dicts. Making ``name`` optional with
    a URN-derived default keeps old conversations renderable without
    a JSONB migration.
    """

    urn: str
    name: str | None = None
    entity_type: str | None = None
    # Coordinator emits string labels ("high"/"medium"/"low"); older code
    # paths emit numeric 0..1. Accept both, stringify for UI rendering.
    relevance: float | str | None = None
    role: str | None = None
    files: list[str] | None = None
    stale: bool | None = None
    repo: str | None = None

    @model_validator(mode="after")
    def _fill_name_from_urn(self) -> SourceCitation:
        """Derive a display name from the URN's last segment when the
        underlying row lacked one.

        Only applies the colon-split derivation to canonical Bifrost
        URNs (``urn:bifrost:...``). Other identifiers (URLs, raw
        keys, etc.) pass through verbatim — splitting ``https://x/y``
        on ``:`` produces nonsense like ``//x/y``.

        Defensive: an empty URN yields ``(unknown)`` so the FE
        always renders something the operator can recognise."""
        if not self.name:
            urn = (self.urn or "").strip()
            if not urn:
                self.name = "(unknown)"
            elif urn.lower().startswith("urn:"):
                # Bifrost URN → take the segment after the last ``:``
                tail = urn.rsplit(":", 1)[-1].strip()
                self.name = tail or urn
            else:
                self.name = urn
        return self


class MessageData(BaseModel):
    id: str
    conversation_id: str
    turn_index: int
    role: Literal["user", "assistant"]
    content: str
    sources: list[SourceCitation] = []
    confidence: str | None = None
    reasoning: str | None = None
    gaps: list[str] = []
    token_counts: dict[str, Any] = {}
    # "Ask next" follow-up prompt chips. Each: {prompt, rationale,
    # repos_to_index}. Empty for user turns / turns with no gaps.
    suggested_followups: list[dict[str, Any]] = []
    cost_usd: float | None = None
    created_at: datetime


class LastResearchSummary(BaseModel):
    """Honest, after-the-fact view of what the most recent research turn
    actually did — sourced from ``conversation_research_state.bundle``.

    The header used to render ``scope_repo_ids`` (the *input* scope, which
    is often empty or larger than the set the router actually used). That
    misled operators: e.g. ``[]`` rendered as "All repositories" while the
    router fanned out to 3 fallback repos, or ``[a,b,c,d]`` rendered as
    "Scoped to 4 repos" when only one contributed. This summary surfaces
    the post-routing reality so the UI can show "N repos routed: a, b, c"
    plus a warning when the routing fell back."""

    repos_used: list[str] = []
    repos_used_names: list[str] = []
    scope_rationale: str | None = None
    router_fallback: bool = False
    """True when the rationale indicates the router couldn't match the
    question and fell back to default candidates, or when all per-repo
    agents returned irrelevant/failed. Drives the amber badge in the
    header so the operator knows the answer is suspect."""


class ConversationSummary(BaseModel):
    id: str
    title: str
    scope_repo_ids: list[str] = []
    scope_domain_id: str | None = None
    archived: bool = False
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None
    last_research: LastResearchSummary | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageData] = []


class CreateConversationRequest(BaseModel):
    title: str | None = None  # optional; auto-derived from first message if omitted
    scope_repo_ids: list[str] = []
    scope_domain_id: str | None = None


class SeedConversationRequest(BaseModel):
    """Spawn a new conversation pre-loaded with context — the
    primitive collaboration surfaces use to wire 'Discuss this' /
    'Follow up' affordances back into chat.

    The seed lands as a single user message at turn 0; no research
    run fires until the operator clicks Send in the composer.
    """

    title: str | None = None
    scope_repo_ids: list[str] = []
    scope_domain_id: str | None = None
    heading: str | None = None
    """Short descriptive header rendered as ``## {heading}`` at the
    top of the seed message — surfaces what surface spawned this
    conversation when the operator scrolls back later."""
    seed_context: str | None = None
    """Free-form markdown body — typically a rendering of the
    digest section / ask card / handover row being discussed."""
    follow_up_question: str | None = None
    """Optional pre-typed question. When set, appended after the
    context with a ``---`` separator so the conversation already
    has a question for the assistant to answer when the operator
    sends."""


class CreateConversationData(BaseModel):
    id: str


class RenameConversationRequest(BaseModel):
    title: str


class AppendMessageRequest(BaseModel):
    content: str
    # optional override per-turn of scope
    scope_repo_ids: list[str] | None = None
    # Resumability — when set, a retry of an identical request returns
    # the cached final answer instead of spawning a fresh research run.
    # Generate one UUID per turn client-side; reuse the same UUID on
    # retry. The dedup window is 24h.
    idempotency_key: str | None = None
    # Bypass the idempotency cache and force a fresh research run even
    # if a cached answer exists for this idempotency_key. Useful when
    # the user knows source data (Jira, code) has changed since the
    # cached answer was produced.
    force_refresh: bool = False


class AppendMessageData(BaseModel):
    user_message_id: str
    # streaming: the assistant message is streamed via SSE.
    # Client connects to /conversations/{id}/stream after POST.
    stream_url: str
    # Populated only when idempotency_key matched a recent done run —
    # the client can render the cached answer immediately without
    # waiting for the SSE stream.
    cached_answer: str | None = None
    cached_run_id: str | None = None


class DeleteConversationData(BaseModel):
    id: str
    status: Literal["deleted"] = "deleted"
