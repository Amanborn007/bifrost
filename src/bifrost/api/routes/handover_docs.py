"""Handover-doc endpoints.

Repo, domain, team and region scopes share one response shape
(same data class, same Markdown on disk, same handover_docs row).
Region mirrors the team variant but joins through ``OrgRepo.region``
instead of ``OrgRepo.squad``. Feature scope is still deferred.

Routes:

  POST  /handover-docs/repos/{repo_id}/generate
        Generate (or regenerate) a handover for one repo. Inline,
        ~30-90s wall (single Sonnet call), blocks until the doc is
        on disk + indexed.

  POST  /handover-docs/domains/{domain_id}/generate
        Generate (or regenerate) a handover for a domain — pools
        entities across all repos linked to the domain. Inline,
        ~60-180s wall depending on repo count.

  POST  /handover-docs/teams/{squad}/generate
        Generate (or regenerate) a handover for a team (squad).
        ``squad`` is URL-path-segmented; typical squad names use
        kebab-case so URL-encoding isn't usually needed.

  POST  /handover-docs/regions/{region}/generate
        Generate (or regenerate) a handover for a geographic
        region (e.g. "eu"). Pools entities across all
        registered repos whose ``OrgRepo.region`` matches.

  GET   /handover-docs
        List handovers; ``scope_kind`` filterable.

  GET   /handover-docs/{doc_id}
        Detail with the on-disk Markdown body eagerly inlined as
        ``body_md`` (mirrors the brief-rendering pattern from
        ``collab-sessions``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from bifrost.api._cookie_signing import parse_signed_cookie
from bifrost.api.auth.middleware import current_principal
from bifrost.api.auth.principal import ROLE_ADMIN
from bifrost.api.deps import AdminOnly, Repo, get_session_factory, require_role
from bifrost.api.schemas.collaboration import (
    CostEstimateData,
    DoubtCandidateData,
    GenerateHandoverData,
    GenerateRepoHandoverData,
    HandoverAuditEntry,
    HandoverBodyHistoryEntry,
    HandoverDocDetail,
    HandoverDocSummary,
    HandoverFeedbackEntry,
    HandoverInboxEntry,
    HandoverSearchHit,
    PatchHandoverBodyRequest,
    PromoteCandidateRequest,
    ReferencingSessionEntry,
    RestoreVersionRequest,
    SectionMetadataEntry,
    SubmitFeedbackRequest,
)
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.sse import event_stream
from bifrost.cli.config import get_settings
from bifrost.collaboration.cost_estimate import (
    CostEstimate,
    estimate_domain_handover_cost,
    estimate_region_handover_cost,
    estimate_repo_handover_cost,
    estimate_team_handover_cost,
)
from bifrost.collaboration.events import (
    handover_channel_key,
    publish_handover_event,
)
from bifrost.collaboration.handover_generator import (
    generate_domain_handover,
    generate_region_handover,
    generate_repo_handover,
    generate_team_handover,
    regenerate_section,
)
from bifrost.collaboration.loopback import find_doubt_candidates_for_handover
from bifrost.collaboration.rate_limit import check_and_consume as _rate_check_and_consume
from bifrost.extraction.cli_client import ClaudeCliError
from bifrost.extraction.secrets import SecretScanner
from bifrost.storage.models import (
    HandoverDoc,
    HandoverSectionFeedback,
)
from bifrost.storage.models import (
    Repo as RepoModel,
)
from bifrost.storage.repository import EntityRepository


def _scrub_body(body_md: str | None, *, sink: str) -> str:
    """Run ``SecretScanner.scan_and_redact`` on user-supplied handover
    Markdown before persisting.

    Sinks: PATCH /body (full body), promote-candidate (answer Markdown),
    body-history snapshots (the soon-to-be-overwritten body), and the
    on-disk write path. ``sink`` is the audit label that appears in
    the scrubber's WARN log so a leak gets attributed to the right
    route. Empty / None input passes through.
    """
    if not body_md:
        return body_md or ""
    return SecretScanner().scan_and_redact(body_md, file_path=sink).redacted_content


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/handover-docs", tags=["handover-docs"])


# Strong references to background tasks.
# CPython's event loop only weak-refs scheduled tasks; without this set,
# fire-and-forget tasks can be GC'd mid-flight, leaving partial state on
# disk and stranded SSE channels. Every ``asyncio.create_task`` for a
# background generation goes through ``_spawn_background`` instead.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Schedule a coroutine on the loop with a strong reference.

    The returned task is stored in ``_BACKGROUND_TASKS`` for the
    duration of its run, then automatically removed via
    ``add_done_callback``. Callers don't need to await — the
    reference lifecycle is automatic.
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


def _to_cost_data(estimate: CostEstimate, ceiling_usd: float) -> CostEstimateData:
    """Wrap a :class:`CostEstimate` into the API schema, marking
    ``over_ceiling`` if the prediction exceeds the configured cap."""
    return CostEstimateData(
        estimated_usd=estimate.estimated_usd,
        reasoning=estimate.reasoning,
        repos_to_generate=estimate.repos_to_generate,
        will_use_tree=estimate.will_use_tree,
        over_ceiling=estimate.estimated_usd > ceiling_usd,
        ceiling_usd=ceiling_usd,
    )


def _verified_session_id(bifrost_session: str | None) -> str | None:
    """Return the verified session id from a signed cookie.

    Wraps :func:`parse_signed_cookie` so route handlers don't need to
    import it directly. Returns ``None`` when the cookie is absent,
    malformed, or fails signature verification — the caller should
    fall back to the ``"anon"`` bucket / leave audit ``actor`` null
    rather than trust the raw value.
    """
    return parse_signed_cookie(bifrost_session)


def _enforce_rate_limit(request: Request) -> JSONResponse | None:
    """Check the per-principal handover-generate rate limit.

    Returns ``None`` when allowed, else a 429 :class:`JSONResponse` with a
    ``Retry-After`` header that the route returns directly. The bucket is
    the authenticated principal's email, so rotating the
    ``bifrost_session`` cookie cannot reset it. The limit is read fresh
    from settings so tests can monkeypatch it.
    """
    settings = get_settings()
    requester = current_principal(request).email.strip().lower()
    allowed, retry_after = _rate_check_and_consume(
        requester=requester,
        limit=settings.handover_rate_limit_per_minute,
    )
    if allowed:
        return None
    return JSONResponse(
        status_code=429,
        content={
            "detail": (f"handover-generate rate limit exceeded — retry in {retry_after}s"),
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


async def _require_admin_for_force(request: Request, force: bool) -> None:
    """``?force=true`` skips the cost ceiling, so it needs the admin role."""
    if force:
        await require_role(ROLE_ADMIN)(request)


# ── List ─────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=BifrostEnvelope[list[HandoverDocSummary]],
)
async def list_handover_docs(
    repo: Repo,
    scope_kind: str | None = None,
    limit: int = 100,
    include_archived: bool = False,
) -> BifrostEnvelope[list[HandoverDocSummary]]:
    """List handover docs.

    archived rows are filtered out by default. Pass
    ``include_archived=true`` to include them — used by the UI's
    "Show archived" toggle so users can restore a doc."""
    docs = await repo.list_handover_docs(
        scope_kind=scope_kind,
        limit=limit,
        include_archived=include_archived,
    )
    return BifrostEnvelope(data=[_to_summary(d) for d in docs])


# ── Search ───────────────────────────────────
#
# Free-text search (tsvector + GIN, baseline migration); bodies are read
# only for snippets. Registered BEFORE ``/{doc_id}`` so ``/search``
# isn't parsed as a UUID.

# Snippet width around the first body match, in characters.
_HANDOVER_SEARCH_SNIPPET_RADIUS = 100


def _build_snippet(body: str, query_lower: str) -> str | None:
    """Return up to ~200 chars of body context around the first
    case-insensitive match of ``query_lower``, or ``None`` if there
    is no body match. The query is lower-cased once by the caller;
    we lower-case the body here to find the index, then slice the
    original ``body`` so original casing/punctuation is preserved."""
    if not body or not query_lower:
        return None
    idx = body.lower().find(query_lower)
    if idx < 0:
        return None
    start = max(0, idx - _HANDOVER_SEARCH_SNIPPET_RADIUS)
    end = min(len(body), idx + len(query_lower) + _HANDOVER_SEARCH_SNIPPET_RADIUS)
    snippet = body[start:end].strip()
    # Collapse whitespace runs (Markdown bodies often have line
    # breaks and indented blocks that read poorly as a flat snippet).
    snippet = " ".join(snippet.split())
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet = snippet + "…"
    return snippet


@router.get(
    "/search",
    response_model=BifrostEnvelope[list[HandoverSearchHit]],
)
async def search_handover_docs(
    repo: Repo,
    q: str = "",
    limit: int = 20,
) -> BifrostEnvelope[list[HandoverSearchHit]]:
    """Free-text search across live (non-archived, non-session)
    handover docs.

    Implementation: a Postgres ``tsvector`` GIN index on the
    ``search_tsv`` generated column (``scope_value || scope_kind``)
    handles the title-side predicate; rows come back ordered by
    ``ts_rank``. The on-disk Markdown body is read for each returned
    hit only to compute a context snippet around the first match —
    body text isn't indexed (it lives under ``fs_path``) so a query
    that hits body-only and not the scope-side won't match. That
    fits the common case (people search by repo / domain / squad
    name) and keeps the route fast.

    Scoring: ``score`` is the raw ``ts_rank`` returned by Postgres.
    Higher is more relevant; absolute magnitudes vary with corpus
    size so the UI should treat it as a relative ordering signal,
    not an absolute number.
    """
    query = (q or "").strip()
    if not query:
        raise HTTPException(
            status_code=400,
            detail="q must be a non-empty query string",
        )
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be positive")
    # Hard cap defensively — clients shouldn't ask for thousands.
    limit = min(limit, 100)

    rows = await repo.search_handover_docs(query=query, limit=limit)

    query_lower = query.lower()
    hits: list[HandoverSearchHit] = []
    for doc, rank in rows:
        body = _read_body(doc) or ""
        # Snippet: first body match wins. Falls back to ``None`` if
        # the body has no occurrence of the literal query string —
        # the UI then shows the scope_value as the obvious match.
        snippet = _build_snippet(body, query_lower) if body else None

        summary = _to_summary(doc)
        hits.append(
            HandoverSearchHit(
                **summary.model_dump(),
                score=float(rank),
                snippet=snippet,
            ),
        )

    return BifrostEnvelope(data=hits)


# ── Owner inbox (team-collab gap closure) ─────────────────────────────
#
# "Things that need my attention" view for a doc owner. Identity is the
# verified cookie session id — we look back through ``handover_audits``
# for any row this actor wrote (``actor=<session_id>``) and surface the
# top N most-recently-touched docs, enriched with the three counters
# that drive the inbox UX (open feedback, sessions referencing, doubt
# candidates). Anonymous calls return an empty list — no actor, nothing
# to attribute.
#
# Registered BEFORE ``/{doc_id}`` so the literal path
# ``/handover-docs/inbox`` doesn't get parsed as a UUID.


@router.get(
    "/inbox",
    response_model=BifrostEnvelope[list[HandoverInboxEntry]],
)
async def get_handover_inbox(
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
    limit: int = 5,
) -> BifrostEnvelope[list[HandoverInboxEntry]]:
    """Top handovers the current cookie session has touched recently.

    Returns up to ``limit`` rows ordered by most-recent audit
    timestamp where ``actor`` matches the verified cookie session id.
    Each row carries:

      * ``open_feedback_count`` — open ``handover_section_feedback``
        rows on this doc.
      * ``referencing_sessions_count`` — collab sessions whose
        ``matched_doc_ids`` contains this doc.
      * ``doubt_candidates_count`` — recurring unresolved doubts that
        the loop-back finder surfaces (default min_occurrences=3).

    Anonymous calls return an empty list — without a verified
    session id we have no "owns/touched" signal to filter on.
    """
    actor = _verified_session_id(bifrost_session)
    if not actor:
        return BifrostEnvelope(data=[])

    capped = max(1, min(limit, 50))
    docs = await repo.list_handover_inbox(actor=actor, limit=capped)

    out: list[HandoverInboxEntry] = []
    for doc in docs:
        # Three small per-doc aggregates. Each is index-only:
        #   - count_open_section_feedback hits the partial index.
        #   - count_sessions_referencing_doc hits a JSONB containment.
        #   - find_doubt_candidates_for_handover is bounded; we only
        #     consume the count here, the full list lives behind the
        #     dedicated /candidates endpoint.
        open_fb = await repo.count_open_section_feedback(doc.id)
        ref_count = await repo.count_sessions_referencing_doc(doc.id)
        candidates = await find_doubt_candidates_for_handover(
            repo=repo,
            handover_doc_id=doc.id,
        )
        last_touched_at = getattr(
            doc,
            "__bifrost_last_touched_at__",
            doc.updated_at,
        )
        out.append(
            HandoverInboxEntry(
                id=str(doc.id),
                scope_kind=doc.scope_kind,
                scope_value=doc.scope_value,
                freshness_state=doc.freshness_state,
                open_feedback_count=open_fb,
                referencing_sessions_count=ref_count,
                doubt_candidates_count=len(candidates),
                last_touched_at=last_touched_at,
            ),
        )
    return BifrostEnvelope(data=out)


# ── Detail ───────────────────────────────────────────────────────────


@router.get(
    "/{doc_id}",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def get_handover_doc(
    doc_id: str,
    repo: Repo,
) -> BifrostEnvelope[HandoverDocDetail]:
    did = _parse_uuid(doc_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    summary = _to_summary(doc)
    body = _read_body(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=body)
    return BifrostEnvelope(data=detail)


# ── Cost estimate ───────────────────────────────────

# Pre-flight cost estimates for each generate endpoint. The UI fetches
# these before clicking "Generate" so it can:
#   * Skip friction when the run is cheap (≤ $1).
#   * Show an explicit confirm dialog at higher cost.
#   * Detect a 409 from generate (ceiling exceeded) and prompt for an
#     override that re-issues the call with ``?force=true``.
# Estimates are coarse — see :mod:`bifrost.collaboration.cost_estimate`.


@router.get(
    "/repos/{repo_id}/estimate",
    response_model=BifrostEnvelope[CostEstimateData],
)
async def estimate_repo_handover_route(
    repo_id: str,
    repo: Repo,
) -> BifrostEnvelope[CostEstimateData]:
    rid = _parse_uuid(repo_id)
    settings = get_settings()
    try:
        est = await estimate_repo_handover_cost(repo_id=rid, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=_to_cost_data(est, settings.handover_max_cost_usd),
    )


@router.get(
    "/domains/{domain_id}/estimate",
    response_model=BifrostEnvelope[CostEstimateData],
)
async def estimate_domain_handover_route(
    domain_id: str,
    repo: Repo,
) -> BifrostEnvelope[CostEstimateData]:
    did = _parse_uuid(domain_id)
    settings = get_settings()
    try:
        est = await estimate_domain_handover_cost(domain_id=did, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=_to_cost_data(est, settings.handover_max_cost_usd),
    )


@router.get(
    "/teams/{squad}/estimate",
    response_model=BifrostEnvelope[CostEstimateData],
)
async def estimate_team_handover_route(
    squad: str,
    repo: Repo,
) -> BifrostEnvelope[CostEstimateData]:
    squad_value = (squad or "").strip()
    if not squad_value:
        raise HTTPException(status_code=400, detail="squad must not be empty")
    settings = get_settings()
    try:
        est = await estimate_team_handover_cost(
            squad=squad_value,
            repo=repo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=_to_cost_data(est, settings.handover_max_cost_usd),
    )


@router.get(
    "/regions/{region}/estimate",
    response_model=BifrostEnvelope[CostEstimateData],
)
async def estimate_region_handover_route(
    region: str,
    repo: Repo,
) -> BifrostEnvelope[CostEstimateData]:
    region_value = (region or "").strip()
    if not region_value:
        raise HTTPException(status_code=400, detail="region must not be empty")
    settings = get_settings()
    try:
        est = await estimate_region_handover_cost(
            region=region_value,
            repo=repo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=_to_cost_data(est, settings.handover_max_cost_usd),
    )


# ── Generate (repo scope) ────────────────────────────────────────────


@router.post(
    "/repos/{repo_id}/generate",
    response_model=BifrostEnvelope[GenerateRepoHandoverData],
    status_code=status.HTTP_201_CREATED,
)
async def generate_repo_handover_route(
    repo_id: str,
    repo: Repo,
    request: Request,
    force: bool = False,
    background: bool = False,
) -> BifrostEnvelope[GenerateRepoHandoverData] | JSONResponse:
    await _require_admin_for_force(request, force)
    if (denied := _enforce_rate_limit(request)) is not None:
        return denied
    rid = _parse_uuid(repo_id)
    settings = get_settings()
    try:
        est = await estimate_repo_handover_cost(repo_id=rid, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _enforce_ceiling(est, settings.handover_max_cost_usd, force=force, scope="repo", label=repo_id)

    # ``background=true`` returns 202 immediately and runs
    # the actual generation on a fresh DB session via asyncio.create_task.
    # The UI subscribes to /stream/repo/<repo_name> for completion.
    if background:
        repo_row = await repo._session.get(RepoModel, rid)
        if repo_row is None:
            raise HTTPException(status_code=404, detail=f"repo {repo_id} not found")
        scope_value = repo_row.name

        async def _runner(bg_repo: EntityRepository) -> None:
            await generate_repo_handover(repo_id=rid, repo=bg_repo)

        _spawn_background(_run_in_background("repo", scope_value, _runner))
        return BifrostEnvelope(
            data=GenerateRepoHandoverData(
                doc_id="",
                fs_path="",
                title=f"Generating: {scope_value}",
                used_fallback=False,
            ),
        )

    try:
        result = await generate_repo_handover(repo_id=rid, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=GenerateRepoHandoverData(
            doc_id=str(result.doc_id),
            fs_path=result.fs_path,
            title=result.title,
            used_fallback=result.used_fallback,
        ),
    )


# ── Generate (domain scope) ──────────────────────────────────────────


@router.post(
    "/domains/{domain_id}/generate",
    response_model=BifrostEnvelope[GenerateHandoverData],
    status_code=status.HTTP_201_CREATED,
)
async def generate_domain_handover_route(
    domain_id: str,
    repo: Repo,
    request: Request,
    force: bool = False,
    background: bool = False,
) -> BifrostEnvelope[GenerateHandoverData] | JSONResponse:
    await _require_admin_for_force(request, force)
    if (denied := _enforce_rate_limit(request)) is not None:
        return denied
    did = _parse_uuid(domain_id)
    settings = get_settings()
    try:
        est = await estimate_domain_handover_cost(domain_id=did, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _enforce_ceiling(
        est, settings.handover_max_cost_usd, force=force, scope="domain", label=domain_id
    )

    if background:
        from bifrost.storage.models import Domain

        domain_row = await repo._session.get(Domain, did)
        if domain_row is None:
            raise HTTPException(status_code=404, detail=f"domain {domain_id} not found")
        scope_value = domain_row.name

        async def _runner(bg_repo: EntityRepository) -> None:
            await generate_domain_handover(domain_id=did, repo=bg_repo)

        _spawn_background(_run_in_background("domain", scope_value, _runner))
        return BifrostEnvelope(
            data=GenerateHandoverData(
                doc_id="",
                fs_path="",
                title=f"Generating: {scope_value}",
                used_fallback=False,
            ),
        )

    try:
        result = await generate_domain_handover(domain_id=did, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=GenerateHandoverData(
            doc_id=str(result.doc_id),
            fs_path=result.fs_path,
            title=result.title,
            used_fallback=result.used_fallback,
        ),
    )


# ── Generate (team scope) ──────────────────────────────────────


@router.post(
    "/teams/{squad}/generate",
    response_model=BifrostEnvelope[GenerateHandoverData],
    status_code=status.HTTP_201_CREATED,
)
async def generate_team_handover_route(
    squad: str,
    repo: Repo,
    request: Request,
    force: bool = False,
    background: bool = False,
) -> BifrostEnvelope[GenerateHandoverData] | JSONResponse:
    await _require_admin_for_force(request, force)
    if (denied := _enforce_rate_limit(request)) is not None:
        return denied
    # ``squad`` is the manifest's team (squad) string. FastAPI URL-decodes
    # automatically for path params, so a squad like "inventory/stock"
    # would arrive as "inventory%2Fstock" → "inventory/stock"; typical
    # squads are kebab-case so this rarely matters.
    squad_value = (squad or "").strip()
    if not squad_value:
        raise HTTPException(status_code=400, detail="squad must not be empty")
    settings = get_settings()
    try:
        est = await estimate_team_handover_cost(squad=squad_value, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _enforce_ceiling(
        est, settings.handover_max_cost_usd, force=force, scope="team", label=squad_value
    )

    if background:

        async def _runner(bg_repo: EntityRepository) -> None:
            await generate_team_handover(squad=squad_value, repo=bg_repo)

        _spawn_background(_run_in_background("team", squad_value, _runner))
        return BifrostEnvelope(
            data=GenerateHandoverData(
                doc_id="",
                fs_path="",
                title=f"Generating: {squad_value}",
                used_fallback=False,
            ),
        )

    try:
        result = await generate_team_handover(
            squad=squad_value,
            repo=repo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=GenerateHandoverData(
            doc_id=str(result.doc_id),
            fs_path=result.fs_path,
            title=result.title,
            used_fallback=result.used_fallback,
        ),
    )


# ── Generate (region scope) ─────────────────────────────────


@router.post(
    "/regions/{region}/generate",
    response_model=BifrostEnvelope[GenerateHandoverData],
    status_code=status.HTTP_201_CREATED,
)
async def generate_region_handover_route(
    region: str,
    repo: Repo,
    request: Request,
    force: bool = False,
    background: bool = False,
) -> BifrostEnvelope[GenerateHandoverData] | JSONResponse:
    await _require_admin_for_force(request, force)
    if (denied := _enforce_rate_limit(request)) is not None:
        return denied
    # ``region`` is the OrgRepo.region string (e.g. "us",
    # "eu"). FastAPI URL-decodes path params automatically.
    region_value = (region or "").strip()
    if not region_value:
        raise HTTPException(status_code=400, detail="region must not be empty")
    settings = get_settings()
    try:
        est = await estimate_region_handover_cost(region=region_value, repo=repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _enforce_ceiling(
        est, settings.handover_max_cost_usd, force=force, scope="region", label=region_value
    )

    if background:

        async def _runner(bg_repo: EntityRepository) -> None:
            await generate_region_handover(region=region_value, repo=bg_repo)

        _spawn_background(_run_in_background("region", region_value, _runner))
        return BifrostEnvelope(
            data=GenerateHandoverData(
                doc_id="",
                fs_path="",
                title=f"Generating: {region_value}",
                used_fallback=False,
            ),
        )

    try:
        result = await generate_region_handover(
            region=region_value,
            repo=repo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BifrostEnvelope(
        data=GenerateHandoverData(
            doc_id=str(result.doc_id),
            fs_path=result.fs_path,
            title=result.title,
            used_fallback=result.used_fallback,
        ),
    )


async def _run_in_background(
    scope_kind: str,
    scope_value: str,
    runner,
) -> None:
    """Open a fresh DB session via the application's session factory and
    invoke ``runner(repo)`` with a request-scoped ``EntityRepository``
    bound to it. Used for fire-and-forget handover generation
    so the HTTP response can return 202 immediately while the actual
    Sonnet calls run on a background asyncio task.

    The factory's lifecycle is managed by the FastAPI app; we acquire
    a session, run the work, commit (the generator is responsible for
    its own ``flush``), close. On any exception we log + emit an
    ``error`` event so the UI's SSE subscription always sees a
    terminal frame.
    """
    settings = get_settings()
    factory = get_session_factory(settings)
    try:
        async with factory() as session:
            try:
                bg_repo = EntityRepository(session)
                await runner(bg_repo)
                await session.commit()
            except Exception:
                logger.exception(
                    "background handover generation crashed for %s/%s",
                    scope_kind,
                    scope_value,
                )
                await session.rollback()
                await publish_handover_event(
                    scope_kind,
                    scope_value,
                    "error",
                    {"reason": "background_task_crashed"},
                )
                return
    except Exception:
        logger.exception(
            "couldn't open session for background handover %s/%s",
            scope_kind,
            scope_value,
        )
        await publish_handover_event(
            scope_kind,
            scope_value,
            "error",
            {"reason": "session_factory_failed"},
        )


def _enforce_ceiling(
    estimate: CostEstimate,
    ceiling_usd: float,
    *,
    force: bool,
    scope: str,
    label: str,
) -> None:
    """409 the generate request if the estimated cost would exceed the
    configured ceiling, unless the caller passed ``?force=true``.

    On force=true we log an explicit audit line so cost overruns are
    traceable in the server log without needing a separate audit table."""
    if estimate.estimated_usd <= ceiling_usd:
        return
    if force:
        logger.warning(
            "handover ceiling override: scope=%s label=%s estimated=%.2f ceiling=%.2f reason=%s",
            scope,
            label,
            estimate.estimated_usd,
            ceiling_usd,
            estimate.reasoning,
        )
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "handover_cost_ceiling_exceeded",
            "message": (
                f"Estimated handover cost ${estimate.estimated_usd:.2f} exceeds "
                f"ceiling ${ceiling_usd:.2f}. Pass ?force=true to override."
            ),
            "estimate": _to_cost_data(estimate, ceiling_usd).model_dump(),
        },
    )


# ── Doubt candidates (loop-back) ───────────────────────────


@router.get(
    "/{doc_id}/candidates",
    response_model=BifrostEnvelope[list[DoubtCandidateData]],
)
async def get_handover_candidates(
    doc_id: str,
    repo: Repo,
    min_occurrences: int = 3,
) -> BifrostEnvelope[list[DoubtCandidateData]]:
    """Recurring unresolved doubts that point at this handover.

    Returns clusters seen across ``min_occurrences`` or more
    distinct sessions whose requirement / chat history mentioned
    this handover's scope. Empty when the threshold isn't crossed —
    UI hides the panel in that case so a sparse / new handover
    doesn't flash the user with noise."""
    did = _parse_uuid(doc_id)
    candidates = await find_doubt_candidates_for_handover(
        repo=repo,
        handover_doc_id=did,
        min_occurrences=max(1, min_occurrences),
    )
    return BifrostEnvelope(
        data=[
            DoubtCandidateData(
                representative_question=c.representative_question,
                occurrence_count=c.occurrence_count,
                session_ids=c.session_ids,
                first_seen_at=c.first_seen_at,
                last_seen_at=c.last_seen_at,
            )
            for c in candidates
        ],
    )


