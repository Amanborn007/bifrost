"""Tests for JiraAdapter — mocked Jira REST API via httpx transport."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from bifrost.adapters.base import (
    AdapterScope,
    ScopeFilter,
    SourceObjectRef,
    SourceObjectType,
    UserContext,
)
from bifrost.adapters.jira import (
    JiraAdapter,
    JiraAuthenticationError,
    JiraNotFoundError,
    JiraRateLimitError,
    _adf_to_text,
)

# ── Fixtures ────────────────────────────────────────────────────────

_BASE_URL = "https://acme.atlassian.net"


def _make_issue(
    key: str = "PROJ-1",
    summary: str = "Test issue",
    issue_type: str = "Task",
    status: str = "Open",
    priority: str = "Medium",
    labels: list[str] | None = None,
    description_adf: dict | None = None,
    comments: list[dict] | None = None,
    issuelinks: list[dict] | None = None,
    parent: dict | None = None,
) -> dict:
    """Build a minimal Jira issue response dict."""
    return {
        "key": key,
        "id": "10001",
        "self": f"{_BASE_URL}/rest/api/3/issue/{key}",
        "fields": {
            "summary": summary,
            "issuetype": {"name": issue_type},
            "status": {"name": status},
            "priority": {"name": priority},
            "created": "2026-04-10T10:00:00.000+0000",
            "updated": "2026-04-14T12:00:00.000+0000",
            "creator": {"displayName": "Alice Dev", "accountId": "abc123"},
            "assignee": {"displayName": "Bob Eng", "accountId": "def456"},
            "labels": labels or [],
            "description": description_adf,
            "comment": {"comments": comments or [], "total": len(comments or [])},
            "issuelinks": issuelinks or [],
            "parent": parent,
        },
    }


def _search_response(issues: list[dict], total: int | None = None) -> dict:
    """Wrap issues in a JQL search response envelope."""
    return {
        "startAt": 0,
        "maxResults": 100,
        "total": total if total is not None else len(issues),
        "issues": issues,
    }


def _make_adapter(handler: Any) -> JiraAdapter:
    """Create a JiraAdapter with a mocked httpx transport."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return JiraAdapter(
        base_url=_BASE_URL,
        email="test@acme.com",
        token="fake-token",
        instance="acme.atlassian.net",
        client=client,
    )


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=data,
    )


# ── ABC compliance ──────────────────────────────────────────────────


def test_abc_compliance():
    """JiraAdapter can be instantiated without TypeError."""
    adapter = JiraAdapter(
        base_url=_BASE_URL,
        email="test@acme.com",
        token="fake-token",
    )
    assert adapter.adapter_id == "jira"


def test_get_capabilities():
    adapter = JiraAdapter(base_url=_BASE_URL, email="a@b.com", token="t")
    caps = adapter.get_capabilities()
    assert caps.source_category.value == "work-intent"
    assert SourceObjectType.ISSUE in caps.supported_object_types
    assert SourceObjectType.EPIC in caps.supported_object_types
    assert caps.supports_permission_check is True
    assert caps.supports_incremental_identify is True


