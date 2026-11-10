"""CSRF defence for cookie/proxy-authenticated browser sessions.

A mutating request (POST/PUT/PATCH/DELETE) that looks like it came from
a browser must come from a configured CORS origin and carry
``X-Requested-With: bifrost-ui``, a header a cross-site form or
``fetch`` cannot add without passing the CORS preflight.

"Looks like a browser" means the request carries ``Origin``,
``Sec-Fetch-Site`` or a ``Cookie``. Bearer-token clients are exempt (a
browser cannot attach ``Authorization`` cross-origin without a
preflight), and so are plain non-browser clients that send none of the
above.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

CSRF_HEADER_NAME = "x-requested-with"
CSRF_HEADER_VALUE = "bifrost-ui"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})


class CsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        super().__init__(app)
        self._allowed_origins = frozenset(o.rstrip("/") for o in allowed_origins)

    @staticmethod
    def _reject(request: Request, code: str, detail: str) -> JSONResponse:
        logger.warning(
            "csrf.rejected code=%s method=%s path=%s", code, request.method, request.url.path
        )
        return JSONResponse(
            status_code=403,
            content={"error": "csrf check failed", "detail": detail, "code": code},
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in MUTATING_METHODS:
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return await call_next(request)

        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        has_cookie = bool(request.headers.get("cookie"))
        if origin is None and fetch_site is None and not has_cookie:
            return await call_next(request)

        if origin is not None:
            if origin.rstrip("/") not in self._allowed_origins:
                return self._reject(request, "csrf_origin", "Origin is not an allowed origin")
        elif fetch_site is not None and fetch_site.lower() not in SAME_ORIGIN_FETCH_SITES:
            return self._reject(request, "csrf_origin", "cross-site request refused")

        if request.headers.get(CSRF_HEADER_NAME) != CSRF_HEADER_VALUE:
            return self._reject(
                request,
                "csrf_header",
                f"mutating browser requests need {CSRF_HEADER_NAME}: {CSRF_HEADER_VALUE}",
            )
        return await call_next(request)
