"""CorrelationIDMiddleware -- request-scoped correlation IDs.

S12 (observability-audit.md) section 5.4 requires every request to carry a
correlation_id that surfaces in logs and downstream calls. The
implementation was missing entirely: there was no middleware, no
contextvars binding, no response header. As a result, a failing request
could not be traced across orchestration -> agents -> adapters -> LLM
without grepping by timestamp.

This middleware:
  1. Reads `X-Correlation-ID` from the inbound request, or generates a
     fresh UUID4 if absent (callers can supply their own to join logs
     across services).
  2. Binds the value to structlog's contextvars so every log emitted
     inside the request's asyncio task carries `correlation_id=...`
     automatically (no per-call-site changes needed).
  3. Attaches it to `request.state.correlation_id` so route handlers
     and dependencies can read it via FastAPI.
  4. Echoes the value back in the `X-Correlation-ID` response header so
     clients can quote it when filing bug reports.

Header name choice: `X-Correlation-ID` matches the convention used
across the existing service-mesh ingress and is what the operator pages
already display. W3C `traceparent` is a future option for OpenTelemetry
integration (out of scope here).
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_HEADER = "X-Correlation-ID"


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Bind a correlation_id to the request's logging context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cid = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        request.state.correlation_id = cid

        # Bind to structlog contextvars so every structlog logger call
        # inside the request task automatically carries the cid. Clear
        # at the end so the next reuse of this asyncio task (unlikely
        # but possible) does not leak state across requests.
        structlog.contextvars.bind_contextvars(correlation_id=cid)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers[CORRELATION_HEADER] = cid
        return response