# ── discover / identify ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_yields_issues():
    issues = [
        _make_issue("PROJ-1", "First issue"),
        _make_issue("PROJ-2", "Second issue", issue_type="Epic"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/search" in str(request.url):
            return _json_response(_search_response(issues))
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    scope = AdapterScope(
        source_scope={"project_key": "PROJ"},
    )

    objects = []
    async for obj in adapter.discover(scope):
        objects.append(obj)

    assert len(objects) == 2
    assert objects[0].ref.object_id == "PROJ-1"
    assert objects[0].ref.object_type == SourceObjectType.ISSUE
    assert objects[1].ref.object_type == SourceObjectType.EPIC
    assert "[PROJ-1]" in objects[0].name
    assert "[PROJ-2]" in objects[1].name


@pytest.mark.asyncio
async def test_discover_pagination():
    """Discover handles paginated JQL results."""
    page1_issues = [_make_issue(f"PROJ-{i}") for i in range(1, 4)]
    page2_issues = [_make_issue(f"PROJ-{i}") for i in range(4, 6)]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if "/rest/api/3/search" in str(request.url):
            call_count += 1
            start_at = int(request.url.params.get("startAt", "0"))
            if start_at == 0:
                return _json_response(
                    {
                        "startAt": 0,
                        "maxResults": 3,
                        "total": 5,
                        "issues": page1_issues,
                    }
                )
            return _json_response(
                {
                    "startAt": 3,
                    "maxResults": 3,
                    "total": 5,
                    "issues": page2_issues,
                }
            )
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    scope = AdapterScope(source_scope={"project_key": "PROJ"})

    objects = []
    async for obj in adapter.discover(scope):
        objects.append(obj)

    assert len(objects) == 5
    assert call_count == 2


@pytest.mark.asyncio
async def test_identify_returns_refs():
    issues = [_make_issue("PROJ-1")]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/search" in str(request.url):
            return _json_response(_search_response(issues))
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    scope = AdapterScope(source_scope={"project_key": "PROJ"})
    refs = await adapter.identify(scope)

    assert len(refs) == 1
    assert refs[0].adapter_id == "jira"
    assert refs[0].object_id == "PROJ-1"


# ── extract ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_full_issue():
    description_adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "This is the description."}],
            }
        ],
    }
    comments = [
        {
            "author": {"displayName": "Carol QA"},
            "created": "2026-04-11T09:00:00.000+0000",
            "body": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Looks good!"}]}
                ],
            },
        }
    ]
    issuelinks = [
        {
            "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
            "outwardIssue": {"key": "PROJ-5"},
        }
    ]
    issue = _make_issue(
        "PROJ-1",
        "Fix login bug",
        description_adf=description_adf,
        comments=comments,
        issuelinks=issuelinks,
        labels=["auth", "critical"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/issue/PROJ-1" in str(request.url):
            return _json_response(issue)
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:PROJ-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="PROJ-1",
    )

    result = await adapter.extract(ref)

    assert result.ref.object_id == "PROJ-1"
    assert result.content.kind == "composite"
    assert result.content.parts is not None
    assert len(result.content.parts) == 2

    # Text rendering
    text_part = result.content.parts[0]
    assert text_part.kind == "text"
    assert text_part.text is not None
    assert "Fix login bug" in text_part.text
    assert "This is the description" in text_part.text
    assert "Carol QA" in text_part.text
    assert "Looks good!" in text_part.text
    assert "PROJ-5" in text_part.text

    # Structured data
    structured = result.content.parts[1]
    assert structured.kind == "structured"
    assert structured.data is not None
    assert structured.data["key"] == "PROJ-1"
    assert structured.data["labels"] == ["auth", "critical"]
    assert len(structured.data["comments"]) == 1
    assert len(structured.data["linked_issues"]) == 1

    # Metadata
    assert result.metadata.author == "Alice Dev"
    assert result.metadata.extra is not None
    assert result.metadata.extra["issue_type"] == "Task"

    # Relevance hints from labels
    assert len(result.relevance_hints) == 2


# ── track_revision ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_track_revision():
    issue = _make_issue("PROJ-1")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/issue/PROJ-1" in str(request.url):
            return _json_response(issue)
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:PROJ-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="PROJ-1",
    )

    revision = await adapter.track_revision(ref)
    assert revision.revision_token == "2026-04-14T12:00:00.000+0000"
    assert revision.revision_timestamp is not None


# ── check_permission ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_permission_allowed():
    issue = _make_issue("PROJ-1")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/issue/PROJ-1" in str(request.url):
            return _json_response(issue)
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:PROJ-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="PROJ-1",
    )
    user = UserContext(user_id="user-1")

    result = await adapter.check_permission(user, ref)
    assert result.can_read is True
    assert result.determined is True


@pytest.mark.asyncio
async def test_check_permission_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/issue/" in str(request.url):
            return _json_response({"errorMessages": ["not found"]}, 404)
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:SECRET-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="SECRET-1",
    )
    user = UserContext(user_id="user-1")

    result = await adapter.check_permission(user, ref)
    assert result.can_read is False