# ── Audit trail ───────────────────────────────────────────


@router.get(
    "/{doc_id}/audit",
    response_model=BifrostEnvelope[list[HandoverAuditEntry]],
)
async def list_handover_doc_audit(
    doc_id: str,
    repo: Repo,
) -> BifrostEnvelope[list[HandoverAuditEntry]]:
    """Return the last 100 lifecycle audit rows for a handover doc,
    newest first.

    Each row is one mutating event — generate / archive / restore /
    edit / promote — captured at the call site. ``actor`` is the
    cookie session id for user-initiated events, ``"system"`` for
    generator-initiated events, ``None`` if no cookie was present.
    Reads do NOT emit audit rows.

    The doc id need NOT correspond to a still-extant
    ``handover_docs`` row — the audit trail intentionally outlives a
    hard-delete of the source doc — so this endpoint returns ``[]``
    rather than 404 when the id is unknown.
    """
    did = _parse_uuid(doc_id)
    rows = await repo.list_handover_audits(did, limit=100)
    return BifrostEnvelope(
        data=[
            HandoverAuditEntry(
                id=str(r.id),
                doc_id=str(r.doc_id),
                event_type=r.event_type,  # type: ignore[arg-type]
                actor=r.actor,
                metadata=dict(r.audit_metadata or {}),
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


# ── Body history ─────────────────────────────────────────


@router.get(
    "/{doc_id}/history",
    response_model=BifrostEnvelope[list[HandoverBodyHistoryEntry]],
)
async def list_handover_doc_body_history(
    doc_id: str,
    repo: Repo,
    limit: int = 20,
) -> BifrostEnvelope[list[HandoverBodyHistoryEntry]]:
    """Return up to ``limit`` body snapshots for a handover doc,
    newest first.

    Each snapshot is the full Markdown body as it stood *before* one
    overwrite event (regen / edit / promote). The UI diff viewer
    fetches this list, lets the operator pick a past snapshot, and
    renders a line-by-line diff against the current body.

    Capped at 20 by the repository write path — older snapshots are
    pruned when a new one lands. Caller passes ``limit`` only to
    request a smaller window; values above the retention ceiling
    return at most the retained rows.

    Like the audit endpoint, this returns ``[]`` rather than 404
    when the doc id is unknown — snapshots intentionally outlive a
    hard-delete of the source doc.
    """
    did = _parse_uuid(doc_id)
    capped = max(1, min(limit, 20))
    rows = await repo.list_handover_body_history(did, limit=capped)
    return BifrostEnvelope(
        data=[
            HandoverBodyHistoryEntry(
                id=str(r.id),
                doc_id=str(r.doc_id),
                event_type=r.event_type,  # type: ignore[arg-type]
                body_md=r.body_md,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


# ── Promote candidate (loop-back) ──────────────────────────


@router.post(
    "/{doc_id}/promote-candidate",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def promote_doubt_candidate(
    doc_id: str,
    body: PromoteCandidateRequest,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverDocDetail]:
    """Append a Q+A entry to the handover's on-disk body so future
    clarifier runs can resolve recurring occurrences as
    ``answered_by_doc`` instead of punting to the human team again.

    The Q+A goes under a ``## Common questions`` section at the end
    of the handover; section is created if absent. The DB row's
    ``last_validated_at`` updates so the freshness story stays
    coherent. ``last_generated_at`` does NOT update — this is a
    targeted human edit, not a regeneration."""
    did = _parse_uuid(doc_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    if not doc.fs_path:
        raise HTTPException(
            status_code=409,
            detail="handover has no fs_path; can't promote into missing doc",
        )

    fs_path = Path(doc.fs_path)
    try:
        existing_body = fs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"couldn't read handover body at {fs_path}: {exc}",
        ) from exc

    # Scrub the user-supplied answer before assembling the new
    # body so a pasted token never lands on disk or in history.
    # ``_append_qa_entry`` (below) also strips embedded
    # ``## `` lines from question/answer to prevent header collisions.
    new_body = _append_qa_entry(
        existing_body,
        question=body.question.strip(),
        answer_md=_scrub_body(body.answer_md.strip(), sink="promote:answer"),
    )

    # Snapshot the pre-promote body so the diff viewer
    # can show what got appended. Best-effort. Scrub the snapshot
    # so any historical leakage is forward-redacted.
    await repo.record_handover_body_snapshot(
        doc_id=doc.id,
        body_md=_scrub_body(existing_body, sink="promote:existing"),
        event_type="pre-promote",
    )

    # Atomic write — temp + rename so a crash mid-write never leaves
    # a torn file on disk. Mirrors the generator's atomicity contract.
    tmp_path = fs_path.with_suffix(fs_path.suffix + ".tmp")
    tmp_path.write_text(new_body, encoding="utf-8")
    tmp_path.replace(fs_path)

    # Mark the doc validated (a human just touched it). Don't bump
    # last_generated_at — this isn't a regeneration. Don't touch
    # freshness_state — staleness is keyed on source-data drift, not
    # human edits.
    from datetime import datetime

    doc.last_validated_at = datetime.now(UTC)
    await repo._session.flush()
    await repo._session.refresh(doc)

    logger.info(
        "promote-candidate doc=%s/%s question_len=%d answer_len=%d",
        doc.scope_kind,
        doc.scope_value,
        len(body.question),
        len(body.answer_md),
    )

    # Audit row. Best-effort. Actor is the *verified*
    # cookie session id; unsigned/spoofed cookies leave actor null.
    await repo.record_handover_audit(
        doc_id=doc.id,
        event_type="promoted",
        actor=_verified_session_id(bifrost_session),
        metadata={
            "scope_kind": doc.scope_kind,
            "scope_value": doc.scope_value,
            "question_len": len(body.question),
            "answer_len": len(body.answer_md),
        },
    )

    summary = _to_summary(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=new_body)
    return BifrostEnvelope(data=detail)


# ── In-UI Markdown editor ────────────────────────────────


@router.patch(
    "/{doc_id}/body",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def patch_handover_doc_body(
    doc_id: str,
    body: PatchHandoverBodyRequest,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverDocDetail]:
    """Replace the on-disk Markdown body of a handover with the
    requester's edited content. Same atomicity contract as
    :func:`promote_doubt_candidate` — temp file + rename so a crash
    mid-write never leaves a torn doc on disk.

    The DB row's ``last_validated_at`` updates (a human just touched
    it). ``last_generated_at`` and ``freshness_state`` are left
    alone — this is a targeted human edit, not a regeneration, and
    staleness is keyed on source-data drift, not human edits."""
    did = _parse_uuid(doc_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    if not doc.fs_path:
        raise HTTPException(
            status_code=409,
            detail="handover has no fs_path; can't write body to missing doc",
        )

    fs_path = Path(doc.fs_path)
    # Capture the pre-edit body for (a) the audit row's length
    # metadata and (b) the body-history snapshot below.
    # Best-effort — if the file is missing or unreadable we record
    # ``None`` length, skip the snapshot, and let the audit emitter
    # swallow the gap; the actual write below still proceeds (it
    # overwrites the path regardless).
    try:
        existing_body: str | None = fs_path.read_text(encoding="utf-8")
        body_len_before: int | None = len(existing_body)
    except (OSError, UnicodeDecodeError):
        existing_body = None
        body_len_before = None

    # Scrub before assembly so secrets in user-pasted content
    # never reach handover_body_history nor the on-disk fs_path.
    new_body = _scrub_body(body.body_md, sink="patch-body:new")
    # Match the generator's convention so concatenation downstream
    # (e.g. _append_qa_entry on a future promote) doesn't double up.
    if not new_body.endswith("\n"):
        new_body = new_body + "\n"

    # Snapshot the pre-edit body so the diff viewer
    # can show what changed. Best-effort. Skipped when the prior
    # file was unreadable (nothing meaningful to snapshot). Also
    # scrub the OLD body before snapshotting — a previously-leaked
    # secret would otherwise persist forward into history.
    if existing_body is not None:
        await repo.record_handover_body_snapshot(
            doc_id=doc.id,
            body_md=_scrub_body(existing_body, sink="patch-body:existing"),
            event_type="pre-edit",
        )

    # Atomic write — temp + rename. Mirrors promote_doubt_candidate.
    tmp_path = fs_path.with_suffix(fs_path.suffix + ".tmp")
    try:
        tmp_path.write_text(new_body, encoding="utf-8")
        tmp_path.replace(fs_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"couldn't write handover body at {fs_path}: {exc}",
        ) from exc

    from datetime import datetime

    doc.last_validated_at = datetime.now(UTC)
    await repo._session.flush()
    await repo._session.refresh(doc)

    logger.info(
        "patch-body doc=%s/%s body_len=%d",
        doc.scope_kind,
        doc.scope_value,
        len(new_body),
    )

    # Audit row. Best-effort. Verified cookie only.
    await repo.record_handover_audit(
        doc_id=doc.id,
        event_type="edited",
        actor=_verified_session_id(bifrost_session),
        metadata={
            "scope_kind": doc.scope_kind,
            "scope_value": doc.scope_value,
            "body_len_before": body_len_before,
            "body_len_after": len(new_body),
        },
    )

    summary = _to_summary(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=new_body)
    return BifrostEnvelope(data=detail)


# ── Per-section regeneration ───────────────────────────

# The section_keys the route accepts. Mirrors
# ``handover_generator._SECTION_KEY_TO_HEADER``; we duplicate the set
# here so the route can 400 cleanly without importing the private
# mapping. Keep these in sync.
_SECTION_KEYS: frozenset[str] = frozenset(
    {
        "executive_summary",
        "architecture",
        "critical_flows",
        "ownership",
        "dependencies",
        "ktlo_surface",
        "glossary",
        "common_questions",
    }
)


@router.post(
    "/{doc_id}/sections/{section_key}/regenerate",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def regenerate_handover_section(
    doc_id: str,
    section_key: str,
    repo: Repo,
    request: Request,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverDocDetail] | JSONResponse:
    """Rewrite a single section of an existing handover doc in-place.

    per-section regeneration. The body's other sections —
    including any Q+A appended via ``promote_doubt_candidate`` and
    any human edits — are preserved verbatim. The targeted
    ``## <Section>`` block is replaced with a fresh Sonnet
    completion.

    The Sonnet call is roughly $0.05-$0.10 per invocation (one model
    turn, ~one handover-body's worth of context). We don't enforce
    the per-call cost ceiling here — it's cheap by design and forcing
    a confirm dialog on every section regen would be friction with no
    cost-protection benefit.

    Errors:
      * 404 if the doc id is unknown or the doc has no fs_path.
      * 400 if ``section_key`` isn't one of the canonical 8 keys, or
        the doc body doesn't have a matching ``## <Header>`` section
        to splice into.
      * 500 if the Sonnet call fails (bubbled with the underlying
        message).

    Rate-limited the same way as full regenerations.
    """
    if (denied := _enforce_rate_limit(request)) is not None:
        return denied

    if section_key not in _SECTION_KEYS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_section_key",
                "message": (
                    f"Unknown section_key {section_key!r}. Expected one "
                    f"of: {sorted(_SECTION_KEYS)}."
                ),
            },
        )

    did = _parse_uuid(doc_id)
    settings = get_settings()

    try:
        doc = await regenerate_section(
            doc_id=did,
            section_key=section_key,
            repo=repo,
            settings=settings,
        )
    except ValueError as exc:
        msg = str(exc)
        # Repository-level "not found" or "no fs_path" → 404. Other
        # ValueErrors (missing section header, empty model output) →
        # 400 — the caller can recover by full-regenerating the doc
        # or by picking a different section.
        if "not found" in msg or "fs_path" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except ClaudeCliError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "section_regen_llm_failed",
                "message": (f"LLM call failed while regenerating section {section_key!r}: {exc}"),
            },
        ) from exc

    logger.info(
        "section-regen doc=%s/%s section=%s actor=%s",
        doc.scope_kind,
        doc.scope_value,
        section_key,
        _verified_session_id(bifrost_session) or "anon",
    )

    summary = _to_summary(doc)
    body = _read_body(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=body)
    return BifrostEnvelope(data=detail)


def _demote_h2_lines(text: str) -> str:
    """Demote any ``## `` (h2) lines in ``text`` to ``### `` (h3).

    User-supplied content (promote-candidate question/answer,
    PATCH /body) lives inside an h2 section. Allowing user-emitted
    h2 lines lets a malicious or hallucinated input fragment the
    section structure — `_splice_section` regex captures from the
    *first* h2 it finds through the *next* h2, so injected h2 lines
    redirect future section regens to the wrong region.

    Demotion is preferable to rejection: it preserves intent
    (still a heading) while keeping the document structure intact.
    """
    if not text:
        return text or ""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        # Strip leading whitespace for the prefix check, but only
        # rewrite the actual leading-token form (avoid touching code
        # blocks or inline ``## ``-in-the-middle text). The section
        # splicer matches ``^## `` so we only need to defend the
        # left-margin form.
        if line.lstrip().startswith("## ") and not line.lstrip().startswith("### "):
            # Insert one extra ``#`` after the existing leading whitespace.
            indent = line[: len(line) - len(line.lstrip())]
            rest = line[len(indent) :]
            out.append(f"{indent}#{rest}")
        else:
            out.append(line)
    return "".join(out)


def _normalise_question(question: str) -> str:
    """Collapse whitespace and demote h2 lines.

    A question with embedded newlines breaks the
    ``### Q: <question>`` heading; an h2 line in the question
    fragments the section. ``" ".join(split())`` normalises both.
    """
    cleaned = " ".join(question.split())
    return _demote_h2_lines(cleaned)


def _append_qa_entry(
    existing_body: str,
    *,
    question: str,
    answer_md: str,
) -> str:
    """Append a Q+A pair into a ``## Common questions`` section.

    Question and answer are normalised before
    rendering: ``## `` lines are demoted to ``### `` to prevent
    section-boundary collisions, and the question's whitespace is
    collapsed so a multi-line question doesn't break the heading.

    If the section already exists, the new entry is appended at the
    end (preserving any existing entries). Otherwise the section is
    created at the end of the doc with the entry as its first child.

    The Q+A entry is rendered as:

        ### Q: <question>

        <answer_md>

    so individual entries are addressable as Markdown anchors and
    show up as Prose-rendered subsections in the UI reader.
    """
    section_header = "## Common questions"
    # Normalise both question and answer before rendering.
    safe_question = _normalise_question(question)
    safe_answer = _demote_h2_lines(answer_md)
    qa_block = f"\n### Q: {safe_question}\n\n{safe_answer}\n"

    # Body always ends with a newline by the generator's convention,
    # but be defensive.
    body = existing_body if existing_body.endswith("\n") else existing_body + "\n"

    if section_header in body:
        # Append to the existing section. Find the section, then find
        # the start of the next ``## ``-level header (or end of doc),
        # and insert the Q+A right before that. This preserves any
        # post-section content (rare, but possible).
        idx = body.find(section_header)
        # Skip past the section header line itself.
        after_header = body.find("\n", idx) + 1
        # Find next top-level (``## `` not ``### ``) heading.
        next_section = body.find("\n## ", after_header)
        insert_at = len(body) if next_section == -1 else next_section + 1
        return body[:insert_at] + qa_block + body[insert_at:]

    # No section yet — append a new one.
    return body + "\n" + section_header + "\n" + qa_block


# ── Soft-archive ──────────────────────────────────────────


@router.delete(
    "/{doc_id}",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def archive_handover_doc(
    doc_id: str,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverDocDetail]:
    """Soft-archive a handover doc — sets ``archived_at = now()``.

    The on-disk Markdown at ``fs_path`` is intentionally untouched so
    the archive is fully reversible via ``POST /{doc_id}/restore``.
    Default list views filter archived rows; the UI's "Show archived"
    toggle adds them back so the user can restore.

    Returns the updated detail (with ``archived_at`` populated and
    ``body_md`` still readable from disk if the file is intact)."""
    did = _parse_uuid(doc_id)
    ok = await repo.archive_handover_doc(did)
    if not ok:
        raise HTTPException(status_code=404, detail="handover doc not found")
    await repo._session.flush()
    doc = await repo.get_handover_doc(did)
    if doc is None:  # extremely unlikely race; defensive
        raise HTTPException(status_code=404, detail="handover doc not found")
    logger.info(
        "archive-handover doc=%s/%s id=%s",
        doc.scope_kind,
        doc.scope_value,
        doc_id,
    )
    # Audit row. Best-effort. Verified cookie only.
    await repo.record_handover_audit(
        doc_id=doc.id,
        event_type="archived",
        actor=_verified_session_id(bifrost_session),
        metadata={
            "scope_kind": doc.scope_kind,
            "scope_value": doc.scope_value,
        },
    )
    summary = _to_summary(doc)
    body = _read_body(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=body)
    return BifrostEnvelope(data=detail)


@router.post(
    "/{doc_id}/restore",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def restore_handover_doc(
    doc_id: str,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverDocDetail]:
    """Restore a previously-archived handover doc — clears
    ``archived_at``. Idempotent (restoring a live doc is a no-op
    that still returns its current detail)."""
    did = _parse_uuid(doc_id)
    ok = await repo.restore_handover_doc(did)
    if not ok:
        raise HTTPException(status_code=404, detail="handover doc not found")
    await repo._session.flush()
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    logger.info(
        "restore-handover doc=%s/%s id=%s",
        doc.scope_kind,
        doc.scope_value,
        doc_id,
    )
    # Audit row. Best-effort. Verified cookie only.
    await repo.record_handover_audit(
        doc_id=doc.id,
        event_type="restored",
        actor=_verified_session_id(bifrost_session),
        metadata={
            "scope_kind": doc.scope_kind,
            "scope_value": doc.scope_value,
        },
    )
    summary = _to_summary(doc)
    body = _read_body(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=body)
    return BifrostEnvelope(data=detail)


# ── Section feedback (team-collab gap closure) ─────────────────────
#
# Lets a reader flag a specific section of a handover as outdated /
# incorrect / unclear / missing-info / other. The doc owner's inbox
# surfaces the open backlog; a section-metadata aggregate drives the
# per-section "needs attention" badges in the UI.


@router.post(
    "/{doc_id}/section-feedback",
    response_model=BifrostEnvelope[HandoverFeedbackEntry],
    status_code=status.HTTP_201_CREATED,
)
async def submit_section_feedback(
    doc_id: str,
    body: SubmitFeedbackRequest,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverFeedbackEntry]:
    """Attach one feedback row to a section of a handover doc.

    The route layer validates length / kind enum via the request
    schema; the underlying CHECK constraint provides defense-in-
    depth. ``actor`` is the *verified* cookie session id; spoofed
    cookies leave it ``NULL`` (mirrors the audit-log identity model).

    Returns 404 when the doc id is unknown.
    """
    did = _parse_uuid(doc_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")

    feedback = await repo.add_section_feedback(
        doc_id=did,
        section_key=body.section_key.strip(),
        kind=body.kind,
        note=(body.note or None),
        actor=_verified_session_id(bifrost_session),
    )
    await repo._session.flush()
    await repo._session.refresh(feedback)

    logger.info(
        "section-feedback doc=%s/%s section=%s kind=%s actor=%s",
        doc.scope_kind,
        doc.scope_value,
        feedback.section_key,
        feedback.kind,
        _verified_session_id(bifrost_session) or "anon",
    )
    return BifrostEnvelope(data=_feedback_to_entry(feedback))


@router.get(
    "/{doc_id}/section-feedback",
    response_model=BifrostEnvelope[list[HandoverFeedbackEntry]],
)
async def list_section_feedback(
    doc_id: str,
    repo: Repo,
    include_resolved: bool = False,
) -> BifrostEnvelope[list[HandoverFeedbackEntry]]:
    """List feedback rows on a handover doc, newest first.

    Default scope is the open backlog (``resolved_at IS NULL``);
    pass ``include_resolved=true`` for the full history. Returns
    ``[]`` rather than 404 when the doc id is unknown — feedback
    intentionally outlives a soft-archive (the row gets cascade-
    deleted only on a future hard-purge of the doc).
    """
    did = _parse_uuid(doc_id)
    rows = await repo.list_section_feedback(
        did,
        include_resolved=include_resolved,
    )
    return BifrostEnvelope(
        data=[_feedback_to_entry(r) for r in rows],
    )


@router.post(
    "/{doc_id}/section-feedback/{feedback_id}/resolve",
    response_model=BifrostEnvelope[HandoverFeedbackEntry],
)
async def resolve_section_feedback_route(
    doc_id: str,
    feedback_id: str,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverFeedbackEntry]:
    """Mark a feedback row resolved.

    Sets ``resolved_at = now()`` and ``resolved_by`` from the
    verified cookie. Idempotent — re-resolving an already-resolved
    row bumps the timestamp and rewrites ``resolved_by``.

    404 cases: doc id unknown, feedback id unknown, or the feedback
    row exists but belongs to a different doc (ownership check).
    """
    did = _parse_uuid(doc_id)
    fid = _parse_uuid(feedback_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    existing = await repo.get_section_feedback(fid)
    if existing is None or existing.doc_id != did:
        raise HTTPException(
            status_code=404,
            detail="feedback not found",
        )

    feedback = await repo.resolve_section_feedback(
        fid,
        resolved_by=_verified_session_id(bifrost_session),
    )
    if feedback is None:
        # Race: deleted between the lookup and the resolve. Treat as
        # a 404 so the client can refresh its view.
        raise HTTPException(
            status_code=404,
            detail="feedback not found",
        )
    await repo._session.flush()
    await repo._session.refresh(feedback)

    logger.info(
        "section-feedback-resolve doc=%s/%s feedback=%s actor=%s",
        doc.scope_kind,
        doc.scope_value,
        feedback_id,
        _verified_session_id(bifrost_session) or "anon",
    )
    return BifrostEnvelope(data=_feedback_to_entry(feedback))


# ── Section metadata (team-collab gap closure) ─────────────────────


@router.get(
    "/{doc_id}/section-metadata",
    response_model=BifrostEnvelope[dict[str, SectionMetadataEntry]],
)
async def get_section_metadata(
    doc_id: str,
    repo: Repo,
) -> BifrostEnvelope[dict[str, SectionMetadataEntry]]:
    """Per-section freshness + feedback aggregates for a handover doc.

    Returns ``{section_key: {last_regenerated_at, feedback_open_count,
    last_feedback_at}}``. Sections with no signals at all are
    omitted; a UI rendering all canonical sections iterates the
    static section list and falls back to the default zero/None per
    slot.

    404 when the doc id is unknown — signals are tied to a doc, not
    standalone.
    """
    did = _parse_uuid(doc_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    raw = await repo.section_metadata(did)
    out: dict[str, SectionMetadataEntry] = {
        section_key: SectionMetadataEntry(
            last_regenerated_at=values.get("last_regenerated_at"),
            feedback_open_count=int(values.get("feedback_open_count") or 0),
            last_feedback_at=values.get("last_feedback_at"),
        )
        for section_key, values in raw.items()
    }
    return BifrostEnvelope(data=out)


# ── Restore from body history (team-collab gap closure) ────────────


@router.post(
    "/{doc_id}/restore-version",
    response_model=BifrostEnvelope[HandoverDocDetail],
)
async def restore_handover_version(
    doc_id: str,
    body: RestoreVersionRequest,
    repo: Repo,
    bifrost_session: str | None = Cookie(default=None),
) -> BifrostEnvelope[HandoverDocDetail]:
    """Revert a handover body to a specific past snapshot.

    Pipeline:
      1. Validate the doc + history row exist and belong together.
      2. Snapshot the CURRENT body as ``event_type='pre-restore'``
         so the restore itself is reversible.
      3. Atomic write (temp + rename) the snapshot's ``body_md`` to
         ``fs_path``.
      4. Bump ``last_validated_at``.
      5. Emit an audit row (``event_type='restored_version'`` with
         ``history_id`` + the original snapshot's event type).

    Errors:
      * 404 when the doc id is unknown, the doc has no fs_path, or
        the history id doesn't belong to this doc.
      * 500 when the on-disk write fails (surfaced with the OS error).
    """
    did = _parse_uuid(doc_id)
    hid = _parse_uuid(body.history_id)
    doc = await repo.get_handover_doc(did)
    if doc is None:
        raise HTTPException(status_code=404, detail="handover doc not found")
    if not doc.fs_path:
        raise HTTPException(
            status_code=404,
            detail="handover has no fs_path; nothing to restore into",
        )

    # Ownership check: the snapshot must point at this doc. We pull
    # the full retained window (≤20 rows) and pick the matching id —
    # cheaper and clearer than a dedicated get-by-id helper.
    history_rows = await repo.list_handover_body_history(did, limit=20)
    snapshot = next((r for r in history_rows if r.id == hid), None)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail="history snapshot not found for this doc",
        )

    fs_path = Path(doc.fs_path)
    # Best-effort: capture the current body so the pre-restore
    # snapshot has data. If the file is unreadable we skip the
    # snapshot but still proceed with the restore — the restore is
    # the user-visible side effect.
    try:
        existing_body: str | None = fs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing_body = None

    # Scrub the soon-to-be-overwritten body before it
    # lands in history. Same convention as the patch-body / promote
    # write paths.
    if existing_body is not None:
        await repo.record_handover_body_snapshot(
            doc_id=doc.id,
            body_md=_scrub_body(existing_body, sink="restore:existing"),
            event_type="pre-restore",
        )

    new_body = snapshot.body_md
    if not new_body.endswith("\n"):
        new_body = new_body + "\n"

    # Atomic write — temp + rename. Mirrors patch-body / promote.
    tmp_path = fs_path.with_suffix(fs_path.suffix + ".tmp")
    try:
        tmp_path.write_text(new_body, encoding="utf-8")
        tmp_path.replace(fs_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"couldn't write handover body at {fs_path}: {exc}",
        ) from exc

    from datetime import datetime

    doc.last_validated_at = datetime.now(UTC)
    await repo._session.flush()
    await repo._session.refresh(doc)

    logger.info(
        "restore-version doc=%s/%s history=%s body_len=%d",
        doc.scope_kind,
        doc.scope_value,
        body.history_id,
        len(new_body),
    )

    await repo.record_handover_audit(
        doc_id=doc.id,
        event_type="restored_version",
        actor=_verified_session_id(bifrost_session),
        metadata={
            "scope_kind": doc.scope_kind,
            "scope_value": doc.scope_value,
            "history_id": str(snapshot.id),
            "restored_to_event": snapshot.event_type,
            "body_len_before": len(existing_body) if existing_body else None,
            "body_len_after": len(new_body),
        },
    )

    summary = _to_summary(doc)
    detail = HandoverDocDetail(**summary.model_dump(), body_md=new_body)
    return BifrostEnvelope(data=detail)


# ── Referencing sessions (team-collab gap closure) ─────────────────


@router.get(
    "/{doc_id}/referencing-sessions",
    response_model=BifrostEnvelope[list[ReferencingSessionEntry]],
)
async def list_referencing_sessions(
    doc_id: str,
    repo: Repo,
    limit: int = 50,
) -> BifrostEnvelope[list[ReferencingSessionEntry]]:
    """List collaboration sessions whose ``matched_doc_ids`` contains
    this handover doc.

    Use case: a doc owner wants to see who's been asking about their
    doc. Order is ``created_at DESC``; capped at ``limit`` (default
    50). Each row carries the small subset of session fields that
    matter to the doc owner — id, requester, requirement, state,
    open-doubt count, created/updated timestamps.

    Returns ``[]`` rather than 404 when the doc id is unknown — the
    answer is the same shape and saves a roundtrip when the UI is
    polling speculatively.
    """
    did = _parse_uuid(doc_id)
    capped = max(1, min(limit, 200))
    sessions = await repo.list_sessions_referencing_doc(did, limit=capped)
    out: list[ReferencingSessionEntry] = []
    for sess in sessions:
        # Cheap per-session count — same trick the list_collab_sessions
        # route uses. Sessions referencing a doc are bounded.
        doubts = await repo.list_collab_doubts(sess.id)
        open_count = sum(1 for d in doubts if d.status == "open")
        out.append(
            ReferencingSessionEntry(
                id=str(sess.id),
                requester=sess.requester,
                requirement_text=sess.requirement_text,
                state=sess.state,
                doubt_count_open=open_count,
                created_at=sess.created_at,
                # ``updated_at`` is the closest proxy for "last
                # activity" — every clarifier turn / state mutation
                # bumps it via the ORM ``onupdate`` clause.
                last_activity_at=sess.updated_at,
            ),
        )
    return BifrostEnvelope(data=out)


# ── Bulk operations ───────────────────────────────────────


@router.post(
    "/bulk/refresh-stale",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict],
    status_code=status.HTTP_202_ACCEPTED,
)
async def bulk_refresh_stale(
    repo: Repo,
    force: bool = False,
) -> BifrostEnvelope[dict]:
    """Fan out background regenerations for every handover doc whose
    ``freshness_state != 'fresh'``.

    Returns 202 immediately with the count accepted. Each generation
    runs on its own asyncio task with a fresh DB session. The cost ceiling is enforced per-scope; if any
    individual scope blows the ceiling, that one is skipped (logged
    as ``ceiling_skip``) — the others still run. ``force=true``
    bypasses the per-scope ceiling for this batch.

    Spawns repo handovers via name lookup; domain via id; team /
    region via scope_value (which is the join key). Unsupported
    scope kinds (``feature``, ``session``) are skipped.
    """
    docs = await repo.list_handover_docs(limit=500)
    stale = [d for d in docs if d.freshness_state != "fresh"]
    accepted = 0
    skipped: list[dict] = []
    settings = get_settings()

    for doc in stale:
        try:
            if doc.scope_kind == "repo":
                # Look up repo by name. Skip if repo no longer exists
                # (could happen after deregistration; the doc would
                # ideally have been deleted but isn't yet).
                from sqlalchemy import select

                stmt = select(RepoModel).where(RepoModel.name == doc.scope_value)
                row = (await repo._session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    skipped.append(
                        {
                            "scope_kind": doc.scope_kind,
                            "scope_value": doc.scope_value,
                            "reason": "repo_not_found",
                        }
                    )
                    continue
                est = await estimate_repo_handover_cost(repo_id=row.id, repo=repo)
                rid = row.id

                async def _runner_repo(bg_repo: EntityRepository, rid=rid) -> None:
                    await generate_repo_handover(repo_id=rid, repo=bg_repo)

                runner = _runner_repo
            elif doc.scope_kind == "domain":
                from sqlalchemy import select

                from bifrost.storage.models import Domain

                stmt = select(Domain).where(Domain.name == doc.scope_value)
                drow = (await repo._session.execute(stmt)).scalar_one_or_none()
                if drow is None:
                    skipped.append(
                        {
                            "scope_kind": doc.scope_kind,
                            "scope_value": doc.scope_value,
                            "reason": "domain_not_found",
                        }
                    )
                    continue
                est = await estimate_domain_handover_cost(domain_id=drow.id, repo=repo)
                did = drow.id

                async def _runner_domain(bg_repo: EntityRepository, did=did) -> None:
                    await generate_domain_handover(domain_id=did, repo=bg_repo)

                runner = _runner_domain
            elif doc.scope_kind == "team":
                est = await estimate_team_handover_cost(
                    squad=doc.scope_value,
                    repo=repo,
                )
                squad = doc.scope_value

                async def _runner_team(bg_repo: EntityRepository, squad=squad) -> None:
                    await generate_team_handover(squad=squad, repo=bg_repo)

                runner = _runner_team
            elif doc.scope_kind == "region":
                est = await estimate_region_handover_cost(
                    region=doc.scope_value,
                    repo=repo,
                )
                region = doc.scope_value

                async def _runner_region(bg_repo: EntityRepository, region=region) -> None:
                    await generate_region_handover(region=region, repo=bg_repo)

                runner = _runner_region
            else:
                skipped.append(
                    {
                        "scope_kind": doc.scope_kind,
                        "scope_value": doc.scope_value,
                        "reason": "unsupported_scope_kind",
                    }
                )
                continue

            if not force and est.estimated_usd > settings.handover_max_cost_usd:
                skipped.append(
                    {
                        "scope_kind": doc.scope_kind,
                        "scope_value": doc.scope_value,
                        "reason": "ceiling_skip",
                        "estimated_usd": est.estimated_usd,
                        "ceiling_usd": settings.handover_max_cost_usd,
                    }
                )
                continue

            _spawn_background(
                _run_in_background(doc.scope_kind, doc.scope_value, runner),
            )
            accepted += 1
        except Exception:
            logger.exception(
                "bulk-refresh-stale: skip %s/%s (unexpected)",
                doc.scope_kind,
                doc.scope_value,
            )
            skipped.append(
                {
                    "scope_kind": doc.scope_kind,
                    "scope_value": doc.scope_value,
                    "reason": "exception",
                }
            )

    logger.info(
        "bulk-refresh-stale total_stale=%d accepted=%d skipped=%d force=%s",
        len(stale),
        accepted,
        len(skipped),
        force,
    )
    return BifrostEnvelope(
        data={
            "total_stale": len(stale),
            "accepted": accepted,
            "skipped": skipped,
        },
    )


# ── Generation progress SSE ───────────────────────────────


@router.get("/stream/{scope_kind}/{scope_value}")
async def stream_handover_generation(
    scope_kind: str,
    scope_value: str,
) -> StreamingResponse:
    """Live progress events while a handover is being generated.

    Frontend opens this stream at click-time keyed on the (kind,
    scope) pair. Events are: ``scope_resolved``,
    ``child_started`` / ``child_done`` (tree path),
    ``compose_started`` / ``compose_done``, ``fs_written``,
    ``complete``, ``error``. The same TaskEventBus replay buffer
    that powers task / collab streams handles reconnects.

    Sets X-Accel-Buffering: no so an nginx proxy doesn't buffer the
    stream — same as the collab session stream."""
    if scope_kind not in {
        "repo",
        "domain",
        "team",
        "region",
    }:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported scope_kind: {scope_kind}",
        )
    return StreamingResponse(
        event_stream(handover_channel_key(scope_kind, scope_value)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid uuid") from exc


def _to_summary(doc: HandoverDoc) -> HandoverDocSummary:
    return HandoverDocSummary(
        id=str(doc.id),
        scope_kind=doc.scope_kind,  # type: ignore[arg-type]
        scope_value=doc.scope_value,
        parent_id=str(doc.parent_id) if doc.parent_id else None,
        fs_path=doc.fs_path,
        freshness_state=doc.freshness_state,  # type: ignore[arg-type]
        last_generated_at=doc.last_generated_at,
        last_validated_at=doc.last_validated_at,
        generation_cost_usd=(float(doc.generation_cost_usd) if doc.generation_cost_usd else None),
        archived_at=doc.archived_at,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _read_body(doc: HandoverDoc) -> str | None:
    """Eagerly read the on-disk Markdown body. Returns ``None`` if
    the file is missing or unreadable — UI degrades to "no body
    available" rather than 500-ing the detail fetch."""
    if not doc.fs_path:
        return None
    try:
        return Path(doc.fs_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "couldn't read handover at %s: %s",
            doc.fs_path,
            exc,
        )
        return None


def _feedback_to_entry(
    fb: HandoverSectionFeedback,
) -> HandoverFeedbackEntry:
    """Serialise a :class:`HandoverSectionFeedback` ORM row to its
    API schema. Keeps the route layer free of ORM-attribute access."""
    return HandoverFeedbackEntry(
        id=str(fb.id),
        doc_id=str(fb.doc_id),
        section_key=fb.section_key,
        kind=fb.kind,  # type: ignore[arg-type]
        note=fb.note,
        actor=fb.actor,
        resolved_at=fb.resolved_at,
        resolved_by=fb.resolved_by,
        created_at=fb.created_at,
    )
