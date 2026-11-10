"""FastAPI application factory.

Creates the app with async lifespan, CORS, structlog request logging,
and the /health endpoint. Route groups are mounted from api.routes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog

# Ensure standard-library loggers (used by extraction pipeline) output to stderr
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from bifrost.api.deps import get_session_factory
from bifrost.api.error_handlers import register_error_handlers
from bifrost.api.routes import admin_router_top, api_router, public_router
from bifrost.cli.config import get_settings, validate_startup

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Async lifespan — startup and shutdown hooks."""
    settings = get_settings()
    logger.info("bifrost.startup", track=settings.track, log_level=settings.log_level)

    # Production-mode preflight.
    # When BIFROST_PRODUCTION_MODE=true, refuse to start with any of
    # the prod-required secrets missing or still on their local-dev
    # defaults. Default is False so dev / CI / demo keep booting.
    prod_errors = settings.validate_production_secrets()
    if prod_errors:
        for err in prod_errors:
            logger.error("bifrost.production_misconfig", error=err)
        raise RuntimeError(
            "BIFROST_PRODUCTION_MODE=true but required secrets are "
            "missing or still on dev defaults. Errors:\n  - " + "\n  - ".join(prod_errors)
        )

    import asyncio as _aio

    # Start the extraction worker
    from bifrost.extraction.worker import ExtractionWorker, set_worker

    _sf = get_session_factory(settings)
    worker = ExtractionWorker(
        session_factory=_sf,
        settings=settings,
    )
    set_worker(worker)
    await worker.start()
    logger.info("bifrost.worker_started", dry_run=settings.extraction_dry_run)

    # Resumability: if the API died mid-run, the research_runs row
    # was left in an in-flight status. Mark anything older than 10
    # minutes as failed so a fresh POST doesn't 409 forever against
    # a corpse. Live runs are immune (they're younger than 10 min).
    # Backgrounded via create_task so the lifespan yield (and the
    # first /health probe) is not blocked by this DB query.
    from bifrost.research.runs import reclaim_orphaned_runs

    async def _reclaim_orphans() -> None:
        try:
            reclaimed = await reclaim_orphaned_runs(_sf)
            if reclaimed:
                logger.info(
                    "bifrost.research_runs_orphaned_reclaimed",
                    count=reclaimed,
                )
        except Exception:  # noqa: BLE001
            logger.exception("bifrost.research_runs_orphan_reclaim_failed")

    _aio.create_task(_reclaim_orphans(), name="bifrost.reclaim_orphans")

    # Schedule the hourly task auto-archive sweep.
    from bifrost.actions.auto_archive import start_auto_archive_task

    auto_archive_task = start_auto_archive_task(
        get_session_factory(settings),
    )

    # Periodic janitor for the SSE replay buffer — evicts terminal
    # buffers whose TTL (5min) has elapsed so memory doesn't grow
    # unboundedly across the lifetime of the process.
    from bifrost.api.sse import event_bus

    async def _sse_janitor() -> None:
        try:
            while True:
                await _aio.sleep(120)  # every 2 min
                try:
                    purged = event_bus.sweep()
                    if purged:
                        logger.debug(
                            "sse.janitor.sweep",
                            purged=purged,
                        )
                except Exception:
                    logger.exception("sse.janitor.error")
        except _aio.CancelledError:
            raise

    sse_janitor_task = _aio.create_task(
        _sse_janitor(),
        name="bifrost.sse_janitor",
    )
    logger.info("bifrost.sse_janitor_started", interval_s=120)

    # PR observer (Phase B of the SDLC redesign). Polls every 60s
    # for open task PRs; on merge, transitions task→pr_merged which
    # fires the Jira sync hook to move the ticket to "Done".
    from bifrost.actions.pr_observer import OBSERVE_INTERVAL_S, observer_loop

    pr_observer_task = _aio.create_task(
        observer_loop(
            get_session_factory(settings),
            interval_s=OBSERVE_INTERVAL_S,
        ),
        name="bifrost.pr_observer",
    )
    logger.info("bifrost.pr_observer_started", interval_s=OBSERVE_INTERVAL_S)

    yield

    for task in (auto_archive_task, sse_janitor_task, pr_observer_task):
        if task is not None:
            task.cancel()
            with suppress(_aio.CancelledError):
                await task
    await worker.stop()
    logger.info("bifrost.shutdown")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, and duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Bifrost",
        description="AI-powered engineering knowledge platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — explicit allowlist.
    # ``allow_origins=["*"]`` combined with ``allow_credentials=True``
    # let Starlette echo any inbound Origin header, which let
    # non-browser clients land credentialed cross-origin requests
    # with arbitrary attribution. Read the allowlist from
    # ``BIFROST_CORS_ORIGINS`` (comma-separated). The default covers
    # the Vite dev server. Production deployments must set this.
    settings = validate_startup()
    cors_origins = settings.cors_origins_list
    if not cors_origins:
        # Refuse to start with an empty allowlist. Previously this
        # was a warning that silently broke the UI (FE requests get
        # CORS-rejected with no clear server-side signal). A misconfig
        # should fail loud, not produce a half-working service.
        # If the operator genuinely wants no cross-origin clients,
        # they can set BIFROST_CORS_ORIGINS to a same-origin value
        # like the app's own URL.
        raise RuntimeError(
            "BIFROST_CORS_ORIGINS resolved to an empty list. "
            "Set it to a comma-separated list of allowed origins, "
            "e.g. 'http://localhost:5173,http://127.0.0.1:5173' for "
            "local dev or 'https://bifrost.example.com' in prod."
        )
    # Middleware order: add_middleware makes the LAST one added the
    # outermost, so CORS goes last and answers preflights and decorates
    # the CSRF/identity rejections below it.
    app.add_middleware(RequestLoggingMiddleware)

    # Identity gate. Building the provider here fails startup on a
    # misconfigured identity section (e.g. header mode without
    # trusted_proxy, or roles unset). See ``src/bifrost/api/auth/``.
    from bifrost.api.auth.csrf import CsrfMiddleware
    from bifrost.api.auth.middleware import IdentityMiddleware
    from bifrost.api.auth.providers import (
        admin_groups,
        build_identity_provider,
        build_token_authenticator,
        identity_header_names,
        trusted_proxy_networks,
    )

    provider = build_identity_provider(settings)
    app.state.identity_provider = provider
    app.add_middleware(
        IdentityMiddleware,
        provider=provider,
        admin_groups=admin_groups(settings),
        tokens=build_token_authenticator(settings),
        identity_headers=identity_header_names(settings),
        trusted_networks=trusted_proxy_networks(settings),
    )
    app.add_middleware(CsrfMiddleware, allowed_origins=cors_origins)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        # PUT removed — no route currently uses it. Add back only when
        # a new endpoint truly needs PUT semantics.
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )

    # Health endpoint — no auth required
    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Mount API route groups under /api/v1.
    # - ``public_router`` (health, auth): reachable without a principal.
    # - ``api_router``: every route needs an authenticated principal
    #   (IdentityMiddleware); mutating routes add their own role gate.
    # - ``admin_router_top``: admin role required on every route.
    app.include_router(public_router, prefix="/api/v1")
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(admin_router_top, prefix="/api/v1")

    register_error_handlers(app)

    return app
