"""Collaboration session endpoints.

The interaction substrate for cross-team feature requests. The
initial version ships bare CRUD over sessions / messages / doubts / artifacts so
the UI can drive a session end-to-end; the clarifier and
synthesizer agents land in a follow-up commit and plug in via
``POST /collab-sessions/{id}/messages`` and the close handler.

See ``docs/handbook/07-collaboration-system.md``.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from bifrost.api._cookie_signing import mint_signed_cookie, parse_signed_cookie
from bifrost.api.deps import DB, AdminOnly, Repo, Settings
from bifrost.api.schemas.collaboration import (
    AppendCollabMessageRequest,
    CloseCollabSessionRequest,
    CollabArtifactData,
    CollabDoubtData,
    CollabMessageData,
    CollabSessionDetail,
    CollabSessionSummary,
    CreateCollabSessionData,
    CreateCollabSessionRequest,
    ResolveDoubtRequest,
)
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.sse import event_stream
from bifrost.cli.config import get_settings
from bifrost.collaboration.clarifier import run_clarifier
from bifrost.collaboration.events import publish_session_updated
from bifrost.collaboration.slack_escalation import (
    SlackEscalationError,
    post_escalation_to_slack,
    slack_escalation_enabled,
)
from bifrost.collaboration.synthesizer import run_synthesizer
from bifrost.errors import ConflictError
from bifrost.storage.models import (
    CollabArtifact,
    CollabDoubt,
    CollabMessage,
    CollaborationSession,
)
from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/collab-sessions", tags=["collaboration"])


SESSION_COOKIE_NAME = "bifrost_session"


def _ensure_cookie(response: Response, session: str | None) -> str:
    """Return the caller's cookie session id (NOT a CollaborationSession id),
    setting the cookie if missing. Same identity model as conversations.

    Only a cookie whose signature validates is honoured; anything else
    gets a fresh signed session.
    """
    parsed = parse_signed_cookie(session)
    if parsed is not None:
        return parsed
    new_value = mint_signed_cookie()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        new_value,
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="lax",
    )
    # Return the unsigned id (everything before the first ``.``) so
    # downstream code (audit actor, rate-limit key) sees a stable
    # identifier without the signature half.
    return new_value.split(".", 1)[0]


# ── Session creation + listing ───────────────────────────────────────


@router.post(
    "",
    response_model=BifrostEnvelope[CreateCollabSessionData],
    status_code=status.HTTP_201_CREATED,
)
async def create_collab_session(
    body: CreateCollabSessionRequest,
    response: Response,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[CreateCollabSessionData]:
    cookie_session = _ensure_cookie(response, bifrost_session)
    requester = (body.requester or cookie_session).strip()
    requirement = body.requirement_text.strip()
    if not requirement:
        raise HTTPException(
            status_code=400,
            detail="requirement_text cannot be blank",
        )
    sess = await repo.create_collab_session(
        requester=requester,
        requirement_text=requirement,
    )
    # Seed the chat with the requester's opening message so the
    # clarifier has a turn-zero to react to.
    await repo.append_collab_message(
        session_id=sess.id,
        role="requester",
        content_md=requirement,
    )
    await publish_session_updated(sess.id, kind="requester_message")
    # Fire the clarifier inline against turn-zero so the session
    # detail GET that follows the navigate() already has the agent's
    # response + first-pass doubts in it. ~3-5s wall; users see a
    # ``Starting…`` button state during.
    await _run_clarifier_safely(sess.id, repo)
    return BifrostEnvelope(data=CreateCollabSessionData(id=str(sess.id)))


@router.get(
    "",
    response_model=BifrostEnvelope[list[CollabSessionSummary]],
)
async def list_collab_sessions(
    response: Response,
    repo: Repo,
    db: DB,
    bifrost_session: str | None = Cookie(default=None),
    state: str | None = None,
    requester: str | None = None,
    limit: int = 50,
) -> BifrostEnvelope[list[CollabSessionSummary]]:
    _ensure_cookie(response, bifrost_session)
    sessions = await repo.list_collab_sessions(
        requester=requester,
        state=state,
        limit=limit,
    )
    # For each session, count linked tasks
    link_counts: dict[str, tuple[int, list[str]]] = {}
    if sessions:
        link_counts_result = await db.execute(
            sa_text("""
                SELECT stl.session_id::text, COUNT(*) AS cnt,
                       COALESCE(array_agg(t.jira_key) FILTER (WHERE t.jira_key IS NOT NULL), ARRAY[]::text[]) AS keys
                FROM session_task_links stl
                LEFT JOIN tasks t ON t.id = stl.task_id
                WHERE stl.session_id = ANY(CAST(:ids AS uuid[]))
                GROUP BY stl.session_id
            """),
            {"ids": [str(s.id) for s in sessions]},
        )
        link_counts = {
            row["session_id"]: (row["cnt"], row["keys"]) for row in link_counts_result.mappings()
        }
    out: list[CollabSessionSummary] = []
    for s in sessions:
        # Cheap counts via the existing list helpers; if N gets large
        # we'll add a single-row aggregate query. Expect <100s.
        msgs = await repo.list_collab_messages(s.id)
        doubts = await repo.list_collab_doubts(s.id)
        out.append(
            _to_summary(
                s,
                len(msgs),
                sum(1 for d in doubts if d.status == "open"),
                linked_task_count=link_counts.get(str(s.id), (0, []))[0],
                linked_task_keys=link_counts.get(str(s.id), (0, []))[1],
            )
        )
    return BifrostEnvelope(data=out)


# ── Session detail ───────────────────────────────────────────────────


@router.get(
    "/{session_id}",
    response_model=BifrostEnvelope[CollabSessionDetail],
)
async def get_collab_session(
    session_id: str,
    repo: Repo,
) -> BifrostEnvelope[CollabSessionDetail]:
    sid = _parse_uuid(session_id)
    sess = await repo.get_collab_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await repo.list_collab_messages(sid)
    doubts = await repo.list_collab_doubts(sid)
    artifacts = await repo.list_collab_artifacts(sid)
    detail = CollabSessionDetail(
        **_to_summary(
            sess, len(messages), sum(1 for d in doubts if d.status == "open")
        ).model_dump(),
        messages=[_msg_to_data(m) for m in messages],
        doubts=[_doubt_to_data(d) for d in doubts],
        artifacts=[_artifact_to_data(a) for a in artifacts],
        brief_md=_read_final_brief(artifacts),
    )
    return BifrostEnvelope(data=detail)


# ── Append message ───────────────────────────────────────────────────


@router.post(
    "/{session_id}/messages",
    response_model=BifrostEnvelope[CollabMessageData],
    status_code=status.HTTP_201_CREATED,
)
async def append_message(
    session_id: str,
    body: AppendCollabMessageRequest,
    repo: Repo,
) -> BifrostEnvelope[CollabMessageData]:
    sid = _parse_uuid(session_id)
    sess = await repo.get_collab_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    if sess.state in ("briefed", "closed"):
        raise HTTPException(
            status_code=409,
            detail=f"session is {sess.state}; reopen before appending",
        )
    doubt_uuid: uuid.UUID | None = None
    if body.doubt_id:
        doubt_uuid = _parse_uuid(body.doubt_id)
    msg = await repo.append_collab_message(
        session_id=sid,
        role="requester",
        content_md=body.content_md,
        source_citations=body.source_citations,
        doubt_id=doubt_uuid,
    )
    # Flip the state machine forward on the first follow-up turn.
    if sess.state == "open":
        await repo.update_collab_session_state(sid, "clarifying")
    # Surface the requester turn to live subscribers immediately so
    # the chat doesn't sit silent until the (slow) clarifier finishes.
    await publish_session_updated(sid, kind="requester_message")
    # Run the clarifier inline so the next GET already includes the
    # agent's response. The clarifier itself decides whether to ask
    # more questions or mark the requirement concrete + advance state.
    await _run_clarifier_safely(sid, repo)
    return BifrostEnvelope(data=_msg_to_data(msg))


# ── Doubts ───────────────────────────────────────────────────────────


@router.post(
    "/{session_id}/doubts/{doubt_id}/resolve",
    response_model=BifrostEnvelope[CollabDoubtData],
)
async def resolve_doubt(
    session_id: str,
    doubt_id: str,
    body: ResolveDoubtRequest,
    repo: Repo,
) -> BifrostEnvelope[CollabDoubtData]:
    sid = _parse_uuid(session_id)
    did = _parse_uuid(doubt_id)
    sess = await repo.get_collab_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    if body.status == "open":
        raise HTTPException(
            status_code=400,
            detail="cannot resolve a doubt back to 'open'",
        )
    existing = await repo.get_collab_doubt(did, for_update=True)
    if existing is None or existing.session_id != sid:
        raise HTTPException(status_code=404, detail="doubt not found")
    if body.status == "escalated" and existing.status == "escalated":
        raise ConflictError(
            "doubt is already escalated",
            code="collab.doubt.already_escalated",
            details={"doubt_id": str(did)},
        )
    doubt = await repo.resolve_collab_doubt(
        did,
        status=body.status,
        resolved_md=body.resolved_md,
        source_ref=body.source_ref,
    )
    if doubt is None:
        raise HTTPException(status_code=404, detail="doubt not found")

    # Materialize the draft_ask_message artifact, then post it to Slack when
    # channel + bot token are set. A Slack failure is raised after publishing.
    slack_error: SlackEscalationError | None = None
    if body.status == "escalated":
        materialized = await _materialize_escalation_draft(sess, doubt, repo)
        if materialized is not None:
            try:
                await _maybe_auto_post_to_slack(materialized, sess, doubt)
            except SlackEscalationError as exc:
                slack_error = exc

    await publish_session_updated(sid, kind="doubt_resolved")
    if slack_error is not None:
        raise slack_error
    return BifrostEnvelope(data=_doubt_to_data(doubt))


async def _maybe_auto_post_to_slack(
    materialized: tuple[Path, str],
    sess: CollaborationSession,
    doubt: CollabDoubt,
) -> None:
    """Post the escalation draft to the configured Slack channel.

    No-op unless the channel is set. Raises ``SlackEscalationError`` when
    Slack fails; the on-disk draft stays the fallback for a manual paste.
    """
    settings = get_settings()
    channel = (settings.slack_escalation_channel or "").strip()
    if not channel:
        return
    if not slack_escalation_enabled(settings):
        logger.warning(
            "slack_escalation_channel is set but BIFROST_SLACK_BOT_TOKEN is not; "
            "skipping Slack post session=%s doubt=%s",
            sess.id,
            doubt.id,
        )
        return

    fs_path, body_md = materialized
    permalink = await post_escalation_to_slack(
        channel=channel,
        body_md=body_md,
        settings=settings,
    )
    footer = f"\n---\n\n_Posted to Slack: {permalink}_\n"
    try:
        with fs_path.open("a", encoding="utf-8") as fh:
            fh.write(footer)
    except OSError as exc:
        logger.warning(
            "couldn't append Slack permalink to artifact %s: %s",
            fs_path,
            exc,
        )


async def _materialize_escalation_draft(
    sess: CollaborationSession,
    doubt: CollabDoubt,
    repo: EntityRepository,
) -> tuple[Path, str] | None:
    """Create a Markdown ``draft_ask_message`` artifact for an
    escalated doubt. The draft is templated from the doubt + session
    metadata; no LLM call needed.

    Returns ``(fs_path, body_md)`` so the auto-post wrapper
    can re-use the rendered body without re-reading from disk and can
    append a permalink footer in one shot. Returns ``None`` only if
    the on-disk write itself failed (callers treat that as a hard
    failure of the materialize step — no Slack post follows)."""
    matched = []
    for raw in sess.matched_doc_ids or []:
        try:
            d = await repo.get_handover_doc(uuid.UUID(str(raw)))
            if d is not None:
                matched.append(d)
        except (ValueError, AttributeError):
            continue
    teams_md = (
        "\n".join(f"- **{m.scope_kind}/{m.scope_value}**" for m in matched)
        if matched
        else "_(no handover docs matched yet)_"
    )

    body = (
        f"Hi team 👋\n\n"
        f"We're working on this requirement:\n\n"
        f"> {sess.requirement_text}\n\n"
        f"…and ran into a question your handover docs don't yet "
        f"answer:\n\n"
        f"> {doubt.question}\n\n"
        f"Matched scope(s):\n{teams_md}\n\n"
        f"Could someone point us to the right person, or post a "
        f"short answer here? Once we have it I can drop it into "
        f"the relevant handover so future asks resolve from the "
        f"docs.\n\n"
        f"Session: `{sess.id}`\n"
    )

    # Write to disk under sessions/<id>/escalation-<short>.md so the
    # GET /brief.md downloader doesn't conflict.
    base = os.environ.get("BIFROST_HANDOVERS_DIR", "docs/handovers")
    out_dir = Path(base) / "sessions" / str(sess.id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        fs_path = out_dir / f"escalation-{str(doubt.id)[:8]}.md"
        fs_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "couldn't write escalation draft for session=%s doubt=%s: %s",
            sess.id,
            doubt.id,
            exc,
        )
        return None

    await repo.add_collab_artifact(
        session_id=sess.id,
        kind="draft_ask_message",
        fs_path=str(fs_path),
    )
    logger.info(
        "escalation draft persisted session=%s doubt=%s path=%s",
        sess.id,
        doubt.id,
        fs_path,
    )
    return (fs_path, body)


# ── Close ────────────────────────────────────────────────────────────


@router.post(
    "/{session_id}/close",
    response_model=BifrostEnvelope[CollabSessionDetail],
)
async def close_collab_session(
    session_id: str,
    body: CloseCollabSessionRequest,
    repo: Repo,
) -> BifrostEnvelope[CollabSessionDetail]:
    sid = _parse_uuid(session_id)
    sess = await repo.get_collab_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    open_doubts = [d for d in await repo.list_collab_doubts(sid) if d.status == "open"]
    if open_doubts and not body.accept_open_doubts_as_risks:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(open_doubts)} open doubt(s) — pass "
                "accept_open_doubts_as_risks=true to brief them as risks"
            ),
        )
    # Accept remaining opens as unresolved risks (they'll appear in the
    # brief's "Open risks" section once the synthesizer agent runs).
    for d in open_doubts:
        await repo.resolve_collab_doubt(
            d.id,
            status="unresolved",
            resolved_md="Accepted as open risk at session close",
        )
    # Park at ``resolved`` first so any failure during synthesis leaves
    # an honest state behind (the session IS resolved; only the brief
    # artifact failed). Synthesizer flips to ``briefed`` on success.
    await repo.update_collab_session_state(sid, "resolved", resolved=True)
    await publish_session_updated(sid, kind="state_resolved")
    await _run_synthesizer_safely(sid, repo)
    return await get_collab_session(session_id, repo)


# ── Reopen ───────────────────────────────────────────────────────────


@router.post(
    "/{session_id}/reopen",
    response_model=BifrostEnvelope[CollabSessionDetail],
)
async def reopen_collab_session(
    session_id: str,
    repo: Repo,
) -> BifrostEnvelope[CollabSessionDetail]:
    """Re-engage a closed session.

    Flips ``briefed``/``resolved``/``closed`` → ``clarifying`` so the
    composer comes back and the requester can ask follow-ups. Existing
    messages, doubts, and the brief artifact are preserved — only the
    state changes. Re-running close from this point will overwrite the
    brief with a fresh one (acceptable: the synthesizer is idempotent
    in spirit, just rewriting the same file).
    """
    sid = _parse_uuid(session_id)
    sess = await repo.get_collab_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    if sess.state not in ("briefed", "resolved", "closed"):
        raise HTTPException(
            status_code=409,
            detail=f"session is {sess.state}; reopen is for terminal states only",
        )
    await repo.update_collab_session_state(sid, "clarifying")
    await publish_session_updated(sid, kind="reopened")
    return await get_collab_session(session_id, repo)


# ── SSE stream ───────────────────────────────────────────────────────


@router.get("/{session_id}/stream")
async def stream_collab_session(session_id: str, repo: Repo) -> StreamingResponse:
    """Subscribe to live events for one collaboration session.

    Frontend uses this to react to clarifier/synthesizer turns
    landing without polling. Reuses the application-wide
    ``TaskEventBus`` (its key namespace is just opaque strings —
    we pass ``str(session_id)``). No special heartbeat needed;
    sessions are short-lived and EventSource auto-reconnects
    against the bus's replay buffer.

    Validates that the session exists so a typo'd URL returns 404
    instead of opening a connection that never receives anything.
    """
    sid = _parse_uuid(session_id)
    if await repo.get_collab_session(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return StreamingResponse(
        event_stream(str(sid)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
        },
    )


# ── Session-task linking ─────────────────────────────────────────────


class LinkTaskBody(BaseModel):
    task_id: str


@router.post("/{session_id}/link-task", status_code=201)
async def link_task_to_session(
    session_id: str, body: LinkTaskBody, db: DB
) -> BifrostEnvelope[dict]:
    """Link a Bifrost task to a collaboration session."""
    await db.execute(
        sa_text("""
            INSERT INTO session_task_links (session_id, task_id)
            VALUES (CAST(:sid AS uuid), CAST(:tid AS uuid))
            ON CONFLICT (session_id, task_id) DO NOTHING
        """),
        {"sid": session_id, "tid": body.task_id},
    )
    return BifrostEnvelope(data={"linked": True})


@router.get("/{session_id}/tasks")
async def get_session_tasks(
    session_id: str, db: DB, settings: Settings
) -> BifrostEnvelope[list[dict]]:
    """Return tasks linked to a collaboration session."""
    rows = (
        (
            await db.execute(
                sa_text("""
            SELECT t.id::text, t.title, t.state, t.jira_key,
                   t.pr_url, t.pr_number, t.updated_at, stl.linked_at
            FROM session_task_links stl
            JOIN tasks t ON t.id = stl.task_id
            WHERE stl.session_id = CAST(:sid AS uuid)
            ORDER BY stl.linked_at DESC
        """),
                {"sid": session_id},
            )
        )
        .mappings()
        .all()
    )
    return BifrostEnvelope(
        data=[{**r, "jira_url": settings.sources.jira.browse_url(r["jira_key"])} for r in rows]
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid uuid") from exc


def _to_summary(
    s: CollaborationSession,
    message_count: int,
    open_doubt_count: int,
    linked_task_count: int = 0,
    linked_task_keys: list[str] | None = None,
) -> CollabSessionSummary:
    return CollabSessionSummary(
        id=str(s.id),
        requester=s.requester,
        state=s.state,  # type: ignore[arg-type]
        requirement_text=s.requirement_text,
        matched_doc_ids=[str(x) for x in (s.matched_doc_ids or [])],
        created_at=s.created_at,
        updated_at=s.updated_at,
        resolved_at=s.resolved_at,
        message_count=message_count,
        open_doubt_count=open_doubt_count,
        linked_task_count=linked_task_count,
        linked_task_keys=linked_task_keys or [],
    )


def _msg_to_data(m: CollabMessage) -> CollabMessageData:
    return CollabMessageData(
        id=str(m.id),
        session_id=str(m.session_id),
        ord=m.ord,
        role=m.role,  # type: ignore[arg-type]
        content_md=m.content_md,
        source_citations=list(m.source_citations or []),
        doubt_id=str(m.doubt_id) if m.doubt_id else None,
        created_at=m.created_at,
    )


def _doubt_to_data(d: CollabDoubt) -> CollabDoubtData:
    return CollabDoubtData(
        id=str(d.id),
        session_id=str(d.session_id),
        question=d.question,
        status=d.status,  # type: ignore[arg-type]
        resolved_md=d.resolved_md,
        source_ref=d.source_ref,
        created_at=d.created_at,
        resolved_at=d.resolved_at,
    )


def _artifact_to_data(a: CollabArtifact) -> CollabArtifactData:
    return CollabArtifactData(
        id=str(a.id),
        session_id=str(a.session_id),
        kind=a.kind,  # type: ignore[arg-type]
        fs_path=a.fs_path,
        created_at=a.created_at,
    )


async def _run_clarifier_safely(
    session_id: uuid.UUID,
    repo: EntityRepository,
) -> None:
    """Run the clarifier and swallow any unexpected exceptions.

    The clarifier itself already returns a fallback turn for
    LLM-side failures, but we belt-and-brace it here so a freak
    code path (e.g. DB hiccup mid-write) can never break the user's
    POST. Worst case: no clarifier turn is appended this round; the
    session is still usable, the next requester message will
    re-trigger the clarifier."""
    try:
        await run_clarifier(session_id=session_id, repo=repo)
    except Exception:  # noqa: BLE001 — last-mile guard
        logger.exception(
            "clarifier crashed unexpectedly for session=%s; continuing",
            session_id,
        )


@router.get("/{session_id}/brief.md")
async def download_brief_markdown(
    session_id: str,
    repo: Repo,
) -> Response:
    """Stream the session's final brief as a downloadable Markdown
    file. Users can ⌘+P → "Save as PDF" from the
    browser to get a PDF without us shipping a heavy PDF library.

    Returns 404 if the session has no ``final_brief`` artifact yet
    (i.e. it hasn't been closed/briefed) or the file went missing
    on disk."""
    sid = _parse_uuid(session_id)
    sess = await repo.get_collab_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    artifacts = await repo.list_collab_artifacts(sid)
    body = _read_final_brief(artifacts)
    if body is None:
        raise HTTPException(
            status_code=404,
            detail="no brief available — close the session to produce one",
        )
    # Include the title in the filename so it lands somewhere
    # recognizable in the user's Downloads folder.
    filename = f"brief-{session_id[:8]}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _read_final_brief(artifacts: list[CollabArtifact]) -> str | None:
    """Return the Markdown body of the most-recent ``final_brief``
    artifact, or ``None`` if none exist or the file can't be read.

    We tolerate missing/unreadable files because the user might have
    moved or deleted them out of band; surfacing ``None`` lets the UI
    render an honest "brief artifact missing on disk" message instead
    of the API 500-ing the whole detail fetch."""
    briefs = [a for a in artifacts if a.kind == "final_brief"]
    if not briefs:
        return None
    # Most recent wins if multiple (e.g., user re-closed after editing).
    latest = max(briefs, key=lambda a: a.created_at)
    try:
        return Path(latest.fs_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "couldn't read brief artifact at %s: %s",
            latest.fs_path,
            exc,
        )
        return None


async def _run_synthesizer_safely(
    session_id: uuid.UUID,
    repo: EntityRepository,
) -> None:
    """Run the synthesizer at session close, with the same belt-and-brace
    pattern as the clarifier wrapper. The synthesizer itself writes a
    fallback system turn on LLM failure (and leaves state at ``resolved``
    rather than ``briefed``); this layer only catches the rare freak
    exceptions so a synthesis crash can never break the user's close."""
    try:
        await run_synthesizer(session_id=session_id, repo=repo)
    except Exception:  # noqa: BLE001
        logger.exception(
            "synthesizer crashed unexpectedly for session=%s; "
            "session left at resolved (no brief artifact)",
            session_id,
        )


# ────────────────────────────────────────────────────────────────────
# Collaboration primitives — five routes that turn
# the page from a niche cross-team-request workflow into the daily
# coordination surface. Each primitive is a thin route over a dedicated
# ``actions/*`` aggregator. All five are READ-ONLY except for asks
# (which has its own write surface).
# ────────────────────────────────────────────────────────────────────


from dataclasses import asdict

from fastapi import Query
from pydantic import BaseModel

from bifrost.actions.asks_router import (
    cancel_ask,
    create_ask,
    get_ask,
    list_asks,
    mark_answered,
    mark_escalated,
    mark_sent,
)
from bifrost.actions.domain_hub import build_domain_hub
from bifrost.actions.personal_inbox import build_inbox
from bifrost.actions.review_queue import build_review_queue
from bifrost.actions.squad_pulse import (
    build_pulse,
    render_pulse_section_markdown,
)
from bifrost.actions.standup_gen import build_standup

# Mounted as a separate router at ``/collaboration`` so the new
# primitives have a clean URL space (``/collaboration/pulse`` etc.)
# while the existing session/handover surface keeps its
# ``/collab-sessions`` prefix.
collab_primitives_router = APIRouter(
    prefix="/collaboration",
    tags=["collaboration"],
)


# ── P1 — Squad Pulse ───────────────────────────────────────────────


@collab_primitives_router.get("/pulse")
async def get_pulse(
    db: DB,
    squad: str | None = Query(default=None),
    domain_id: str | None = Query(default=None),
    repo_id: str | None = Query(default=None),
    range_days: int = Query(default=7, ge=1, le=90),
) -> BifrostEnvelope[dict]:
    """Per-squad/domain/repo activity rollup for the last
    ``range_days`` days. Read-only; pure aggregation."""
    if not (squad or domain_id or repo_id):
        raise HTTPException(
            400,
            "exactly one of squad / domain_id / repo_id is required",
        )
    report = await build_pulse(
        db,
        squad=squad,
        domain_id=domain_id,
        repo_id=repo_id,
        range_days=range_days,
    )
    if report is None:
        raise HTTPException(404, "scope not found")
    return BifrostEnvelope(data=report.to_json())


@collab_primitives_router.get("/pulse/section-markdown")
async def get_pulse_section_markdown(
    db: DB,
    section: str = Query(...),
    squad: str | None = Query(default=None),
    domain_id: str | None = Query(default=None),
    repo_id: str | None = Query(default=None),
    range_days: int = Query(default=7, ge=1, le=90),
) -> BifrostEnvelope[dict]:
    """Render a single pulse section as markdown — used as
    ``seed_context`` when the operator clicks 'Discuss this section'
    and the FE calls ``POST /conversations/seed``."""
    report = await build_pulse(
        db,
        squad=squad,
        domain_id=domain_id,
        repo_id=repo_id,
        range_days=range_days,
    )
    if report is None:
        raise HTTPException(404, "scope not found")
    md = render_pulse_section_markdown(report, section)
    return BifrostEnvelope(data={"markdown": md})


# ── P2 — Asks Board ────────────────────────────────────────────────


class CreateAskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    scope_repo_ids: list[str] = []
    target_squad_override: str | None = None


class MarkSentRequest(BaseModel):
    slack_thread_url: str | None = None


class MarkAnsweredRequest(BaseModel):
    answer_conversation_id: str | None = None


@collab_primitives_router.post("/asks")
async def create_ask_endpoint(
    body: CreateAskRequest,
    db: DB,
    request: Request,
) -> BifrostEnvelope[dict]:
    """Create a new ask — auto-routes via the ownership graph + drafts
    a Slack message. Asker email comes from the resolved principal.
    Failures in the Haiku draft do NOT fail the call (the row is
    saved; the operator types their own draft if Claude was down)."""
    principal = getattr(request.state, "principal", None)
    asker_email = (getattr(principal, "email", None) if principal else None) or "unknown@bifrost"
    ask_id = await create_ask(
        db,
        asker_email=asker_email,
        question=body.question,
        scope_repo_ids=body.scope_repo_ids,
        target_squad_override=body.target_squad_override,
    )
    await db.flush()
    row = await get_ask(db, ask_id=ask_id)
    if row is None:
        raise HTTPException(500, "created but reload failed")
    return BifrostEnvelope(data=asdict(row))


@collab_primitives_router.get("/asks")
async def list_asks_endpoint(
    db: DB,
    asker_email: str | None = Query(default=None),
    target_squad: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> BifrostEnvelope[list[dict]]:
    rows = await list_asks(
        db,
        asker_email=asker_email,
        target_squad=target_squad,
        status=status,
        limit=limit,
    )
    return BifrostEnvelope(data=[asdict(r) for r in rows])


@collab_primitives_router.post("/asks/{ask_id}/mark-sent")
async def mark_sent_endpoint(
    ask_id: str,
    body: MarkSentRequest,
    db: DB,
) -> BifrostEnvelope[dict]:
    await mark_sent(
        db,
        ask_id=ask_id,
        slack_thread_url=body.slack_thread_url,
    )
    await db.flush()
    row = await get_ask(db, ask_id=ask_id)
    if row is None:
        raise HTTPException(404, "ask not found")
    return BifrostEnvelope(data=asdict(row))


@collab_primitives_router.post("/asks/{ask_id}/mark-answered")
async def mark_answered_endpoint(
    ask_id: str,
    body: MarkAnsweredRequest,
    db: DB,
) -> BifrostEnvelope[dict]:
    await mark_answered(
        db,
        ask_id=ask_id,
        answer_conversation_id=body.answer_conversation_id,
    )
    await db.flush()
    row = await get_ask(db, ask_id=ask_id)
    if row is None:
        raise HTTPException(404, "ask not found")
    return BifrostEnvelope(data=asdict(row))


@collab_primitives_router.post("/asks/{ask_id}/escalate")
async def escalate_endpoint(
    ask_id: str,
    db: DB,
) -> BifrostEnvelope[dict]:
    await mark_escalated(db, ask_id=ask_id)
    await db.flush()
    row = await get_ask(db, ask_id=ask_id)
    if row is None:
        raise HTTPException(404, "ask not found")
    return BifrostEnvelope(data=asdict(row))


@collab_primitives_router.post("/asks/{ask_id}/cancel")
async def cancel_ask_endpoint(
    ask_id: str,
    db: DB,
) -> BifrostEnvelope[dict]:
    await cancel_ask(db, ask_id=ask_id)
    await db.flush()
    row = await get_ask(db, ask_id=ask_id)
    if row is None:
        raise HTTPException(404, "ask not found")
    return BifrostEnvelope(data=asdict(row))


# ── P3 — Code Review Queue ────────────────────────────────────────


@collab_primitives_router.get("/review-queue")
async def get_review_queue(
    db: DB,
    request: Request,
    email: str | None = Query(default=None),
    range_days: int = Query(default=30, ge=1, le=180),
) -> BifrostEnvelope[dict]:
    """Aggregated open PRs that the operator likely cares about.

    When ``email`` is omitted, defaults to the resolved principal.
    """
    if not email:
        principal = getattr(request.state, "principal", None)
        email = getattr(principal, "email", None) if principal else None
    if not email:
        raise HTTPException(
            400,
            "email is required (no identity principal on request)",
        )
    queue = await build_review_queue(
        db,
        email=email,
        range_days=range_days,
    )
    return BifrostEnvelope(data=asdict(queue))


# ── P4 — Standup Generator ────────────────────────────────────────


@collab_primitives_router.get("/standup-draft")
async def get_standup_draft(
    db: DB,
    request: Request,
    email: str | None = Query(default=None),
    range_days: int = Query(default=7, ge=1, le=30),
) -> BifrostEnvelope[dict]:
    """Markdown draft of the operator's weekly status update."""
    if not email:
        principal = getattr(request.state, "principal", None)
        email = getattr(principal, "email", None) if principal else None
    if not email:
        raise HTTPException(400, "email is required")
    draft = await build_standup(
        db,
        email=email,
        range_days=range_days,
    )
    return BifrostEnvelope(data=asdict(draft))


# ── P5 — Domain Knowledge Hub ─────────────────────────────────────


@collab_primitives_router.get("/domain-hub")
async def get_domain_hub(
    db: DB,
    squad: str = Query(...),
    range_days: int = Query(default=30, ge=1, le=180),
) -> BifrostEnvelope[dict]:
    """Onboarding-friendly per-squad summary."""
    hub = await build_domain_hub(
        db,
        squad=squad,
        range_days=range_days,
    )
    return BifrostEnvelope(data=asdict(hub))


# ── Personal inbox — your-actionable-work aggregator ──────────────


@collab_primitives_router.get("/inbox")
async def get_inbox(
    db: DB,
    request: Request,
    email: str | None = Query(default=None),
) -> BifrostEnvelope[dict]:
    """Return ``email``'s actionable inbox — escalated asks, blocked
    tasks, asks routed to the user's squad, their PRs in review, their open work.

    Defaults to the resolved principal when ``email`` is omitted.
    Powers the Overview hero panel + sidebar live counts + Cmd+K
    palette's recents."""
    if not email:
        principal = getattr(request.state, "principal", None)
        email = getattr(principal, "email", None) if principal else None
    if not email:
        raise HTTPException(400, "email is required")
    rep = await build_inbox(db, email=email)
    return BifrostEnvelope(data=asdict(rep))


# ── Slack draft — chat "Next step → Draft Slack message" ───────────


class CreateSlackDraftRequest(BaseModel):
    channel: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=12_000)


@collab_primitives_router.post("/slack-draft", dependencies=[AdminOnly])
async def create_slack_draft_endpoint(
    body: CreateSlackDraftRequest,
) -> BifrostEnvelope[dict]:
    """Create a real Slack *draft* (not a sent message) of ``message``
    in ``channel`` via the operator's authenticated Slack MCP.

    The draft lands in the operator's Slack "Drafts & Sent" — nothing
    is posted until they hit send inside Slack. Best-effort: any
    failure returns ``ok=false`` with a human-readable ``error`` rather
    than a 5xx, so the UI can fall back to clipboard copy honestly.
    """
    from bifrost.collaboration.slack_draft import create_slack_draft

    result = await create_slack_draft(
        channel=body.channel.strip(),
        body_md=body.message,
    )
    return BifrostEnvelope(
        data={
            "ok": result.ok,
            "channel_link": result.channel_link,
            "error": result.error,
        },
    )
