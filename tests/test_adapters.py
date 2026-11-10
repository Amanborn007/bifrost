"""Tests for MockAdapter — verifies it satisfies the SourceAdapter ABC contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bifrost.adapters.base import (
    AdapterScope,
    ScopeFilter,
    SourceAdapter,
    SourceObjectType,
    UserContext,
)
from bifrost.adapters.mock import MockAdapter

SAMPLE_FILES = {
    "src/main.py": "def main(): pass",
    "src/utils.py": "def helper(): return 42",
    "README.md": "# My Project",
}


@pytest.fixture
def adapter() -> MockAdapter:
    return MockAdapter(files=SAMPLE_FILES, instance="test-repo")


class TestMockAdapterIsSourceAdapter:
    def test_isinstance(self, adapter: MockAdapter) -> None:
        assert isinstance(adapter, SourceAdapter)

    def test_adapter_id(self, adapter: MockAdapter) -> None:
        assert adapter.adapter_id == "mock"


class TestDiscover:
    async def test_discovers_all_files(self, adapter: MockAdapter) -> None:
        scope = AdapterScope(source_scope={"repo": "test"})
        objects = [obj async for obj in adapter.discover(scope)]
        assert len(objects) == 3
        names = {obj.ref.object_id for obj in objects}
        assert names == set(SAMPLE_FILES.keys())

    async def test_discover_with_path_filter(self, adapter: MockAdapter) -> None:
        scope = AdapterScope(
            source_scope={"repo": "test"},
            filter=ScopeFilter(path_pattern="src/*.py"),
        )
        objects = [obj async for obj in adapter.discover(scope)]
        assert len(objects) == 2
        paths = {obj.ref.object_id for obj in objects}
        assert paths == {"src/main.py", "src/utils.py"}

    async def test_discover_empty_tree(self) -> None:
        adapter = MockAdapter(files={})
        scope = AdapterScope(source_scope={})
        objects = [obj async for obj in adapter.discover(scope)]
        assert objects == []


class TestIdentify:
    async def test_identify_returns_refs(self, adapter: MockAdapter) -> None:
        scope = AdapterScope(source_scope={})
        refs = await adapter.identify(scope)
        assert len(refs) == 3
        assert all(r.adapter_id == "mock" for r in refs)
        assert all(r.object_type == SourceObjectType.FILE for r in refs)


class TestExtract:
    async def test_extract_returns_content(self, adapter: MockAdapter) -> None:
        scope = AdapterScope(source_scope={})
        refs = await adapter.identify(scope)
        main_ref = next(r for r in refs if r.object_id == "src/main.py")

        content = await adapter.extract(main_ref)
        assert content.content.kind == "text"
        assert content.content.text == "def main(): pass"
        assert content.ref == main_ref
        assert content.revision.revision_token.startswith("mock-rev-")

    async def test_extract_missing_file_raises(self, adapter: MockAdapter) -> None:
        from bifrost.adapters.base import SourceObjectRef

        bad_ref = SourceObjectRef(
            urn="bifrost:mock:test:file:nonexistent.py",
            adapter_id="mock",
            source_instance="test",
            object_type=SourceObjectType.FILE,
            object_id="nonexistent.py",
        )
        with pytest.raises(FileNotFoundError):
            await adapter.extract(bad_ref)


class TestTrackRevision:
    async def test_track_revision(self, adapter: MockAdapter) -> None:
        scope = AdapterScope(source_scope={})
        refs = await adapter.identify(scope)
        rev = await adapter.track_revision(refs[0])
        assert rev.revision_token
        assert rev.revision_timestamp is not None


class TestCheckPermission:
    async def test_always_allows(self, adapter: MockAdapter) -> None:
        scope = AdapterScope(source_scope={})
        refs = await adapter.identify(scope)
        user = UserContext(user_id="test-user")
        result = await adapter.check_permission(user, refs[0])
        assert result.can_read is True
        assert result.determined is True


class TestCapabilities:
    def test_capabilities(self, adapter: MockAdapter) -> None:
        caps = adapter.get_capabilities()
        assert caps.supports_watch is False
        assert SourceObjectType.FILE in caps.supported_object_types


class TestHealthCheck:
    async def test_healthy(self, adapter: MockAdapter) -> None:
        health = await adapter.health_check()
        assert health.status == "healthy"


class TestChangeDetection:
    def test_get_changes_since(self) -> None:
        now = datetime.now(UTC)
        earlier = now - timedelta(hours=1)
        later = now + timedelta(hours=1)

        adapter = MockAdapter(
            files={"old.py": "old", "new.py": "new"},
            changes={"old.py": earlier, "new.py": later},
        )
        changed = adapter.get_changes_since(now)
        assert changed == ["new.py"]

    def test_no_changes(self) -> None:
        now = datetime.now(UTC)
        adapter = MockAdapter(files={"a.py": "a"})
        # All files were created at adapter init time, which is <= now
        changed = adapter.get_changes_since(now + timedelta(seconds=1))
        assert changed == []


class TestTestHelpers:
    def test_add_file(self) -> None:
        adapter = MockAdapter(files={})
        adapter.add_file("new.py", "content")
        assert "new.py" in adapter._files

    def test_remove_file(self) -> None:
        adapter = MockAdapter(files={"a.py": "x"})
        adapter.remove_file("a.py")
        assert "a.py" not in adapter._files

    def test_remove_nonexistent_is_noop(self) -> None:
        adapter = MockAdapter(files={})
        adapter.remove_file("nope.py")  # should not raise
