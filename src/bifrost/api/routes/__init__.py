"""API route groups — real handlers for all Bifrost endpoints.

Each router corresponds to a section in TRD §4.

Three top-level routers, all mounted at ``/api/v1`` by ``app.py``:

  * ``api_router`` — every route needs an authenticated principal (enforced
    by ``IdentityMiddleware``). Mutating routes that are not plain member
    actions add ``AdminOnly`` on their own decorator.

  * ``admin_router_top`` — the admin surface; ``AdminOnly`` on every route.

  * ``public_router`` — health and auth endpoints, no principal needed.
    Liveness probes must succeed regardless of identity state.

Callers should not assume a single APIRouter instance covers the
entire ``/api/v1`` prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from bifrost.api.deps import AdminOnly
from bifrost.api.routes.admin import router as admin_router
from bifrost.api.routes.ask import router as ask_router
from bifrost.api.routes.audit import router as audit_router
from bifrost.api.routes.auth import router as auth_router
from bifrost.api.routes.catalog import router as catalog_router
from bifrost.api.routes.collaboration import (
    collab_primitives_router,
)
from bifrost.api.routes.collaboration import (
    router as collaboration_router,
)
from bifrost.api.routes.conversations import router as conversations_router
from bifrost.api.routes.costs import router as costs_router
from bifrost.api.routes.dependencies import router as dependencies_router
from bifrost.api.routes.domains import router as domains_router
from bifrost.api.routes.entities import router as entities_router
from bifrost.api.routes.extract import router as extract_router
from bifrost.api.routes.extraction_tasks import router as extraction_tasks_router
from bifrost.api.routes.feedback import router as feedback_router
from bifrost.api.routes.handover_docs import router as handover_docs_router
from bifrost.api.routes.health import router as health_router
from bifrost.api.routes.indexer import router as indexer_router
from bifrost.api.routes.integrations import router as integrations_router
from bifrost.api.routes.knowledge import router as knowledge_router
from bifrost.api.routes.ownership import router as ownership_router
from bifrost.api.routes.patterns import router as patterns_router
from bifrost.api.routes.query import router as query_router
from bifrost.api.routes.repos import router as repos_router
from bifrost.api.routes.streaming import router as streaming_router
from bifrost.api.routes.tasks import router as tasks_router

api_router = APIRouter()

# Mount all route groups. ``extraction_tasks_router`` is the
# extraction-job lifecycle (was /tasks before the brief→task rename;
# now /extraction-tasks). ``tasks_router`` is the user-facing
# coding-task lifecycle (was /briefs).
api_router.include_router(repos_router)
api_router.include_router(domains_router)
api_router.include_router(entities_router)
api_router.include_router(extraction_tasks_router)
api_router.include_router(tasks_router)
api_router.include_router(streaming_router)
api_router.include_router(query_router)
api_router.include_router(ask_router)
api_router.include_router(dependencies_router)
api_router.include_router(integrations_router)
api_router.include_router(conversations_router)
api_router.include_router(costs_router)
api_router.include_router(extract_router)
api_router.include_router(feedback_router)
api_router.include_router(knowledge_router)
api_router.include_router(ownership_router)
api_router.include_router(indexer_router)
api_router.include_router(catalog_router)
api_router.include_router(patterns_router)
api_router.include_router(collaboration_router)
api_router.include_router(collab_primitives_router)
api_router.include_router(handover_docs_router)

# Admin router — mounted separately at /api/v1 by app.py. The inner routers
# declare the same gate; repeating it here keeps them admin-only if a future
# refactor strips the inner dependency.
admin_router_top = APIRouter(dependencies=[AdminOnly])
admin_router_top.include_router(admin_router)
admin_router_top.include_router(audit_router)

# Public router — mounted by app.py at /api/v1 alongside api_router. The auth
# router is here because the FE identity gate calls ``/api/v1/auth/me``
# before the operator is authenticated; IdentityMiddleware lists /auth/* in
# its public paths.
public_router = APIRouter()
public_router.include_router(health_router)
public_router.include_router(auth_router)
