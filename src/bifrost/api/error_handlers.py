"""Deterministic ``BifrostError`` → HTTP mapping.

Registers one exception handler so any :class:`~bifrost.errors.BifrostError`
that propagates out of a route maps to its declared ``http_status`` with a
structured body, instead of falling through to FastAPI's generic 500. Routes
that raise ``HTTPException`` directly are untouched — FastAPI's own handler
still owns those.

Body shape (stable for clients):

    {"error": {"code": "<dotted-code>", "message": "...", "retryable": bool}}

A ``Retry-After`` header is emitted whenever the error carries a
``retry_after`` hint (e.g. an upstream 429), so clients can back off correctly.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bifrost.errors import BifrostError

logger = structlog.stdlib.get_logger(__name__)


async def bifrost_error_handler(request: Request, exc: BifrostError) -> JSONResponse:
    """Map a ``BifrostError`` to its deterministic HTTP response."""
    body = {
        "error": {
            "code": exc.code,
            "message": exc.message or str(exc),
            "retryable": exc.retryable,
        }
    }
    headers: dict[str, str] = {}
    if exc.retry_after is not None:
        # HTTP Retry-After is an integer number of seconds.
        headers["Retry-After"] = str(max(0, int(exc.retry_after)))

    # 5xx are our problem; log them with the stack. 4xx are the caller's
    # input — log at info without the noise of a traceback.
    log = logger.bind(
        code=exc.code,
        http_status=exc.http_status,
        retryable=exc.retryable,
        path=request.url.path,
    )
    if exc.http_status >= 500:
        log.error("bifrost.error", exc_info=exc)
    else:
        log.info("bifrost.error")

    return JSONResponse(status_code=exc.http_status, content=body, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    """Register Bifrost's exception handlers on the app."""
    app.add_exception_handler(BifrostError, bifrost_error_handler)  # type: ignore[arg-type]
