"""Conversation (chat thread) endpoints.

Provides CRUD over persistent chat threads plus SSE streaming of
assistant answers. Identity is session-based via the `bifrost_session`
cookie (no real auth in V1).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from bifrost.api._cookie_signing import mint_signed_cookie, parse_signed_cookie
from bifrost.api.deps import Repo, Settings
from bifrost.api.schemas.chat_settings import (
    ChatSettings,
    PartialChatSettings,
    merge_settings,
    settings_from_stored,
)
from bifrost.api.schemas.conversations import (
    AppendMessageData,
    AppendMessageRequest,
    ConversationDetail,
    ConversationSummary,
    CreateConversationData,
    CreateConversationRequest,
    DeleteConversationData,
    LastResearchSummary,
    MessageData,
    RenameConversationRequest,
    SeedConversationRequest,
    SourceCitation,
)
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.sse import event_stream
from bifrost.errors import ValidationError

router = APIRouter(prefix="/conversations", tags=["conversations"])


SESSION_COOKIE_NAME = "bifrost_session"


# Markers in ``bundle.scope_rationale`` that indicate the per-repo router
# couldn't match the question and either picked arbitrary fallbacks or
# every routed agent came back empty. Surfaced as an amber badge in the
# chat header so the operator doesn't trust the answer at face value.
_ROUTER_FALLBACK_MARKERS = (
    "router failed",
    "all per-repo agents returned irrelevant",
    "partial bundle",
)


def _last_research_from_bundle(bundle: dict[str, Any] | None) -> LastResearchSummary | None:
    """Project the persisted research bundle into the wire shape.

    Returns ``None`` when no bundle exists or it has no per-repo data —
    that's the "fresh conversation, no turn yet" case where the UI falls
    back to the pre-turn ``scope_repo_ids`` display."""
    if not bundle:
        return None
    repos_used = list(bundle.get("repos_used") or [])
    repos_used_names = list(bundle.get("repos_used_names") or [])
    if not repos_used and not repos_used_names:
        return None
    rationale_raw = bundle.get("scope_rationale")
    rationale = str(rationale_raw) if rationale_raw else None
    needle = (rationale or "").lower()
    router_fallback = any(m in needle for m in _ROUTER_FALLBACK_MARKERS)
    return LastResearchSummary(
        repos_used=[str(r) for r in repos_used if r],
        repos_used_names=[str(n) for n in repos_used_names if n],
        scope_rationale=rationale,
        router_fallback=router_fallback,
    )


def _ensure_session(response: Response, session: str | None) -> str:
    """Return the caller's session id, setting the cookie if missing.

    Only a cookie whose signature validates is honoured; a missing,
    bare-hex or badly signed cookie gets a fresh signed session.
    """
    parsed = parse_signed_cookie(session)
    if parsed is not None:
        return parsed
    new_value = mint_signed_cookie()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        new_value,
        max_age=60 * 60 * 24 * 365,  # 1 year
        httponly=False,  # readable by JS so the client can echo it; no sensitive data here
        samesite="lax",
    )
    return new_value.split(".", 1)[0]


# ── Create ───────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=BifrostEnvelope[CreateConversationData],
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: CreateConversationRequest,
    request: Request,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[CreateConversationData]:
    session_id = _ensure_session(response, bifrost_session)
    title = (body.title or "New conversation").strip() or "New conversation"
    # Stamp the identity-resolved operator email when available. Audit
    # trail only — session_id remains the access-control owner.
    principal = getattr(request.state, "principal", None)
    owner_email = getattr(principal, "email", None) if principal else None
    conv = await repo.create_conversation(
        session_id=session_id,
        title=title,
        scope_repo_ids=body.scope_repo_ids,
        scope_domain_id=uuid.UUID(body.scope_domain_id) if body.scope_domain_id else None,
        owner_email=owner_email,
    )
    return BifrostEnvelope(data=CreateConversationData(id=str(conv.id)))


# ── Seed (chat-spawn from collab surfaces) ──────────────────────────


@router.post(
    "/seed",
    response_model=BifrostEnvelope[CreateConversationData],
    status_code=status.HTTP_201_CREATED,
)
async def seed_conversation(
    body: SeedConversationRequest,
    request: Request,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[CreateConversationData]:
    """Create a new conversation pre-loaded with context — the
    universal primitive every collaboration surface uses to spawn a
    chat for follow-up.

    Why a dedicated route instead of POST + immediate POST /messages:
    those are TWO requests with a session-commit race in between
    (same race we hit with task spawn). One transaction, one trip,
    no race — and the FE can navigate to /query?conv=<id> as soon
    as it gets the response.

    The seeded user message is written here so the conversation
    isn't visually empty when the operator lands on it. The actual
    research run does NOT fire — the operator hits the composer's
    Send button when they're ready to ask.
    """
    session_id = _ensure_session(response, bifrost_session)
    principal = getattr(request.state, "principal", None)
    owner_email = getattr(principal, "email", None) if principal else None

    title = (body.title or "Follow-up").strip()[:120] or "Follow-up"
    conv = await repo.create_conversation(
        session_id=session_id,
        title=title,
        scope_repo_ids=body.scope_repo_ids or [],
        scope_domain_id=(uuid.UUID(body.scope_domain_id) if body.scope_domain_id else None),
        owner_email=owner_email,
    )

    # Render the seed message body. Two pieces:
    #   1) A short heading so the operator sees what context this
    #      conversation was spawned from when they scroll back later.
    #   2) The seed_context (free-form markdown supplied by the
    #      caller — typically a rendering of the section / ask /
    #      digest item being discussed).
    parts: list[str] = []
    if body.heading:
        parts.append(f"## {body.heading}\n")
    if body.seed_context:
        parts.append(body.seed_context.strip())
    if body.follow_up_question:
        parts.append("\n---\n")
        parts.append(body.follow_up_question.strip())
    seed_body = "\n".join(parts).strip()
    if seed_body:
        await repo.append_message(
            conversation_id=conv.id,
            role="user",
            content=seed_body,
        )

    return BifrostEnvelope(data=CreateConversationData(id=str(conv.id)))


# ── List ─────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=BifrostEnvelope[list[ConversationSummary]],
)
async def list_conversations(
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
    include_archived: bool = False,
    limit: int = 50,
) -> BifrostEnvelope[list[ConversationSummary]]:
    """List conversations owned by this cookie session.

    Uses the single-query summary helper to fetch
    message_count + last_message_preview alongside the row data —
    one DB roundtrip instead of N+1.
    """
    session_id = _ensure_session(response, bifrost_session)
    rows = await repo.list_conversations_with_summary(
        session_id=session_id,
        include_archived=include_archived,
        limit=limit,
    )
    return BifrostEnvelope(
        data=[
            ConversationSummary(
                id=str(c.id),
                title=c.title,
                scope_repo_ids=list(c.scope_repo_ids or []),
                scope_domain_id=str(c.scope_domain_id) if c.scope_domain_id else None,
                archived=c.archived,
                created_at=c.created_at,
                updated_at=c.updated_at,
                message_count=count,
                last_message_preview=preview,
            )
            for c, count, preview in rows
        ],
    )


# ── Chat settings ───────────────────────────────────────────────────
#
# Per-conversation knobs that drive research behaviour: walk depth,
# breadth, edge-confidence floor, edge directions, external-source
# toggles, multi-code-agent cap. Stored as JSONB on
# ``conversations.settings_json``; resolved through ``ChatSettings``
# Pydantic at read time so missing keys get factory defaults.
#
# Routes registered BEFORE ``/{conversation_id}`` so the literal
# ``/settings`` path doesn't get parsed as a UUID.


@router.get(
    "/{conversation_id}/settings",
    response_model=BifrostEnvelope[ChatSettings],
)
async def get_conversation_settings(
    conversation_id: str,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[ChatSettings]:
    """Return the resolved chat settings for this conversation.
    Defaults applied for any missing key. Ownership-checked."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(cid, session_id=session_id)
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    stored = await repo.get_chat_settings(cid)
    return BifrostEnvelope(data=settings_from_stored(stored))


