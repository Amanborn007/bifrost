"""JiraAdapter — SourceAdapter for Jira REST API v3.

Connects to Jira Cloud via API token authentication, discovers issues/epics
via JQL, extracts issue content (summary, description, comments, links),
and tracks revisions via the issue ``updated`` field.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from bifrost.adapters.base import (
    AdapterCapabilities,
    AdapterScope,
    ContentBody,
    ExtractedContent,
    HealthStatus,
    PermissionResult,
    RelevanceHint,
    RevisionInfo,
    SourceAdapter,
    SourceCategory,
    SourceMetadata,
    SourceObject,
    SourceObjectRef,
    SourceObjectType,
    UserContext,
)
from bifrost.core.http import http_timeout
from bifrost.errors import UpstreamError, with_retry

logger = logging.getLogger(__name__)

_MAX_JQL_PAGE_SIZE = 100
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


# ── Exceptions ──────────────────────────────────────────────────────


class JiraAdapterError(UpstreamError):
    """Base exception for Jira adapter operations."""

    code = "adapter.jira"
    retryable = False
    http_status = 502


class JiraAuthenticationError(JiraAdapterError):
    """Authentication with Jira failed (401)."""

    code = "adapter.jira.auth"
    retryable = False
    # 502, not 401: it's *Bifrost's* upstream Jira credentials that failed,
    # not the API caller's identity.
    http_status = 502


class JiraNotFoundError(JiraAdapterError):
    """Requested Jira resource was not found (404)."""

    code = "adapter.jira.not_found"
    retryable = False
    http_status = 404


class JiraRateLimitError(JiraAdapterError):
    """Jira API rate limit hit (429)."""

    code = "adapter.jira.rate_limit"
    retryable = True
    http_status = 429

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(
            f"Jira rate limit exceeded (retry after {retry_after}s)"
            if retry_after
            else "Jira rate limit exceeded",
            retry_after=retry_after,
        )


# ── Helpers ─────────────────────────────────────────────────────────


def _build_auth_header(email: str, token: str) -> str:
    """Build Basic auth header value: base64(email:token)."""
    raw = f"{email}:{token}"
    encoded = base64.b64encode(raw.encode()).decode()
    return f"Basic {encoded}"


def _source_object_type_for_issue(issue_type_name: str) -> SourceObjectType:
    """Map a Jira issue type name to a SourceObjectType."""
    lower = issue_type_name.lower()
    if lower == "epic":
        return SourceObjectType.EPIC
    return SourceObjectType.ISSUE


def _parse_jira_datetime(value: str | None) -> datetime | None:
    """Parse a Jira datetime string (ISO-8601 with offset) to UTC datetime."""
    if not value:
        return None
    # Jira returns e.g. "2024-03-15T10:30:00.000+0000"
    return datetime.fromisoformat(value)


# ── JiraAdapter ─────────────────────────────────────────────────────


class JiraAdapter(SourceAdapter):
    """SourceAdapter backed by Jira Cloud REST API v3.

    Authenticates via API token (Basic auth). Discovers issues using JQL
    and maps them onto the SourceAdapter contract as ``SourceObjectType.ISSUE``
    or ``SourceObjectType.EPIC``.

    Args:
        base_url: Jira Cloud instance URL (e.g. ``https://example.atlassian.net``).
        email: Atlassian account email for API authentication.
        token: Atlassian API token.
        instance: Optional instance identifier for URN generation. Defaults
            to the hostname portion of *base_url*.
        client: Optional pre-configured httpx.AsyncClient (for testing).
    """

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        token: str,
        instance: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Strip trailing slash for consistent URL joining
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._token = token
        self._instance = instance or self._base_url.split("//", 1)[-1].split("/")[0]
        self._auth_header = _build_auth_header(email, token)
        self._external_client = client

    # ── SourceAdapter ABC ───────────────────────────────────────────

    @property
    def adapter_id(self) -> str:
        return "jira"

    async def discover(self, scope: AdapterScope) -> AsyncIterator[SourceObject]:
        """Yield SourceObjects for every Jira issue within the scope.

        ``scope.source_scope["project_key"]`` narrows to a Jira project.
        Optional ``scope.filter.modified_after`` adds a date filter.
        """
        jql = self._build_jql(scope)
        start_at = 0

        while True:
            data = await self._search_issues(jql, start_at=start_at, max_results=_MAX_JQL_PAGE_SIZE)
            issues: list[dict] = data.get("issues", [])

            for issue in issues:
                yield self._issue_to_source_object(issue)

            total = data.get("total", 0)
            start_at += len(issues)
            if start_at >= total or not issues:
                break

    async def identify(self, scope: AdapterScope) -> list[SourceObjectRef]:
        refs: list[SourceObjectRef] = []
        async for obj in self.discover(scope):
            refs.append(obj.ref)
        return refs

    async def extract(self, ref: SourceObjectRef) -> ExtractedContent:
        """Extract full content for a single Jira issue."""
        issue_key = ref.object_id
        issue = await self._get_issue(issue_key)

        fields = issue.get("fields", {})
        issue_type_name = (fields.get("issuetype") or {}).get("name", "Task")
        created = _parse_jira_datetime(fields.get("created"))
        updated = _parse_jira_datetime(fields.get("updated"))

        # Build structured content from issue fields
        content_data = self._extract_issue_content(issue)
        content_text = self._render_issue_text(content_data)

        return ExtractedContent(
            ref=ref,
            revision=RevisionInfo(
                ref=ref,
                revision_token=fields.get("updated", "unknown"),
                revision_timestamp=updated or datetime.now(UTC),
            ),
            content=ContentBody(
                kind="composite",
                parts=[
                    ContentBody(kind="text", text=content_text),
                    ContentBody(kind="structured", data=content_data),
                ],
            ),
            metadata=SourceMetadata(
                created_at=created,
                modified_at=updated,
                author=(fields.get("creator") or {}).get("displayName"),
                content_type="application/json",
                extra={
                    "issue_type": issue_type_name,
                    "status": (fields.get("status") or {}).get("name"),
                    "priority": (fields.get("priority") or {}).get("name"),
                },
            ),
            relevance_hints=self._build_relevance_hints(issue),
            extracted_at=datetime.now(UTC),
            original_token_estimate=len(content_text) // 4 if content_text else 0,
        )

    async def track_revision(self, ref: SourceObjectRef) -> RevisionInfo:
        issue_key = ref.object_id
        issue = await self._get_issue(issue_key, fields=["updated"])
        updated_str = (issue.get("fields") or {}).get("updated")
        updated = _parse_jira_datetime(updated_str) or datetime.now(UTC)

        return RevisionInfo(
            ref=ref,
            revision_token=updated_str or "unknown",
            revision_timestamp=updated,
        )

    async def check_permission(self, user: UserContext, ref: SourceObjectRef) -> PermissionResult:
        """Check permission by attempting to read the issue.

        Jira's permission model is enforced server-side, so a 403/404
        response indicates no access.
        """
        try:
            await self._get_issue(ref.object_id, fields=["summary"])
            return PermissionResult(can_read=True, determined=True)
        except JiraNotFoundError:
            return PermissionResult(
                can_read=False,
                determined=True,
                reason="Issue not found or no access",
            )
        except JiraAuthenticationError:
            return PermissionResult(
                can_read=False,
                determined=True,
                reason="Authentication failed",
            )

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_watch=False,
            supports_incremental_identify=True,
            supports_change_summary=False,
            supports_permission_check=True,
            supported_object_types=[
                SourceObjectType.ISSUE,
                SourceObjectType.EPIC,
                SourceObjectType.ISSUE_COMMENT,
            ],
            source_category=SourceCategory.WORK_INTENT,
            max_concurrent_extractions=5,
            supports_partial_extraction=False,
        )

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            data = await self._request("GET", "/rest/api/3/myself")
            elapsed = (time.monotonic() - start) * 1000
            display_name = data.get("displayName", "unknown")
            return HealthStatus(
                status="healthy",
                latency_ms=elapsed,
                message=f"Authenticated as {display_name}",
                checked_at=datetime.now(UTC),
            )
        except JiraAuthenticationError:
            elapsed = (time.monotonic() - start) * 1000
            return HealthStatus(
                status="unhealthy",
                latency_ms=elapsed,
                message="Authentication failed",
                checked_at=datetime.now(UTC),
            )
        except JiraAdapterError as exc:
            elapsed = (time.monotonic() - start) * 1000
            return HealthStatus(
                status="degraded",
                latency_ms=elapsed,
                message=str(exc),
                checked_at=datetime.now(UTC),
            )

    # ── Jira-specific operations ────────────────────────────────────

    async def get_project_metadata(self, project_key: str) -> dict:
        """Fetch Jira project info: name, lead, issue types, etc."""
        return await self._request("GET", f"/rest/api/3/project/{project_key}")

    async def get_changes_since(self, project_key: str, since: datetime) -> list[SourceObject]:
        """Return issues updated since *since* in the given project."""
        formatted = since.strftime("%Y-%m-%d %H:%M")
        jql = f'project = "{project_key}" AND updated >= "{formatted}" ORDER BY updated ASC'

        results: list[SourceObject] = []
        start_at = 0
        while True:
            data = await self._search_issues(jql, start_at=start_at, max_results=_MAX_JQL_PAGE_SIZE)
            issues = data.get("issues", [])
            for issue in issues:
                results.append(self._issue_to_source_object(issue))
            total = data.get("total", 0)
            start_at += len(issues)
            if start_at >= total or not issues:
                break
        return results

    # ── HTTP layer ──────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Make an authenticated request to the Jira REST API with retry.

        A single attempt is a closure over ``_attempt``; the shared
        :func:`~bifrost.errors.with_retry` primitive owns the retry policy.
        Only :class:`JiraRateLimitError` (429) is ``retryable`` and so gets
        retried — honoring a ``Retry-After`` header when present, exponential
        backoff otherwise. 401/404/other-5xx raise straight through.
        """
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        async def _attempt() -> dict:
            if self._external_client:
                response = await self._external_client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            else:
                async with httpx.AsyncClient(timeout=http_timeout()) as client:
                    response = await client.request(
                        method, url, headers=headers, params=params, json=json_body
                    )

            if response.status_code == 401:
                raise JiraAuthenticationError(f"Authentication failed (401) for {method} {path}")
            if response.status_code == 404:
                raise JiraNotFoundError(f"Resource not found (404): {method} {path}")
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                raise JiraRateLimitError(retry_after=float(retry_after) if retry_after else None)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise JiraAdapterError(
                    f"Jira API error ({exc.response.status_code}): {method} {path}"
                ) from exc
            return response.json()

        return await with_retry(_attempt, max_attempts=_MAX_RETRIES, base_delay=_BACKOFF_BASE)

    async def _search_issues(
        self, jql: str, *, start_at: int = 0, max_results: int = _MAX_JQL_PAGE_SIZE
    ) -> dict:
        """Execute a JQL search query."""
        return await self._request(
            "GET",
            "/rest/api/3/search",
            params={
                "jql": jql,
                "startAt": str(start_at),
                "maxResults": str(max_results),
                "fields": ",".join(
                    [
                        "summary",
                        "issuetype",
                        "status",
                        "priority",
                        "created",
                        "updated",
                        "creator",
                        "assignee",
                        "labels",
                        "issuelinks",
                        "comment",
                        "description",
                        "parent",
                    ]
                ),
            },
        )

    async def _get_issue(self, issue_key: str, *, fields: list[str] | None = None) -> dict:
        """Fetch a single issue by key."""
        params: dict[str, str] = {}
        if fields:
            params["fields"] = ",".join(fields)
        return await self._request("GET", f"/rest/api/3/issue/{issue_key}", params=params or None)

    # ── Mapping helpers ─────────────────────────────────────────────

    def _build_jql(self, scope: AdapterScope) -> str:
        """Build a JQL query string from an AdapterScope."""
        clauses: list[str] = []

        project_key = scope.source_scope.get("project_key")
        if project_key:
            clauses.append(f'project = "{project_key}"')

        if scope.filter:
            if scope.filter.modified_after:
                formatted = scope.filter.modified_after.strftime("%Y-%m-%d %H:%M")
                clauses.append(f'updated >= "{formatted}"')
            if scope.filter.modified_before:
                formatted = scope.filter.modified_before.strftime("%Y-%m-%d %H:%M")
                clauses.append(f'updated <= "{formatted}"')
            if scope.filter.object_types:
                type_names = []
                for ot in scope.filter.object_types:
                    if ot == SourceObjectType.EPIC:
                        type_names.append("Epic")
                    elif ot in (SourceObjectType.ISSUE, SourceObjectType.ISSUE_COMMENT):
                        pass  # Issues are always returned; comments come with issues
                if type_names:
                    type_list = ", ".join(f'"{t}"' for t in type_names)
                    clauses.append(f"issuetype in ({type_list})")

        jql = " AND ".join(clauses) if clauses else "ORDER BY updated DESC"
        if clauses:
            jql += " ORDER BY updated ASC"
        return jql

    def _make_ref(self, issue_key: str, object_type: SourceObjectType) -> SourceObjectRef:
        return SourceObjectRef(
            urn=f"bifrost:jira:{self._instance}:{object_type.value}:{issue_key}",
            adapter_id="jira",
            source_instance=self._instance,
            object_type=object_type,
            object_id=issue_key,
        )

    def _issue_to_source_object(self, issue: dict) -> SourceObject:
        """Convert a Jira search result issue to a SourceObject."""
        fields = issue.get("fields", {})
        issue_key = issue["key"]
        issue_type_name = (fields.get("issuetype") or {}).get("name", "Task")
        obj_type = _source_object_type_for_issue(issue_type_name)

        created = _parse_jira_datetime(fields.get("created"))
        updated = _parse_jira_datetime(fields.get("updated"))

        return SourceObject(
            ref=self._make_ref(issue_key, obj_type),
            name=f"[{issue_key}] {fields.get('summary', '')}",
            category=SourceCategory.WORK_INTENT,
            metadata=SourceMetadata(
                created_at=created,
                modified_at=updated,
                author=(fields.get("creator") or {}).get("displayName"),
                content_type="application/json",
                extra={
                    "issue_type": issue_type_name,
                    "status": (fields.get("status") or {}).get("name"),
                    "priority": (fields.get("priority") or {}).get("name"),
                    "labels": fields.get("labels", []),
                },
            ),
        )

    def _extract_issue_content(self, issue: dict) -> dict:
        """Extract structured content fields from a full issue response."""
        fields = issue.get("fields", {})
        issue_key = issue.get("key", "")

        # Description: Jira v3 uses ADF — extract text nodes
        description_adf = fields.get("description")
        description_text = _adf_to_text(description_adf) if description_adf else ""

        # Comments
        comment_field = fields.get("comment") or {}
        comments_raw = comment_field.get("comments", [])
        comments = [
            {
                "author": (c.get("author") or {}).get("displayName", "unknown"),
                "created": c.get("created"),
                "body": _adf_to_text(c.get("body")) if c.get("body") else "",
            }
            for c in comments_raw
        ]

        # Linked issues
        links_raw = fields.get("issuelinks") or []
        links = []
        for link in links_raw:
            link_type = (link.get("type") or {}).get("name", "relates to")
            if "outwardIssue" in link:
                linked_key = link["outwardIssue"]["key"]
                direction = "outward"
                label = (link.get("type") or {}).get("outward", link_type)
            elif "inwardIssue" in link:
                linked_key = link["inwardIssue"]["key"]
                direction = "inward"
                label = (link.get("type") or {}).get("inward", link_type)
            else:
                continue
            links.append(
                {
                    "key": linked_key,
                    "direction": direction,
                    "type": link_type,
                    "label": label,
                }
            )

        # Parent (for subtasks / child issues)
        parent = fields.get("parent")
        parent_key = parent["key"] if parent else None

        return {
            "key": issue_key,
            "summary": fields.get("summary", ""),
            "description": description_text,
            "issue_type": (fields.get("issuetype") or {}).get("name", "Task"),
            "status": (fields.get("status") or {}).get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "labels": fields.get("labels", []),
            "comments": comments,
            "linked_issues": links,
            "parent_key": parent_key,
        }

    @staticmethod
    def _render_issue_text(content_data: dict) -> str:
        """Render structured issue content as plain text for LLM consumption."""
        lines: list[str] = []
        lines.append(f"# [{content_data['key']}] {content_data['summary']}")
        lines.append("")
        lines.append(f"Type: {content_data['issue_type']}")
        lines.append(f"Status: {content_data.get('status', 'Unknown')}")
        if content_data.get("priority"):
            lines.append(f"Priority: {content_data['priority']}")
        if content_data.get("assignee"):
            lines.append(f"Assignee: {content_data['assignee']}")
        if content_data.get("labels"):
            lines.append(f"Labels: {', '.join(content_data['labels'])}")
        if content_data.get("parent_key"):
            lines.append(f"Parent: {content_data['parent_key']}")
        lines.append("")

        if content_data.get("description"):
            lines.append("## Description")
            lines.append(content_data["description"])
            lines.append("")

        if content_data.get("comments"):
            lines.append("## Comments")
            for comment in content_data["comments"]:
                lines.append(f"### {comment['author']} ({comment.get('created', '')})")
                lines.append(comment.get("body", ""))
                lines.append("")

        if content_data.get("linked_issues"):
            lines.append("## Linked Issues")
            for link in content_data["linked_issues"]:
                lines.append(f"- {link['label']} {link['key']}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _build_relevance_hints(issue: dict) -> list[RelevanceHint]:
        """Build relevance hints from issue labels and links."""
        hints: list[RelevanceHint] = []
        fields = issue.get("fields", {})
        labels = fields.get("labels") or []

        for label in labels:
            hints.append(
                RelevanceHint(
                    target_kind="concept",
                    target_id=label,
                    confidence="low",
                    basis="label-match",
                )
            )
        return hints


# ── ADF (Atlassian Document Format) text extraction ─────────────────


def _adf_to_text(node: dict | list | None) -> str:
    """Recursively extract plain text from an ADF document tree.

    ADF is a JSON-based rich text format used by Jira Cloud v3 APIs.
    We walk the tree extracting ``text`` nodes, joining with newlines
    for block-level elements.
    """
    if node is None:
        return ""
    if isinstance(node, list):
        return "\n".join(_adf_to_text(child) for child in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")

    # Text leaf node
    if node_type == "text":
        return node.get("text", "")

    # Mention
    if node_type == "mention":
        attrs = node.get("attrs") or {}
        return f"@{attrs.get('text', 'unknown')}"

    # Emoji
    if node_type == "emoji":
        attrs = node.get("attrs") or {}
        return attrs.get("shortName", "")

    # Inline card (links)
    if node_type == "inlineCard":
        attrs = node.get("attrs") or {}
        return attrs.get("url", "")

    # Recurse into children
    children = node.get("content", [])
    parts = [_adf_to_text(child) for child in children]

    # Block-level types get newline separation
    if node_type in ("paragraph", "heading", "blockquote", "codeBlock", "rule"):
        return "\n".join(parts)

    # List items
    if node_type in ("bulletList", "orderedList"):
        return "\n".join(f"- {p}" if p else "" for p in parts)

    if node_type == "listItem":
        return " ".join(parts)

    # Table
    if node_type == "table":
        return "\n".join(parts)
    if node_type == "tableRow":
        return " | ".join(parts)
    if node_type in ("tableHeader", "tableCell"):
        return " ".join(parts)

    return " ".join(parts)
