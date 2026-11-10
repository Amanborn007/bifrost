"""Pattern reuse — S7 V1 stub endpoints.

S7 §14 specifies three V1-stub endpoints that return HTTP 501 with a
documented JSON body. Admin CRUD on the Pattern entity is also part of
the V1 surface but is deferred to a later cut (it needs a Pattern model,
a migration, and a lifecycle enum — none of which exist yet).

These stubs let API consumers (orchestrator, planning agents, doc tools)
code against the contract today, returning a known shape instead of a
404 that looks like a deployment bug.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

router = APIRouter(prefix="/patterns", tags=["patterns"])


NOT_IMPLEMENTED_BODY: dict[str, str] = {
    "error": "not_implemented",
    "message": (
        "Pattern discovery and classification are V2 features. "
        "Use the admin pattern registry to manually register patterns."
    ),
    "docs": "See S7 (pattern-reuse spec) section 14 for V1 scope.",
}


def _not_implemented(response: Response) -> dict[str, str]:
    response.status_code = status.HTTP_501_NOT_IMPLEMENTED
    return NOT_IMPLEMENTED_BODY


@router.post("/search", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def search_patterns(
    response: Response, _body: dict[str, Any] | None = None
) -> dict[str, str]:
    """Pattern search — V2 feature, returns 501 in V1 per S7 section 14."""
    return _not_implemented(response)


@router.post("/classify", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def classify_pattern(
    response: Response, _body: dict[str, Any] | None = None
) -> dict[str, str]:
    """New-vs-existing classification — V2 feature, returns 501 in V1."""
    return _not_implemented(response)


@router.post("/recommend", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def recommend_patterns(
    response: Response, _body: dict[str, Any] | None = None
) -> dict[str, str]:
    """Reuse recommendation — V2 feature, returns 501 in V1."""
    return _not_implemented(response)