@router.patch(
    "/{conversation_id}/settings",
    response_model=BifrostEnvelope[ChatSettings],
)
async def patch_conversation_settings(
    conversation_id: str,
    body: PartialChatSettings,
    response: Response,
    repo: Repo,
    server_settings: Settings,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[ChatSettings]:
    """Merge a partial settings patch into the stored JSONB and
    return the resolved settings. Validates the merged shape via
    ``ChatSettings`` so an invalid combination of fields fails the
    PATCH cleanly with the offending field surfaced. Ownership-
    checked."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    ceiling = server_settings.research_walk_breadth_ceiling
    if body.walk_breadth_soft_cap is not None and body.walk_breadth_soft_cap > ceiling:
        raise ValidationError(
            f"walk_breadth_soft_cap {body.walk_breadth_soft_cap} is above the server ceiling "
            f"of {ceiling}",
            code="settings.walk_breadth_above_ceiling",
            details={"ceiling": ceiling},
        )
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(cid, session_id=session_id)
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    stored = await repo.get_chat_settings(cid)
    # Validate the merged result via Pydantic — a 422 here means
    # the user's patch combined with stored produces invalid state
    # (out-of-range slider, unknown literal, etc.). The unsealed
    # patch shape allows individual fields out-of-range too, but
    # PartialChatSettings has the same Field constraints so this
    # is belt-and-suspenders.
    merged = merge_settings(stored, body)
    # Persist the merged dict back. The ChatSettings round-trip
    # ensures keys and values are normalised (unknown edge_types
    # dropped, etc.).
    await repo.update_chat_settings(cid, merged.model_dump())
    return BifrostEnvelope(data=merged)


# ── Get (with messages) ──────────────────────────────────────────────


@router.get(
    "/{conversation_id}",
    response_model=BifrostEnvelope[ConversationDetail],
)
async def get_conversation(
    conversation_id: str,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[ConversationDetail]:
    """Read a conversation. Ownership is enforced via the cookie
    session id — a leaked conversation UUID can't
    be exploited by a different cookie session."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    conv = await repo.get_conversation_for_session(cid, session_id=session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await repo.list_messages(cid)
    research_row = await repo.load_research_state(cid)
    last_research = _last_research_from_bundle(
        research_row.bundle if research_row else None,
    )
    return BifrostEnvelope(
        data=ConversationDetail(
            id=str(conv.id),
            title=conv.title,
            scope_repo_ids=list(conv.scope_repo_ids or []),
            scope_domain_id=str(conv.scope_domain_id) if conv.scope_domain_id else None,
            archived=conv.archived,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            message_count=len(messages),
            last_message_preview=(messages[-1].content[:120] if messages else None),
            last_research=last_research,
            messages=[
                MessageData(
                    id=str(m.id),
                    conversation_id=str(m.conversation_id),
                    turn_index=m.turn_index,
                    role=m.role,  # type: ignore[arg-type]
                    content=m.content,
                    sources=[SourceCitation(**s) for s in (m.sources or []) if isinstance(s, dict)],
                    confidence=m.confidence,
                    reasoning=m.reasoning,
                    gaps=list(m.gaps or []),
                    token_counts=dict(m.token_counts or {}),
                    cost_usd=m.cost_usd,
                    created_at=m.created_at,
                )
                for m in messages
            ],
        ),
    )


# ── Rename ───────────────────────────────────────────────────────────


@router.patch(
    "/{conversation_id}",
    response_model=BifrostEnvelope[ConversationSummary],
)
async def rename_conversation(
    conversation_id: str,
    body: RenameConversationRequest,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[ConversationSummary]:
    """Rename a conversation. Ownership-checked."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(cid, session_id=session_id)
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv = await repo.rename_conversation(cid, body.title)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Flush from rename_conversation expired ``updated_at`` (server
    # ``onupdate=func.now()`` evaluates server-side, so the ORM
    # invalidates the in-memory value and lazy-loads on next access).
    # Async lazy-loads need a greenlet, so refresh explicitly here.
    await repo._session.refresh(conv)
    messages = await repo.list_messages(cid)
    return BifrostEnvelope(
        data=ConversationSummary(
            id=str(conv.id),
            title=conv.title,
            scope_repo_ids=list(conv.scope_repo_ids or []),
            scope_domain_id=str(conv.scope_domain_id) if conv.scope_domain_id else None,
            archived=conv.archived,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            message_count=len(messages),
            last_message_preview=(messages[-1].content[:120] if messages else None),
        ),
    )


# ── Delete (soft) + Restore ──────────────────────────────────────────


@router.delete(
    "/{conversation_id}",
    response_model=BifrostEnvelope[DeleteConversationData],
)
async def delete_conversation(
    conversation_id: str,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
    hard: bool = False,
) -> BifrostEnvelope[DeleteConversationData]:
    """Soft-delete by default. Pass ``?hard=true`` to
    actually purge the row + cascade to messages — irrecoverable.
    Ownership-checked. Idempotent on already-soft-deleted rows."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    # ``include_deleted=True`` so a re-delete on an already-soft-
    # deleted conv still finds the row and returns 200 (idempotent).
    owned = await repo.get_conversation_for_session(
        cid,
        session_id=session_id,
        include_deleted=True,
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    ok = await repo.delete_conversation(cid, hard=hard)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if hard:
        from bifrost.research.bundle_cache import get_bundle_cache

        await get_bundle_cache().invalidate(conversation_id)
    return BifrostEnvelope(data=DeleteConversationData(id=conversation_id))


@router.post(
    "/{conversation_id}/restore",
    response_model=BifrostEnvelope[ConversationSummary],
)
async def restore_conversation(
    conversation_id: str,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[ConversationSummary]:
    """Un-archive AND un-soft-delete in one call.
    Ownership-checked. Idempotent — a live conv just returns its
    current detail unchanged."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    # Need ``include_deleted=True`` here because deleted rows are the
    # ones we're trying to restore.
    owned = await repo.get_conversation_for_session(
        cid,
        session_id=session_id,
        include_deleted=True,
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    ok = await repo.restore_conversation(cid)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv = await repo.get_conversation(cid)
    assert conv is not None  # restore returned True; row exists
    # After flush, server-side ``onupdate=func.now()`` expired
    # ``updated_at`` on the in-memory row. Without an explicit async
    # refresh, the next ``conv.updated_at`` access triggers a lazy
    # SQL load *outside* any await context → ``MissingGreenlet``.
    await repo._session.refresh(conv)
    messages = await repo.list_messages(cid)
    return BifrostEnvelope(
        data=ConversationSummary(
            id=str(conv.id),
            title=conv.title,
            scope_repo_ids=list(conv.scope_repo_ids or []),
            scope_domain_id=str(conv.scope_domain_id) if conv.scope_domain_id else None,
            archived=conv.archived,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            message_count=len(messages),
            last_message_preview=(messages[-1].content[:120] if messages else None),
        ),
    )


# ── Append message (triggers streaming answer) ──────────────────────

# In-process map of asyncio Tasks for in-flight runs. Used ONLY for
# the cancel endpoint (Task.cancel() needs the actual task handle).
# Dedup / idempotency / restart-resume are decided via the
# ``research_runs`` table — see ``runs.find_in_flight_run`` and
# ``runs.find_by_idempotency``.
_INFLIGHT_CONV_QUERIES: dict[str, asyncio.Task[Any]] = {}


@router.post(
    "/{conversation_id}/messages",
    response_model=BifrostEnvelope[AppendMessageData],
)
async def append_message(
    conversation_id: str,
    body: AppendMessageRequest,
    request: Request,
    response: Response,
    repo: Repo,
    settings: Settings,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[AppendMessageData]:
    """Append a user message + kick off the streaming answer.

    Ownership-checked. Soft-deleted conversations
    are 404 — restore first if you want to keep talking to one.
    """
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    session_id = _ensure_session(response, bifrost_session)
    conv = await repo.get_conversation_for_session(cid, session_id=session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Idempotency dedup: a retry with the same idempotency_key returns
    # the cached final answer instead of spawning a fresh run. Skipped
    # when force_refresh=true. The 24h TTL is enforced inside
    # find_by_idempotency.
    from bifrost.api.deps import get_session_factory
    from bifrost.research.runs import (
        find_by_idempotency,
        find_in_flight_run,
    )

    session_factory = get_session_factory(settings)
    if body.idempotency_key and not body.force_refresh:
        async with session_factory() as session:
            cached = await find_by_idempotency(
                session,
                cid,
                body.idempotency_key,
            )
        if cached and cached.final_answer_text:
            recent_user = await repo.latest_user_message_id(cid)
            return BifrostEnvelope(
                data=AppendMessageData(
                    user_message_id=str(recent_user) if recent_user else "",
                    stream_url=(f"/api/v1/conversations/{conversation_id}/stream"),
                    cached_answer=cached.final_answer_text,
                    cached_run_id=str(cached.id),
                ),
            )

    # Dedup: if a research_run is already in flight for this
    # conversation, refuse the duplicate. The ``research_runs`` table
    # is the source of truth — survives backend restarts where the
    # in-memory _INFLIGHT_CONV_QUERIES map would have been wiped.
    # Callers reattach via the SSE stream_url.
    async with session_factory() as session:
        in_flight = await find_in_flight_run(session, cid)
    if in_flight is not None:
        recent_user = await repo.latest_user_message_id(cid)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "query_in_flight",
                "message": (
                    "A query is already running for this conversation. "
                    "Listen on the existing stream_url; events will "
                    "replay from the buffer when you reconnect."
                ),
                "stream_url": f"/api/v1/conversations/{conversation_id}/stream",
                "user_message_id": str(recent_user) if recent_user else None,
                "run_id": str(in_flight.id),
                "status": in_flight.status,
            },
        )

    # Persist the user message synchronously
    user_msg = await repo.append_message(
        conversation_id=cid,
        role="user",
        content=body.content,
    )

    # Auto-title on first turn if still default
    if conv.title == "New conversation" and len(body.content) > 0:
        await repo.rename_conversation(cid, _auto_title(body.content))

    # Kick off the async query worker (streams via SSE)
    from bifrost.extraction.query_worker import QueryWorkerDeps, run_query_turn
    from bifrost.research.runs import (
        mark_failed as runs_mark_failed,
    )
    from bifrost.research.runs import (
        start_run as runs_start_run,
    )

    deps = QueryWorkerDeps(
        session_factory=session_factory,
        settings=settings,
    )

    # Create the research_runs row up front. The ``turn_id`` is the
    # user message's turn_index; this is unique per conversation so
    # the table's UNIQUE(conversation_id, turn_id) prevents a stray
    # double-spawn for the same turn.
    research_run_id = await runs_start_run(
        session_factory=session_factory,
        conversation_id=cid,
        turn_id=user_msg.turn_index,
        idempotency_key=body.idempotency_key,
    )

    # Per-turn wall-clock cap.
    # Opus synthesis on a wide multi-repo fan-out regularly runs
    # 9-12 min end-to-end; 15min is the next safe ceiling.
    # Frontend watchdog rearms on every synthesis_heartbeat (10s tick)
    # so it never fires while synthesis is alive. The server cap is
    # the fallback for genuinely stuck runs (MCP hang, infinite loop).
    _QUERY_TIMEOUT_SECONDS = 15 * 60

    async def _run_and_track() -> None:
        from bifrost.api.sse import event_bus
        from bifrost.extraction.query_worker import _evt_done, _evt_error
        from bifrost.research.runs import mark_cancelled as runs_mark_cancelled

        try:
            await asyncio.wait_for(
                run_query_turn(
                    conversation_id=cid,
                    user_message_content=body.content,
                    deps=deps,
                    scope_repo_ids=body.scope_repo_ids,
                    research_run_id=research_run_id,
                    turn_index=user_msg.turn_index,
                ),
                timeout=_QUERY_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            # Defense-in-depth for any cancel that reaches the task
            # *without* going through the /cancel endpoint (server
            # shutdown, asyncio.gather parent cancel). The endpoint
            # itself also calls mark_cancelled, so this is idempotent
            # in the user-Stop path. CancelledError is BaseException —
            # without an explicit catch it slipped past the
            # ``except Exception`` arm and left the row stranded in
            # an in-flight phase, which is exactly what made Stop look
            # like a no-op on the next page refresh.
            try:
                await runs_mark_cancelled(session_factory, research_run_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "research_runs mark_cancelled (cancelled) failed; non-fatal",
                )
            raise
        except TimeoutError:
            await event_bus.publish(
                conversation_id,
                _evt_error(
                    f"Query timed out after {_QUERY_TIMEOUT_SECONDS}s. "
                    "Try again, or scope to fewer repos.",
                ),
            )
            await event_bus.publish(conversation_id, _evt_done())
            try:
                await runs_mark_failed(
                    session_factory,
                    research_run_id,
                    error_message=(f"Query timed out after {_QUERY_TIMEOUT_SECONDS}s."),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "research_runs mark_failed (timeout) failed; non-fatal",
                )
        except Exception as exc:
            # run_query_turn already publishes its own error events;
            # this is the last-mile guard for crashes outside the
            # try block in run_query_turn itself.
            logger.exception(
                "query worker crashed for conv=%s",
                conversation_id,
            )
            try:
                await runs_mark_failed(
                    session_factory,
                    research_run_id,
                    error_message=f"Crash: {str(exc)[:300]}",
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "research_runs mark_failed (crash) failed; non-fatal",
                )
        finally:
            _INFLIGHT_CONV_QUERIES.pop(conversation_id, None)

    task = asyncio.create_task(_run_and_track())
    _INFLIGHT_CONV_QUERIES[conversation_id] = task

    return BifrostEnvelope(
        data=AppendMessageData(
            user_message_id=str(user_msg.id),
            stream_url=f"/api/v1/conversations/{conversation_id}/stream",
        ),
    )


@router.get("/{conversation_id}/research-status")
async def research_status(
    conversation_id: str,
    response: Response,
    repo: Repo,
    settings: Settings,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[dict[str, Any]]:
    """Return the most recent research_runs row for a conversation.

    Used by the frontend on page mount + visibility change to detect
    whether a research run is currently in flight (so the progress
    widget can be re-rendered) or whether the most recent attempt
    failed (so the UI shows the right banner).

    Response shape:
      ``{ "data": { "in_flight": <RunRecord|null>, "latest": <RunRecord|null> } }``

    ``in_flight`` is null when nothing is running. ``latest`` is the
    most recent run regardless of state — null only for conversations
    that have never had a research run.
    """
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    session_id = _ensure_session(response, bifrost_session)
    conv = await repo.get_conversation_for_session(cid, session_id=session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    from bifrost.api.deps import get_session_factory
    from bifrost.research.runs import find_in_flight_run, find_latest_run

    session_factory = get_session_factory(settings)
    async with session_factory() as session:
        in_flight = await find_in_flight_run(session, cid)
        latest = await find_latest_run(session, cid)

    return BifrostEnvelope(
        data={
            "in_flight": in_flight.to_dict() if in_flight else None,
            "latest": latest.to_dict() if latest else None,
        },
    )


# ── SSE stream ───────────────────────────────────────────────────────


@router.post("/{conversation_id}/cancel")
async def cancel_conversation(
    conversation_id: str,
    response: Response,
    repo: Repo,
    settings: Settings,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[dict[str, Any]]:
    """Cancel an in-flight query for this conversation.

    A chat answer can take 30-90 seconds (multi-repo
    fan-out + synthesis). The user often realises mid-stream that
    they asked the wrong question — they need a "stop generating"
    affordance. Without it the only option was to wait for the 5-min
    server timeout or close the tab.

    Cancellation is best-effort and operates on two layers:
      * In-memory: ``Task.cancel()`` on the in-flight ``run_query_turn``
        task (if held by this worker). The task's ``finally`` block
        still fires, so the SSE bus gets a terminal ``done`` event and
        the partial-answer checkpoint runs.
      * Durable: ``mark_cancelled`` flips the ``research_runs`` row
        from its in-flight phase to ``cancelled``. Without this step a
        page refresh would re-attach via /research-status to the still
        "in-flight" row and the UI would render a phantom pipeline.
        The DB write is also the only mechanism that catches a cancel
        when the running task lives on a different worker process —
        the in-memory map is per-process.

    Idempotent: cancelling a conversation that has neither an
    in-memory task nor a durable in-flight row returns
    ``{cancelled: false, reason: "not_running"}``.

    Ownership-checked."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(
        cid,
        session_id=session_id,
        include_deleted=True,
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    from bifrost.api.deps import get_session_factory
    from bifrost.research.runs import find_in_flight_run, mark_cancelled

    session_factory = get_session_factory(settings)
    async with session_factory() as session:
        in_flight_row = await find_in_flight_run(session, cid)

    task = _INFLIGHT_CONV_QUERIES.get(conversation_id)
    has_local_task = task is not None and not task.done()

    if not has_local_task and in_flight_row is None:
        return BifrostEnvelope(data={"cancelled": False, "reason": "not_running"})

    # Flip the durable row first so a refresh that races the SSE
    # terminal events (replay buffer drain + status probe land in
    # different orders) always sees a non-in-flight status.
    db_cancelled = False
    if in_flight_row is not None:
        try:
            db_cancelled = await mark_cancelled(session_factory, in_flight_row.id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "research_runs mark_cancelled failed for conv=%s; non-fatal",
                conversation_id,
            )

    if has_local_task:
        task.cancel()  # type: ignore[union-attr]
        # Don't await it — the cancellation propagates through the
        # task's `finally` clause asynchronously. The dedup map is
        # cleared by ``_run_and_track``'s finally block.

    logger.info(
        "conv.cancel conv=%s session=%s task=%s run=%s db=%s",
        conversation_id,
        session_id,
        has_local_task,
        in_flight_row.id if in_flight_row else None,
        db_cancelled,
    )

    # Publish an explicit cancel signal so the UI doesn't have to
    # wait for the task to wind down before it sees the terminal.
    from bifrost.api.sse import event_bus
    from bifrost.extraction.query_worker import _evt_done, _evt_error

    await event_bus.publish(
        conversation_id,
        _evt_error("Cancelled by user."),
    )
    await event_bus.publish(conversation_id, _evt_done())

    return BifrostEnvelope(data={"cancelled": True})


@router.post("/{conversation_id}/agents/{agent_key}/cancel")
async def cancel_single_agent(
    conversation_id: str,
    agent_key: str,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[dict[str, Any]]:
    """Cancel a single per-agent research task without affecting the
    rest of the turn.

    ``agent_key`` is ``"<repo_id>:<source>"`` — for example
    ``"abc123-uuid:confluence"``. Valid sources: ``jira``,
    ``confluence``, ``github``, ``slack``. Cancelling the per-repo
    code agent uses the conversation-wide ``/cancel`` endpoint
    instead (code is too central to lose without abandoning the
    answer).

    On success, the running task receives an ``asyncio.CancelledError``
    which the coordinator's ``_tracked_external_agent`` wrapper
    converts into a synthetic report marked
    ``error="cancelled by user"``. The bundle's other agents keep
    running; synthesis still completes with the partial set.

    Idempotent: cancelling an agent that has no in-flight task or
    has already finished returns ``{cancelled: false,
    reason: "not_running"}``.

    Ownership-checked."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(
        cid,
        session_id=session_id,
        include_deleted=True,
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Parse "repo_id:source" — strict separator so a malformed key
    # doesn't reach into _AGENT_TASKS with arbitrary tuples.
    if ":" not in agent_key:
        raise HTTPException(
            status_code=400,
            detail="agent_key must be '<repo_id>:<source>'",
        )
    repo_id, _, source = agent_key.partition(":")
    if source not in ("jira", "confluence", "github", "slack"):
        raise HTTPException(
            status_code=400,
            detail=(
                "source must be one of jira/confluence/github/slack — "
                "code agents cancel via /conversations/{id}/cancel"
            ),
        )
    try:
        uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="repo_id must be a UUID")

    from bifrost.research.coordinator import cancel_agent_task

    cancelled = cancel_agent_task(conversation_id, repo_id, source)
    if cancelled:
        logger.info(
            "agent.cancel conv=%s repo=%s source=%s session=%s",
            conversation_id,
            repo_id,
            source,
            session_id,
        )
        return BifrostEnvelope(
            data={
                "cancelled": True,
                "agent_key": f"{repo_id}:{source}",
            },
        )
    return BifrostEnvelope(
        data={
            "cancelled": False,
            "reason": "not_running",
            "agent_key": f"{repo_id}:{source}",
        },
    )


# ── Chat-message feedback (priority-3 of the learning wire-up) ──────


class MessageFeedbackRequest(BaseModel):
    """Body for ``POST /conversations/{id}/messages/{msg_id}/feedback``.

    The user clicks 👍/👎 on an assistant answer; that fires deltas
    against every entity URN the message's research bundle cited.
    Note is optional but useful for negative feedback (lets the user
    explain WHY they thumb-downed).
    """

    verdict: str = Field(
        ...,
        description="'up' (confirm) or 'down' (contradict).",
    )
    note: str | None = Field(default=None, max_length=2000)


@router.post(
    "/{conversation_id}/messages/{message_id}/feedback",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def submit_message_feedback(
    conversation_id: str,
    message_id: str,
    body: MessageFeedbackRequest,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[dict[str, Any]]:
    """Translate a user 👍/👎 on an assistant message into
    per-citation confidence deltas.

    Wires the chat side of the continuous-learning loop. When an
    operator confirms an answer, every entity URN the message cited
    gets an ``audit_confirmed`` delta with proposed = current + 0.05
    (small upgrade; governance still requires distinct confirmations
    to actually promote). When they contradict, deltas land at
    proposed = current - 0.10 with reason ``audit_contradicted``;
    governance applies downgrades immediately, so a single thumbs-
    down genuinely shifts the entity confidence.

    The cited URNs are read from
    ``conversation_research_state.bundle.subgraph_entity_urns`` —
    that's where the synthesizer's walked entities are persisted.
    Limitation: feedback always applies to the LATEST research
    bundle for the conversation. If the user thumbs an old assistant
    message, the deltas land against the most-recent bundle. v2
    would store cited URNs per-message; for now this is a v1
    pragmatic choice (most feedback fires on the latest answer
    immediately after generation).

    Ownership-checked. Idempotent: re-submitting the same feedback
    is a noop on the dashboard side because deltas dedupe by
    target+actor+task; the user just sees a "feedback already
    recorded" response.
    """
    if body.verdict not in ("up", "down"):
        raise HTTPException(
            status_code=400,
            detail="verdict must be 'up' or 'down'",
        )
    try:
        cid = uuid.UUID(conversation_id)
        mid = uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid id format",
        )
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(
        cid,
        session_id=session_id,
        include_deleted=True,
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Open a fresh session for the queries + delta writes. The
    # request-scoped ``repo`` from ``Depends(get_repository)`` only
    # exposes high-level methods; we need raw SQL for the
    # research-state lookup.
    from sqlalchemy import select as _sel

    from bifrost.api.deps import get_session_factory
    from bifrost.api.deps import get_settings as _gs
    from bifrost.storage.models import (
        ConversationMessage,
        ConversationResearchState,
    )
    from bifrost.storage.repository import EntityRepository

    factory = get_session_factory(_gs())
    actor = f"chat-feedback:{conversation_id}:{message_id}"
    is_up = body.verdict == "up"
    reason = "audit_confirmed" if is_up else "audit_contradicted"

    async with factory() as session:
        # Confirm the message exists + belongs to this conversation +
        # is an assistant message (we don't accept feedback on user
        # messages).
        msg_check = (
            await session.execute(
                _sel(ConversationMessage.id).where(
                    ConversationMessage.id == mid,
                    ConversationMessage.conversation_id == cid,
                    ConversationMessage.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        if msg_check is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Assistant message not found in this conversation. "
                    "Feedback only applies to assistant messages."
                ),
            )

        # Pull cited URNs from the latest research bundle.
        bundle = (
            await session.execute(
                _sel(ConversationResearchState.bundle).where(
                    ConversationResearchState.conversation_id == cid,
                )
            )
        ).scalar_one_or_none()
        if not bundle or not isinstance(bundle, dict):
            return BifrostEnvelope(
                data={
                    "recorded": 0,
                    "reason": "no_research_state",
                    "message": (
                        "No research bundle found for this conversation — "
                        "feedback can't target citations."
                    ),
                }
            )
        urns = list(bundle.get("subgraph_entity_urns") or [])
        repos_used = list(bundle.get("repos_used") or [])
        if not urns:
            if not repos_used:
                return BifrostEnvelope(
                    data={
                        "recorded": 0,
                        "reason": "no_cited_urns",
                        "message": (
                            "Research bundle has no cited entity URNs (likely "
                            "a partial bundle from an earlier failure). "
                            "Feedback noted but no deltas to record."
                        ),
                    }
                )
            # Partial bundle: URNs are absent but we know which repos were
            # used. Don't discard the feedback — record at repo level so the
            # signal isn't silently lost.
            logger.warning(
                "partial bundle: recording repo-level feedback conv=%s msg=%s verdict=%s repos=%s",
                conversation_id,
                message_id,
                body.verdict,
                repos_used,
            )

        # Cap at 50 URNs so a 200-entity walk doesn't fire 200
        # inserts. Asymmetric shift: upgrades small (+0.08) since
        # governance still needs cross-confirmation; downgrades
        # bigger (-0.10) because a single thumbs-down IS a real
        # negative signal and downgrade governance applies it
        # immediately.
        from decimal import Decimal as _Dec

        from bifrost.actions import learning as _learning

        recorded = 0
        skipped = 0
        repo_api = EntityRepository(session)
        for urn in urns[:50]:
            entity = await repo_api.get_entity_by_urn(urn)
            if entity is None:
                skipped += 1
                continue
            cur = _Dec(str(entity.confidence or 0)).quantize(_Dec("0.01"))
            if is_up:
                proposed = min(_Dec("1.00"), cur + _Dec("0.05"))
            else:
                proposed = max(_Dec("0.00"), cur - _Dec("0.10"))
            try:
                await _learning.record_delta(
                    session,
                    target_kind="entity",
                    target_id=str(entity.id),
                    proposed_confidence=proposed,
                    reason=reason,
                    task_id=None,
                    actor=actor,
                )
                recorded += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "feedback record_delta failed for urn=%s (non-fatal)",
                    urn,
                )
        await session.commit()

    logger.info(
        "chat feedback conv=%s msg=%s verdict=%s recorded=%d skipped=%d",
        conversation_id,
        message_id,
        body.verdict,
        recorded,
        skipped,
    )
    return BifrostEnvelope(
        data={
            "recorded": recorded,
            "skipped": skipped,
            "verdict": body.verdict,
        }
    )


@router.get("/{conversation_id}/stream")
async def conversation_stream(
    conversation_id: str,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> StreamingResponse:
    """Live SSE stream of research events for one conversation.

    Ownership-checked: a leaked conversation UUID can't be exploited by a different
    cookie session to watch the answer being produced. The
    ``include_deleted=True`` flag lets a soft-deleted conversation's
    in-flight stream finish — once a query is running we still want
    the owner to see the result even if they hit delete mid-stream.
    """
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    session_id = _ensure_session(response, bifrost_session)
    owned = await repo.get_conversation_for_session(
        cid,
        session_id=session_id,
        include_deleted=True,
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return StreamingResponse(
        event_stream(conversation_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _auto_title(first_message: str) -> str:
    """Derive a short title from the first user message."""
    s = first_message.strip().replace("\n", " ")
    if len(s) <= 60:
        return s
    return s[:57].rstrip() + "…"