# ── health_check ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/myself" in str(request.url):
            return _json_response({"displayName": "Test User", "accountId": "abc123"})
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    health = await adapter.health_check()

    assert health.status == "healthy"
    assert "Test User" in (health.message or "")
    assert health.latency_ms > 0


@pytest.mark.asyncio
async def test_health_check_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/myself" in str(request.url):
            return _json_response({"errorMessages": ["unauthorized"]}, 401)
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    health = await adapter.health_check()

    assert health.status == "unhealthy"
    assert "Authentication" in (health.message or "")


# ── Error handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({}, 401)

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:PROJ-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="PROJ-1",
    )

    with pytest.raises(JiraAuthenticationError):
        await adapter.extract(ref)


@pytest.mark.asyncio
async def test_404_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:PROJ-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="PROJ-1",
    )

    with pytest.raises(JiraNotFoundError):
        await adapter.extract(ref)


@pytest.mark.asyncio
async def test_429_retries_then_raises():
    """Rate limit (429) triggers retry, eventually raises on exhaustion."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=429,
            headers={"Retry-After": "0"},
            json={},
        )

    adapter = _make_adapter(handler)
    ref = SourceObjectRef(
        urn="bifrost:jira:acme.atlassian.net:issue:PROJ-1",
        adapter_id="jira",
        source_instance="acme.atlassian.net",
        object_type=SourceObjectType.ISSUE,
        object_id="PROJ-1",
    )

    with pytest.raises(JiraRateLimitError):
        await adapter.extract(ref)

    assert call_count == 3  # _MAX_RETRIES


# ── get_changes_since ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_changes_since():
    issues = [_make_issue("PROJ-10", "Recent change")]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/search" in str(request.url):
            jql = request.url.params.get("jql", "")
            assert "updated >=" in jql
            assert "2026-04-13" in jql
            return _json_response(_search_response(issues))
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    since = datetime(2026, 4, 13, 0, 0, 0, tzinfo=UTC)
    changed = await adapter.get_changes_since("PROJ", since)

    assert len(changed) == 1
    assert changed[0].ref.object_id == "PROJ-10"


# ── ADF parsing ─────────────────────────────────────────────────────


def test_adf_to_text_simple():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Hello world"}],
            }
        ],
    }
    assert _adf_to_text(adf) == "Hello world"


def test_adf_to_text_nested():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "mention", "attrs": {"text": "Alice"}},
                    {"type": "text", "text": " check this"},
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Item 1"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Item 2"}],
                            }
                        ],
                    },
                ],
            },
        ],
    }
    text = _adf_to_text(adf)
    assert "Hello" in text
    assert "@Alice" in text
    assert "Item 1" in text
    assert "Item 2" in text


def test_adf_to_text_none():
    assert _adf_to_text(None) == ""


def test_adf_to_text_empty_doc():
    assert _adf_to_text({"type": "doc", "content": []}) == ""


# ── Scope / JQL building ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_with_date_filter():
    """Scope filter with modified_after adds JQL date clause."""
    issues = [_make_issue("PROJ-1")]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/api/3/search" in str(request.url):
            jql = request.url.params.get("jql", "")
            assert 'updated >= "2026-04-01 00:00"' in jql
            return _json_response(_search_response(issues))
        return _json_response({}, 404)

    adapter = _make_adapter(handler)
    scope = AdapterScope(
        source_scope={"project_key": "PROJ"},
        filter=ScopeFilter(
            modified_after=datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC),
        ),
    )

    objects = []
    async for obj in adapter.discover(scope):
        objects.append(obj)

    assert len(objects) == 1


# ── Auth header ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_header_is_basic():
    """Authorization header uses Basic scheme, not query params."""
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return _json_response({"displayName": "Test"})

    adapter = _make_adapter(handler)
    await adapter.health_check()

    assert "authorization" in captured_headers
    assert captured_headers["authorization"].startswith("Basic ")
