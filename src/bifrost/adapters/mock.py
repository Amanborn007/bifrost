"""MockAdapter — in-memory SourceAdapter for testing.

Provides a configurable file tree with change detection, enabling
extraction and orchestration tests without real Git repos or network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from bifrost.adapters.base import (
    AdapterCapabilities,
    AdapterScope,
    ContentBody,
    ExtractedContent,
    HealthStatus,
    PermissionResult,
    RevisionInfo,
    SourceAdapter,
    SourceCategory,
    SourceMetadata,
    SourceObject,
    SourceObjectRef,
    SourceObjectType,
    UserContext,
)


class MockAdapter(SourceAdapter):
    """In-memory adapter for testing. No filesystem or network access.

    Initialize with a dict mapping file paths to content strings:

        adapter = MockAdapter(files={"src/main.py": "print('hello')"})
    """

    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        instance: str = "mock-repo",
        changes: dict[str, datetime] | None = None,
    ) -> None:
        self._files = dict(files or {})
        self._instance = instance
        self._created_at = datetime.now(UTC)
        # Track per-file modification times for change detection
        self._modified: dict[str, datetime] = {}
        for path in self._files:
            self._modified[path] = self._created_at
        # Apply explicit change timestamps
        if changes:
            for path, ts in changes.items():
                if path in self._files:
                    self._modified[path] = ts

    @property
    def adapter_id(self) -> str:
        return "mock"

    def _make_ref(self, path: str) -> SourceObjectRef:
        return SourceObjectRef(
            urn=f"bifrost:mock:{self._instance}:file:{path}",
            adapter_id="mock",
            source_instance=self._instance,
            object_type=SourceObjectType.FILE,
            object_id=path,
        )

    def _make_metadata(self, path: str) -> SourceMetadata:
        content = self._files.get(path, "")
        return SourceMetadata(
            created_at=self._created_at,
            modified_at=self._modified.get(path, self._created_at),
            author="test-author",
            size_bytes=len(content.encode()),
            content_type="text/plain",
        )

    async def discover(self, scope: AdapterScope) -> AsyncIterator[SourceObject]:
        for path in sorted(self._files):
            if scope.filter and scope.filter.path_pattern:
                import fnmatch

                if not fnmatch.fnmatch(path, scope.filter.path_pattern):
                    continue
            yield SourceObject(
                ref=self._make_ref(path),
                name=path.split("/")[-1],
                category=SourceCategory.CODE_TRUTH,
                metadata=self._make_metadata(path),
            )

    async def identify(self, scope: AdapterScope) -> list[SourceObjectRef]:
        refs: list[SourceObjectRef] = []
        async for obj in self.discover(scope):
            refs.append(obj.ref)
        return refs

    async def extract(self, ref: SourceObjectRef) -> ExtractedContent:
        path = ref.object_id
        if path not in self._files:
            msg = f"File not found in mock adapter: {path}"
            raise FileNotFoundError(msg)
        content = self._files[path]
        modified = self._modified.get(path, self._created_at)
        return ExtractedContent(
            ref=ref,
            revision=RevisionInfo(
                ref=ref,
                revision_token=f"mock-rev-{hash(content) & 0xFFFF:04x}",
                revision_timestamp=modified,
            ),
            content=ContentBody(kind="text", text=content),
            metadata=self._make_metadata(path),
            relevance_hints=[],
            extracted_at=datetime.now(UTC),
        )

    async def track_revision(self, ref: SourceObjectRef) -> RevisionInfo:
        path = ref.object_id
        if path not in self._files:
            msg = f"File not found in mock adapter: {path}"
            raise FileNotFoundError(msg)
        content = self._files[path]
        modified = self._modified.get(path, self._created_at)
        return RevisionInfo(
            ref=ref,
            revision_token=f"mock-rev-{hash(content) & 0xFFFF:04x}",
            revision_timestamp=modified,
        )

    async def check_permission(self, user: UserContext, ref: SourceObjectRef) -> PermissionResult:
        return PermissionResult(can_read=True, determined=True)

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_watch=False,
            supports_incremental_identify=True,
            supports_change_summary=False,
            supports_permission_check=True,
            supported_object_types=[SourceObjectType.FILE],
            source_category=SourceCategory.CODE_TRUTH,
            max_concurrent_extractions=10,
            supports_partial_extraction=False,
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            status="healthy",
            latency_ms=0.0,
            message="In-memory mock adapter",
            checked_at=datetime.now(UTC),
        )

    # ── Test helpers ─────────────────────────────────────────────────

    def add_file(self, path: str, content: str, modified_at: datetime | None = None) -> None:
        """Add or update a file in the mock file tree."""
        self._files[path] = content
        self._modified[path] = modified_at or datetime.now(UTC)

    def remove_file(self, path: str) -> None:
        """Remove a file from the mock file tree."""
        self._files.pop(path, None)
        self._modified.pop(path, None)

    def get_changes_since(self, since: datetime) -> list[str]:
        """Return paths of files modified after the given timestamp."""
        return [path for path, ts in self._modified.items() if ts > since]
