"""Route inventory: every mutating route is admin-gated or explicitly a
member action, and every /admin and /audit route is admin-gated.

A new route that is not classified here fails this test, so adding an
endpoint forces a decision about who may call it.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from bifrost.api.app import create_app

API = "/api/v1"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Mutating routes any authenticated member may call. Everything else that
# mutates must require the admin role.
MEMBER_MUTATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # public identity endpoints (handled before the identity gate)
        ("POST", "/auth/refresh"),
        ("POST", "/auth/logout"),
        # asking questions and chat
        ("POST", "/query"),
        ("POST", "/ask"),
        ("POST", "/dependencies/blast-radius"),
        ("POST", "/intent/classify"),
        ("POST", "/conversations"),
        ("POST", "/conversations/seed"),
        ("PATCH", "/conversations/{conversation_id}/settings"),
        ("PATCH", "/conversations/{conversation_id}"),
        ("DELETE", "/conversations/{conversation_id}"),
        ("POST", "/conversations/{conversation_id}/restore"),
        ("POST", "/conversations/{conversation_id}/messages"),
        ("POST", "/conversations/{conversation_id}/cancel"),
        ("POST", "/conversations/{conversation_id}/agents/{agent_key}/cancel"),
        ("POST", "/conversations/{conversation_id}/messages/{message_id}/feedback"),
        ("POST", "/feedback"),
        ("POST", "/patterns/search"),
        ("POST", "/patterns/classify"),
        ("POST", "/patterns/recommend"),
        # task drafting that neither executes code nor writes to Jira
        ("POST", "/tasks"),
        ("POST", "/tasks/from-jira"),
        ("POST", "/tasks/{task_id}/link"),
        ("POST", "/tasks/{task_id}/promote-message"),
        ("POST", "/tasks/{task_id}/gap-check"),
        ("POST", "/tasks/{task_id}/clarifications/{clarification_id}/answer"),
        ("POST", "/tasks/{task_id}/discuss"),
        ("POST", "/tasks/{task_id}/plan"),
        ("POST", "/tasks/{task_id}/enrich-from-jira"),
        ("POST", "/tasks/{task_id}/archive"),
        ("POST", "/tasks/{task_id}/unarchive"),
        ("POST", "/conversations/{conversation_id}/spawn-task"),
        ("POST", "/conversations/{conversation_id}/spawn-task/preview"),
        # collaboration
        ("POST", "/collab-sessions"),
        ("POST", "/collab-sessions/{session_id}/messages"),
        ("POST", "/collab-sessions/{session_id}/doubts/{doubt_id}/resolve"),
        ("POST", "/collab-sessions/{session_id}/close"),
        ("POST", "/collab-sessions/{session_id}/reopen"),
        ("POST", "/collab-sessions/{session_id}/link-task"),
        ("POST", "/collaboration/asks"),
        ("POST", "/collaboration/asks/{ask_id}/mark-sent"),
        ("POST", "/collaboration/asks/{ask_id}/mark-answered"),
        ("POST", "/collaboration/asks/{ask_id}/escalate"),
        ("POST", "/collaboration/asks/{ask_id}/cancel"),
        # handover docs (single-scope generation is cost-capped per request)
        ("POST", "/handover-docs/repos/{repo_id}/generate"),
        ("POST", "/handover-docs/domains/{domain_id}/generate"),
        ("POST", "/handover-docs/teams/{squad}/generate"),
        ("POST", "/handover-docs/regions/{region}/generate"),
        ("POST", "/handover-docs/{doc_id}/promote-candidate"),
        ("PATCH", "/handover-docs/{doc_id}/body"),
        ("POST", "/handover-docs/{doc_id}/sections/{section_key}/regenerate"),
        ("DELETE", "/handover-docs/{doc_id}"),
        ("POST", "/handover-docs/{doc_id}/restore"),
        ("POST", "/handover-docs/{doc_id}/section-feedback"),
        ("POST", "/handover-docs/{doc_id}/section-feedback/{feedback_id}/resolve"),
        ("POST", "/handover-docs/{doc_id}/restore-version"),
    }
)

# Routes the identity middleware serves without a principal; they cannot be
# role-gated.
PUBLIC_MUTATIONS = frozenset({("POST", "/auth/refresh"), ("POST", "/auth/logout")})


def _requires_admin(route: APIRoute) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if getattr(dep.call, "required_role", None) == "admin":
            return True
        stack.extend(dep.dependencies)
    return False


@pytest.fixture(scope="module")
def routes() -> list[tuple[str, str, APIRoute]]:
    out: list[tuple[str, str, APIRoute]] = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(API + "/"):
            continue
        path = route.path[len(API) :]
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path, route))
    return out


def test_every_mutating_route_is_admin_or_a_declared_member_action(routes) -> None:
    unclassified = [
        f"{m} {p}"
        for m, p, r in routes
        if m in MUTATING and not _requires_admin(r) and (m, p) not in MEMBER_MUTATIONS
    ]
    assert unclassified == [], "classify these routes (admin gate or MEMBER_MUTATIONS)"


def test_member_allowlist_has_no_stale_or_admin_entries(routes) -> None:
    by_key = {(m, p): r for m, p, r in routes}
    stale = sorted(k for k in MEMBER_MUTATIONS if k not in by_key)
    assert stale == [], f"MEMBER_MUTATIONS lists routes that do not exist: {stale}"
    gated = sorted(
        k
        for k in MEMBER_MUTATIONS
        if k in by_key and k not in PUBLIC_MUTATIONS and _requires_admin(by_key[k])
    )
    assert gated == [], f"routes listed as member actions are admin-gated: {gated}"


def test_admin_and_audit_routes_all_require_admin(routes) -> None:
    ungated = [
        f"{m} {p}"
        for m, p, r in routes
        if (p.startswith("/admin") or p.startswith("/audit")) and not _requires_admin(r)
    ]
    assert ungated == []
    assert any(p.startswith("/admin") for _, p, _ in routes)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/repos"),
        ("POST", "/repos/{repo_id}/sync-ownership"),
        ("DELETE", "/repos/{repo_id}"),
        ("POST", "/repos/{repo_id}/extract"),
        ("POST", "/domains"),
        ("PUT", "/domains/{domain_id}"),
        ("DELETE", "/domains/{domain_id}"),
        ("POST", "/domains/{domain_id}/repos"),
        ("POST", "/domains/{domain_id}/discover-repos"),
        ("DELETE", "/domains/{domain_id}/repos/{repo_id}"),
        ("POST", "/extraction-tasks"),
        ("DELETE", "/extraction-tasks/{task_id}"),
        ("POST", "/extraction-tasks/{task_id}/pause"),
        ("POST", "/extraction-tasks/{task_id}/resume"),
        ("POST", "/extract"),
        ("POST", "/catalog/sync"),
        ("POST", "/indexer/repos/{repo_id}"),
        ("POST", "/indexer/all"),
        ("POST", "/handover-docs/bulk/refresh-stale"),
        ("POST", "/tasks/{task_id}/execute"),
        ("POST", "/tasks/{task_id}/plans/{plan_id}/retry"),
        ("POST", "/tasks/{task_id}/worktree/remove"),
        ("POST", "/tasks/{task_id}/verify"),
        ("POST", "/tasks/{task_id}/create-pr"),
        ("POST", "/quick"),
        ("POST", "/tasks/{task_id}/jira-retract"),
        ("POST", "/tasks/{task_id}/advance"),
        ("POST", "/tasks/{task_id}/cancel"),
        ("POST", "/tasks/{task_id}/artifacts/{artifact_id}/approve"),
        ("POST", "/tasks/{task_id}/verify-items/{item_id}/override"),
        ("POST", "/tasks/{task_id}/tools/research"),
        ("POST", "/tasks/{task_id}/tools/extract"),
        ("POST", "/tasks/{task_id}/tools/ask-user"),
        ("POST", "/tasks/auto-archive"),
    ],
)
def test_corpus_spend_and_execution_routes_are_admin(routes, method: str, path: str) -> None:
    by_key = {(m, p): r for m, p, r in routes}
    assert (method, path) in by_key, f"{method} {path} is not a route"
    assert _requires_admin(by_key[(method, path)])
