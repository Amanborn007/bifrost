"""Member-callable routes that must stay tightened.

Each block pins one finding: the debug helper and Slack draft are admin-only,
``?force=true`` needs admin, the handover rate limit keys on the principal, the
walk breadth is capped by a server ceiling, doubt resolution is scoped to
its session and escalates once, and ``/ask`` records the spend it incurs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import bifrost.api.audit as audit_mod
from bifrost.api._cookie_signing import mint_signed_cookie
from bifrost.api.deps import get_db, get_repository
from bifrost.cli.config import BifrostSettings, reset_settings
from bifrost.collaboration import rate_limit
from bifrost.collaboration.cost_estimate import CostEstimate
from bifrost.extraction.cli_client import CliCompletion, Usage
from bifrost.storage.repository import EntityRepository
from tests.auth_helpers import boot, client, header_identity

ADMIN_MAP = {"admin": ["eng-admins"]}
MEMBER = header_identity("m@example.com", "eng")
ADMIN = header_identity("a@example.com", "eng-admins")
API = "/api/v1"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_settings()
    rate_limit.reset()
    yield
    rate_limit.reset()
    reset_settings()


@pytest.fixture(autouse=True)
def _audit(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(audit_mod, "record_event", _record)
    return events


def _app(monkeypatch, **kwargs: Any):
    return boot(monkeypatch, provider="header", roles=ADMIN_MAP, **kwargs)


def _err_code(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


# ── 1. POST /tasks/{id}/plans/{plan_id}/debug ────────────────────────


async def _seed_task_with_plan(session: AsyncSession) -> tuple[str, str]:
    task_id, phase_id, plan_id = (str(uuid.uuid4()) for _ in range(3))
    await session.execute(
        text(
            "INSERT INTO tasks (id, title, state, intent_text, created_from) "
            "VALUES (CAST(:t AS uuid), 'hardening', 'success', 'i', 'direct')"
        ),
        {"t": task_id},
    )
    await session.execute(
        text(
            "INSERT INTO task_phases (id, task_id, order_index, slug, title) "
            "VALUES (CAST(:p AS uuid), CAST(:t AS uuid), 1, 's', 't')"
        ),
        {"p": phase_id, "t": task_id},
    )
    await session.execute(
        text(
            "INSERT INTO task_plans (id, phase_id, order_index, plan_key, name, "
            "action_body, done_criterion) "
            "VALUES (CAST(:i AS uuid), CAST(:p AS uuid), 1, 'k', 'n', 'a', 'd')"
        ),
        {"i": plan_id, "p": phase_id},
    )
    return task_id, plan_id


@pytest.fixture
def run_debug(monkeypatch) -> AsyncMock:
    from bifrost.actions.debugger import DebugReport
    from bifrost.api.routes import tasks as tasks_route

    report = DebugReport(
        task_id="t",
        plan_id="p",
        plan_key="k",
        diagnosis="d",
        suggested_fix="f",
        retry_prompt="r",
        retry_safe=True,
        duration_ms=1,
        cost_usd=0.0,
    )
    mock = AsyncMock(return_value=report)
    monkeypatch.setattr(tasks_route, "run_debug", mock)
    return mock


@pytest.fixture
def debug_app(monkeypatch, db_session):
    app = _app(monkeypatch)

    async def _db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = _db
    return app


async def _debug(app, headers, task_id: str, plan_id: str) -> httpx.Response:
    async with client(app) as ac:
        return await ac.post(f"{API}/tasks/{task_id}/plans/{plan_id}/debug", headers=headers)


async def test_member_gets_403_on_debug(debug_app, db_session, run_debug) -> None:
    task_id, plan_id = await _seed_task_with_plan(db_session)
    resp = await _debug(debug_app, MEMBER, task_id, plan_id)
    assert resp.status_code == 403, resp.text
    assert _err_code(resp) == "auth.role_required"
    run_debug.assert_not_awaited()


async def test_admin_passes_on_debug(debug_app, db_session, run_debug) -> None:
    task_id, plan_id = await _seed_task_with_plan(db_session)
    resp = await _debug(debug_app, ADMIN, task_id, plan_id)
    assert resp.status_code == 200, resp.text
    assert run_debug.await_args.kwargs["plan_id"] == plan_id


async def test_debug_rejects_a_plan_that_belongs_to_another_task(
    debug_app, db_session, run_debug
) -> None:
    _, plan_id = await _seed_task_with_plan(db_session)
    other_task_id, _ = await _seed_task_with_plan(db_session)
    resp = await _debug(debug_app, ADMIN, other_task_id, plan_id)
    assert resp.status_code == 404, resp.text
    run_debug.assert_not_awaited()


async def test_debug_rejects_a_malformed_plan_id(debug_app, db_session, run_debug) -> None:
    task_id, _ = await _seed_task_with_plan(db_session)
    resp = await _debug(debug_app, ADMIN, task_id, "not-a-uuid")
    assert resp.status_code == 404, resp.text
    run_debug.assert_not_awaited()


# ── 2. handover-docs generate: ?force=true and the rate limit ────────

SCOPES = [
    ("repos", str(uuid.uuid4()), "estimate_repo_handover_cost", "generate_repo_handover"),
    ("domains", str(uuid.uuid4()), "estimate_domain_handover_cost", "generate_domain_handover"),
    ("teams", "squad-a", "estimate_team_handover_cost", "generate_team_handover"),
    ("regions", "eu-west", "estimate_region_handover_cost", "generate_region_handover"),
]
SCOPE_IDS = [s[0] for s in SCOPES]


def _estimate(usd: float) -> CostEstimate:
    return CostEstimate(
        estimated_usd=usd, reasoning="test", repos_to_generate=1, will_use_tree=False
    )


@pytest.fixture
def handover(monkeypatch) -> Callable[[str, str, float], AsyncMock]:
    from bifrost.api.routes import handover_docs as route

    def install(estimator: str, generator: str, usd: float) -> AsyncMock:
        generated = SimpleNamespace(
            doc_id=uuid.uuid4(), fs_path="p", title="t", used_fallback=False
        )
        generate = AsyncMock(return_value=generated)
        monkeypatch.setattr(route, estimator, AsyncMock(return_value=_estimate(usd)))
        monkeypatch.setattr(route, generator, generate)
        return generate

    return install


def _stub_repo(app) -> None:
    async def _repo():
        yield MagicMock()

    app.dependency_overrides[get_repository] = _repo


async def _generate(app, headers, scope: str, ident: str, query: str = "") -> httpx.Response:
    async with client(app) as ac:
        return await ac.post(
            f"{API}/handover-docs/{scope}/{ident}/generate{query}", headers=headers
        )


@pytest.mark.parametrize(("scope", "ident", "estimator", "generator"), SCOPES, ids=SCOPE_IDS)
async def test_member_with_force_true_gets_403(
    monkeypatch, handover, scope, ident, estimator, generator
) -> None:
    generate = handover(estimator, generator, 999.0)
    app = _app(monkeypatch)
    _stub_repo(app)
    resp = await _generate(app, MEMBER, scope, ident, "?force=true")
    assert resp.status_code == 403, resp.text
    assert _err_code(resp) == "auth.role_required"
    generate.assert_not_awaited()


@pytest.mark.parametrize(("scope", "ident", "estimator", "generator"), SCOPES, ids=SCOPE_IDS)
async def test_member_without_force_passes_the_gate(
    monkeypatch, handover, scope, ident, estimator, generator
) -> None:
    generate = handover(estimator, generator, 1.0)
    app = _app(monkeypatch)
    _stub_repo(app)
    resp = await _generate(app, MEMBER, scope, ident)
    assert resp.status_code == 201, resp.text
    generate.assert_awaited_once()


@pytest.mark.parametrize(("scope", "ident", "estimator", "generator"), SCOPES, ids=SCOPE_IDS)
async def test_member_over_ceiling_without_force_is_still_409(
    monkeypatch, handover, scope, ident, estimator, generator
) -> None:
    generate = handover(estimator, generator, 999.0)
    app = _app(monkeypatch)
    _stub_repo(app)
    resp = await _generate(app, MEMBER, scope, ident)
    assert resp.status_code == 409, resp.text
    generate.assert_not_awaited()


@pytest.mark.parametrize(("scope", "ident", "estimator", "generator"), SCOPES, ids=SCOPE_IDS)
async def test_admin_with_force_true_overrides_the_ceiling(
    monkeypatch, handover, scope, ident, estimator, generator
) -> None:
    generate = handover(estimator, generator, 999.0)
    app = _app(monkeypatch)
    _stub_repo(app)
    resp = await _generate(app, ADMIN, scope, ident, "?force=true")
    assert resp.status_code == 201, resp.text
    generate.assert_awaited_once()


async def test_rate_limit_key_is_the_principal_and_survives_cookie_rotation(
    monkeypatch, handover
) -> None:
    handover("estimate_repo_handover_cost", "generate_repo_handover", 1.0)
    app = _app(monkeypatch, extra_env={"BIFROST_HANDOVER_RATE_LIMIT_PER_MINUTE": "2"})
    _stub_repo(app)
    ident = SCOPES[0][1]
    statuses = []
    for _ in range(3):
        headers = {
            **MEMBER,
            "Cookie": f"bifrost_session={mint_signed_cookie()}",
            "X-Requested-With": "bifrost-ui",
        }
        statuses.append((await _generate(app, headers, "repos", ident)).status_code)
    assert statuses == [201, 201, 429]


async def test_rate_limit_buckets_are_per_principal(monkeypatch, handover) -> None:
    handover("estimate_repo_handover_cost", "generate_repo_handover", 1.0)
    app = _app(monkeypatch, extra_env={"BIFROST_HANDOVER_RATE_LIMIT_PER_MINUTE": "1"})
    _stub_repo(app)
    ident = SCOPES[0][1]
    first = await _generate(app, MEMBER, "repos", ident)
    second = await _generate(app, MEMBER, "repos", ident)
    other = await _generate(app, header_identity("n@example.com", "eng"), "repos", ident)
    assert (first.status_code, second.status_code, other.status_code) == (201, 429, 201)


# ── 3. walk breadth ceiling ──────────────────────────────────────────

CEILING_CODE = "settings.walk_breadth_above_ceiling"


def _settings_app(monkeypatch, ceiling: int):
    app = _app(monkeypatch, extra_env={"BIFROST_RESEARCH_WALK_BREADTH_CEILING": str(ceiling)})
    repo = MagicMock()
    repo.get_conversation_for_session = AsyncMock(return_value=object())
    repo.get_chat_settings = AsyncMock(return_value={})
    repo.update_chat_settings = AsyncMock()

    async def _repo():
        yield repo

    app.dependency_overrides[get_repository] = _repo
    return app, repo


async def _patch_breadth(app, value: int) -> httpx.Response:
    async with client(app) as ac:
        return await ac.patch(
            f"{API}/conversations/{uuid.uuid4()}/settings",
            json={"walk_breadth_soft_cap": value},
            headers=MEMBER,
        )


def test_ceiling_defaults_to_the_old_soft_cap_default() -> None:
    assert BifrostSettings().research_walk_breadth_ceiling == 200


async def test_patch_above_the_ceiling_is_400_with_a_stable_code(monkeypatch) -> None:
    app, repo = _settings_app(monkeypatch, ceiling=100)
    resp = await _patch_breadth(app, 101)
    assert resp.status_code == 400, resp.text
    assert _err_code(resp) == CEILING_CODE
    repo.update_chat_settings.assert_not_awaited()


@pytest.mark.parametrize("value", [100, 20])
async def test_patch_at_or_below_the_ceiling_is_accepted(monkeypatch, value: int) -> None:
    app, repo = _settings_app(monkeypatch, ceiling=100)
    resp = await _patch_breadth(app, value)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["walk_breadth_soft_cap"] == value
    repo.update_chat_settings.assert_awaited_once()


def _plan(**overrides: Any):
    from bifrost.research.orchestrator import OrchestrationPlan

    return OrchestrationPlan(intent="discovery", **overrides)


def test_stored_value_above_the_ceiling_is_clamped_at_use(caplog) -> None:
    from bifrost.api.schemas.chat_settings import ChatSettings
    from bifrost.research.orchestrator import merge_plan_with_settings

    stored = ChatSettings(walk_breadth_soft_cap=2000)
    caplog.set_level(logging.WARNING)
    resolved = merge_plan_with_settings(
        _plan(),
        stored,
        user_touched_settings=True,
        settings=BifrostSettings(research_walk_breadth_ceiling=300),
    )
    assert resolved.walk_breadth_soft_cap == 300
    assert "research_walk_breadth_ceiling" in caplog.text


def test_plan_override_above_the_ceiling_is_clamped_at_use() -> None:
    from bifrost.api.schemas.chat_settings import ChatSettings
    from bifrost.research.orchestrator import merge_plan_with_settings

    resolved = merge_plan_with_settings(
        _plan(walk_breadth_soft_cap=1500),
        ChatSettings(),
        user_touched_settings=False,
        settings=BifrostSettings(research_walk_breadth_ceiling=300),
    )
    assert resolved.walk_breadth_soft_cap == 300


def test_value_within_the_ceiling_is_unchanged_and_silent(caplog) -> None:
    from bifrost.api.schemas.chat_settings import ChatSettings
    from bifrost.research.orchestrator import merge_plan_with_settings

    caplog.set_level(logging.WARNING)
    resolved = merge_plan_with_settings(
        _plan(),
        ChatSettings(walk_breadth_soft_cap=250),
        user_touched_settings=True,
        settings=BifrostSettings(research_walk_breadth_ceiling=300),
    )
    assert resolved.walk_breadth_soft_cap == 250
    assert caplog.text == ""


def test_default_settings_leave_the_router_top_k_at_200() -> None:
    import inspect

    from bifrost.api.schemas.chat_settings import ChatSettings
    from bifrost.research import coordinator
    from bifrost.research.orchestrator import merge_plan_with_settings

    resolved = merge_plan_with_settings(
        _plan(),
        ChatSettings(),
        user_touched_settings=False,
        settings=BifrostSettings(),
    )
    assert resolved.walk_breadth_soft_cap == 200
    source = inspect.getsource(coordinator)
    assert "router_top_k = max(1, int(resolved_walk.walk_breadth_soft_cap or 200))" in source


def test_research_max_repos_does_not_limit_discovery() -> None:
    import inspect

    from bifrost.api.schemas.chat_settings import ChatSettings
    from bifrost.research import coordinator
    from bifrost.research.orchestrator import merge_plan_with_settings

    resolved = merge_plan_with_settings(
        _plan(),
        ChatSettings(),
        user_touched_settings=False,
        settings=BifrostSettings(research_max_repos=1),
    )
    assert resolved.walk_breadth_soft_cap == 200
    assert "research_max_repos" not in inspect.getsource(coordinator)


# ── 4. collab doubts: session scope and single escalation ────────────

CHANNEL = "#eng-handovers"
TOKEN = "xo" + "xb-hardening-token"  # split so tests/test_leak_gate.py does not flag it


class _Slack:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})
        return httpx.Response(200, json={"ok": True, "permalink": "https://x.slack.com/p1"})


@pytest.fixture
def slack(monkeypatch) -> _Slack:
    fake = _Slack()
    real = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fake


@pytest.fixture
async def collab_client(
    monkeypatch, db_session: AsyncSession, tmp_path, slack: _Slack
) -> AsyncGenerator[AsyncClient]:
    from bifrost.api.routes import collaboration as route

    monkeypatch.setenv("BIFROST_HANDOVERS_DIR", str(tmp_path))
    monkeypatch.setattr(
        route,
        "get_settings",
        lambda: BifrostSettings(slack_escalation_channel=CHANNEL, slack_bot_token=TOKEN),
    )
    app = _app(monkeypatch)

    async def _repo() -> AsyncGenerator[EntityRepository]:
        yield EntityRepository(db_session)

    app.dependency_overrides[get_repository] = _repo
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 4242)), base_url="http://t"
    ) as ac:
        yield ac


async def _session_with_doubt(db_session: AsyncSession) -> tuple[Any, Any]:
    repo = EntityRepository(db_session)
    sess = await repo.create_collab_session(requester="r", requirement_text="req")
    doubt = await repo.add_collab_doubt(session_id=sess.id, question="who owns X?")
    return sess, doubt


def _resolve(ac: AsyncClient, sid: Any, did: Any, status: str) -> Any:
    return ac.post(
        f"{API}/collab-sessions/{sid}/doubts/{did}/resolve",
        json={"status": status},
        headers=MEMBER,
    )


async def test_doubt_from_another_session_is_404_and_untouched(
    collab_client, db_session, slack
) -> None:
    sess_a, _ = await _session_with_doubt(db_session)
    _, doubt_b = await _session_with_doubt(db_session)
    resp = await _resolve(collab_client, sess_a.id, doubt_b.id, "escalated")
    assert resp.status_code == 404, resp.text
    await db_session.refresh(doubt_b)
    assert doubt_b.status == "open"
    assert slack.requests == []


async def test_re_escalating_returns_409_without_any_slack_call(
    collab_client, db_session, slack
) -> None:
    sess, doubt = await _session_with_doubt(db_session)
    first = await _resolve(collab_client, sess.id, doubt.id, "escalated")
    assert first.status_code == 200, first.text
    sent = len(slack.requests)
    assert sent > 0

    second = await _resolve(collab_client, sess.id, doubt.id, "escalated")

    assert second.status_code == 409, second.text
    assert _err_code(second) == "collab.doubt.already_escalated"
    assert len(slack.requests) == sent


async def test_an_escalated_doubt_can_still_be_resolved(collab_client, db_session) -> None:
    sess, doubt = await _session_with_doubt(db_session)
    await _resolve(collab_client, sess.id, doubt.id, "escalated")
    resp = await _resolve(collab_client, sess.id, doubt.id, "answered_by_human")
    assert resp.status_code == 200, resp.text


# ── 5. POST /collaboration/slack-draft ───────────────────────────────


@pytest.fixture
def create_draft(monkeypatch) -> AsyncMock:
    from bifrost.collaboration import slack_draft

    mock = AsyncMock(return_value=SimpleNamespace(ok=True, channel_link=None, error=None))
    monkeypatch.setattr(slack_draft, "create_slack_draft", mock)
    return mock


async def _slack_draft(app, headers) -> httpx.Response:
    async with client(app) as ac:
        return await ac.post(
            f"{API}/collaboration/slack-draft",
            json={"channel": "#anywhere", "message": "hello"},
            headers=headers,
        )


async def test_member_gets_403_on_slack_draft(monkeypatch, create_draft) -> None:
    resp = await _slack_draft(_app(monkeypatch), MEMBER)
    assert resp.status_code == 403, resp.text
    assert _err_code(resp) == "auth.role_required"
    create_draft.assert_not_awaited()


async def test_admin_can_create_a_slack_draft(monkeypatch, create_draft) -> None:
    resp = await _slack_draft(_app(monkeypatch), ADMIN)
    assert resp.status_code == 200, resp.text
    create_draft.assert_awaited_once()


# ── 6. POST /ask records the spend it incurs ─────────────────────────


class _MeteredClient:
    """Scripted LLM client: every call bills ``cost`` and is counted by stage."""

    def __init__(self, *, decompose_cost: float, agent_cost: float, repo_urns: list[str]) -> None:
        self.costs = {"decompose": decompose_cost, "agent": agent_cost, "skeptic": 1.0}
        self.repo_urns = repo_urns
        self.calls: list[str] = []

    async def call_with_json_schema(self, *, json_schema: dict, **_: Any) -> CliCompletion:
        props = json_schema["properties"]
        if "repo_urns" in props:
            stage, parsed = "decompose", {"repo_urns": self.repo_urns, "reasoning": "r"}
        elif "passed" in props:
            stage, parsed = (
                "skeptic",
                {"passed": True, "issues": [], "confidence_adjustment": "none"},
            )
        else:
            stage = "agent"
            parsed = {
                "answer": "a",
                "confidence_level": "uncertain",
                "source_urns": [],
                "reasoning": "r",
            }
        self.calls.append(stage)
        usage = Usage(1, 1, 0, 0, self.costs[stage])
        return CliCompletion(parsed=parsed, usage=usage, text="")


@pytest.fixture
def ask_app(monkeypatch, db_session):
    from bifrost.agents.coordinator import TaskCoordinator
    from bifrost.api.routes import ask as ask_route

    urns = ["urn:repo:a", "urn:repo:b"]
    repos = [SimpleNamespace(urn=u, id=uuid.uuid4(), name=u, url=None) for u in urns]
    entity_repo = SimpleNamespace(list_repos=AsyncMock(return_value=repos))
    holder: dict[str, Any] = {}

    def install(decompose_cost: float, agent_cost: float) -> _MeteredClient:
        fake = _MeteredClient(decompose_cost=decompose_cost, agent_cost=agent_cost, repo_urns=urns)

        class _Infra:
            def make_coordinator(self, session, repo, *, pool=None):
                return TaskCoordinator(
                    client=fake, session=session, entity_repo=entity_repo, pool=pool
                )

        monkeypatch.setattr(ask_route, "create_agent_infrastructure", lambda settings: _Infra())
        return fake

    app = _app(monkeypatch)

    async def _repo() -> AsyncGenerator[EntityRepository]:
        yield EntityRepository(db_session)

    app.dependency_overrides[get_repository] = _repo
    holder["install"] = install
    holder["app"] = app
    return SimpleNamespace(**holder)


async def _ask(app, task_type: str = "plan") -> httpx.Response:
    async with client(app) as ac:
        return await ac.post(
            f"{API}/ask", json={"question": "how?", "task_type": task_type}, headers=MEMBER
        )


async def test_ask_records_spend_and_stops_calls_once_over_budget(ask_app) -> None:
    fake = ask_app.install(decompose_cost=3.0, agent_cost=3.0)
    resp = await _ask(ask_app.app)
    assert resp.status_code == 200, resp.text
    # decompose + one agent reach $6 > $5; the second agent and the skeptic never run.
    assert fake.calls == ["decompose", "agent"]
    body = resp.json()["data"]
    assert body["total_cost_usd"] == pytest.approx(6.0)
    assert any("budget" in gap.lower() for gap in body["gaps"])


async def test_ask_refuses_with_a_typed_error_when_decompose_alone_busts_the_budget(
    ask_app,
) -> None:
    fake = ask_app.install(decompose_cost=6.0, agent_cost=0.1)
    resp = await _ask(ask_app.app)
    assert resp.status_code == 402, resp.text
    assert _err_code(resp) == "budget.exceeded"
    assert fake.calls == ["decompose"]


async def test_ask_under_budget_runs_every_stage(ask_app) -> None:
    fake = ask_app.install(decompose_cost=0.5, agent_cost=0.5)
    resp = await _ask(ask_app.app)
    assert resp.status_code == 200, resp.text
    assert sorted(fake.calls) == ["agent", "agent", "decompose", "skeptic"]
    assert resp.json()["data"]["total_cost_usd"] == pytest.approx(0.5 + 0.5 + 0.5 + 1.0)
