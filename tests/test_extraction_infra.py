"""Tests for extraction infrastructure: cost tracking, secret scanning, change detection."""

from __future__ import annotations

import pytest

from bifrost.extraction.changes import ChangeDetector, compute_hash, has_changed
from bifrost.extraction.cost import (
    BudgetExceededError,
    CostRecord,
    CostTracker,
    estimate_cost,
)
from bifrost.extraction.secrets import SecretScanner

# ═══════════════════════════════════════════════════════════════════════
# Cost tracking
# ═══════════════════════════════════════════════════════════════════════


class TestEstimateCost:
    def test_sonnet_pricing(self) -> None:
        cost = estimate_cost(
            "claude-sonnet-4-6-20250514",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert cost == pytest.approx(18.00)  # $3 input + $15 output

    def test_haiku_pricing(self) -> None:
        cost = estimate_cost(
            "claude-haiku-4-5-20251001",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # Haiku 4.5: $1/M input + $5/M output per Anthropic public pricing.
        assert cost == pytest.approx(6.00)

    def test_cache_read_tokens(self) -> None:
        cost = estimate_cost(
            "claude-sonnet-4-6-20250514",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
        )
        assert cost == pytest.approx(0.30)

    def test_unknown_model_uses_default(self) -> None:
        cost = estimate_cost(
            "unknown-model-v99",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert cost == pytest.approx(3.00)  # default = sonnet pricing


class TestCostTracker:
    def test_record_accumulates(self) -> None:
        tracker = CostTracker(task_id="test-1")
        tracker.record(
            model="claude-haiku-4-5-20251001", phase="A", input_tokens=1000, output_tokens=500
        )
        tracker.record(
            model="claude-haiku-4-5-20251001", phase="D", input_tokens=2000, output_tokens=1000
        )
        assert len(tracker.records) == 2
        assert tracker.total_input_tokens == 3000
        assert tracker.total_output_tokens == 1500
        assert tracker.total_cost > 0

    def test_record_returns_cost_record(self) -> None:
        tracker = CostTracker(task_id="test-1")
        rec = tracker.record(
            model="claude-haiku-4-5-20251001", phase="A", input_tokens=1000, output_tokens=500
        )
        assert isinstance(rec, CostRecord)
        assert rec.model == "claude-haiku-4-5-20251001"
        assert rec.phase == "A"

    def test_budget_enforcement_raises(self) -> None:
        tracker = CostTracker(task_id="test-budget", budget=0.001)
        with pytest.raises(BudgetExceededError) as exc_info:
            tracker.record(
                model="claude-sonnet-4-6-20250514",
                phase="D",
                input_tokens=10_000,
                output_tokens=5_000,
            )
        assert exc_info.value.budget == 0.001
        assert exc_info.value.spent > 0.001

    def test_no_budget_allows_unlimited(self) -> None:
        tracker = CostTracker(task_id="test-unlimited")
        for _ in range(100):
            tracker.record(
                model="claude-sonnet-4-6-20250514",
                phase="D",
                input_tokens=10_000,
                output_tokens=5_000,
            )
        assert tracker.total_cost > 0

    @pytest.mark.asyncio
    async def test_persist_writes_rows(self, db_session) -> None:
        tracker = CostTracker(task_id="persist-test")
        tracker.record(
            model="claude-haiku-4-5-20251001", phase="A", input_tokens=500, output_tokens=200
        )
        tracker.record(
            model="claude-sonnet-4-6-20250514", phase="D", input_tokens=5000, output_tokens=1000
        )

        count = await tracker.persist(db_session)
        assert count == 2


# ═══════════════════════════════════════════════════════════════════════
# Secret scanning
# ═══════════════════════════════════════════════════════════════════════


class TestSecretScanner:
    def test_aws_access_key_redacted(self) -> None:
        scanner = SecretScanner()
        text = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
        result = scanner.scan_and_redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_content
        assert "[REDACTED:" in result.redacted_content
        assert result.has_redactions
        assert result.redactions[0].secret_type == "aws_access_key"

    def test_github_token_redacted(self) -> None:
        scanner = SecretScanner()
        text = "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        result = scanner.scan_and_redact(text)
        assert "ghp_" not in result.redacted_content
        assert "[REDACTED:github_token]" in result.redacted_content

    def test_private_key_redacted(self) -> None:
        scanner = SecretScanner()
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogIBAAJBALRiMLAHudeSA/x3hB2f+2NRkJrSa==\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = scanner.scan_and_redact(text)
        assert "MIIBogIBAAJ" not in result.redacted_content
        assert "[REDACTED:private_key]" in result.redacted_content

    def test_generic_api_key_redacted(self) -> None:
        scanner = SecretScanner()
        text = 'api_key = "sk-1234567890abcdefABCDEF1234567890"'
        result = scanner.scan_and_redact(text)
        assert "sk-1234567890" not in result.redacted_content
        assert "[REDACTED:generic_api_key]" in result.redacted_content

    def test_connection_string_redacted(self) -> None:
        scanner = SecretScanner()
        text = "DATABASE_URL=postgres://user:secretpass@db.example.com:5432/mydb"
        result = scanner.scan_and_redact(text)
        assert "secretpass" not in result.redacted_content
        assert "[REDACTED:connection_string]" in result.redacted_content

    def test_clean_content_unchanged(self) -> None:
        scanner = SecretScanner()
        text = "def hello():\n    return 'world'\n"
        result = scanner.scan_and_redact(text)
        assert result.redacted_content == text
        assert not result.has_redactions

    def test_line_structure_preserved(self) -> None:
        scanner = SecretScanner()
        text = "line1\nAKIAIOSFODNN7EXAMPLE\nline3\n"
        result = scanner.scan_and_redact(text)
        # Same number of lines
        assert result.redacted_content.count("\n") == text.count("\n")
        lines = result.redacted_content.splitlines()
        assert lines[0] == "line1"
        assert lines[2] == "line3"

    def test_multiple_secrets_on_one_line(self) -> None:
        scanner = SecretScanner()
        text = "key=AKIAIOSFODNN7EXAMPLE token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        result = scanner.scan_and_redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_content
        assert "ghp_" not in result.redacted_content
        assert len(result.redactions) == 2

    def test_allowlist_skips_match(self) -> None:
        import re

        scanner = SecretScanner(allowlist=[re.compile(r"AKIAIOSFODNN7EXAMPLE")])
        text = "key=AKIAIOSFODNN7EXAMPLE"
        result = scanner.scan_and_redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" in result.redacted_content
        assert not result.has_redactions

    def test_extra_patterns(self) -> None:
        import re

        custom = ("custom_secret", re.compile(r"(?P<secret>CUSTOM_[A-Z]{10})"))
        scanner = SecretScanner(extra_patterns=[custom])
        text = "token=CUSTOM_ABCDEFGHIJ"
        result = scanner.scan_and_redact(text)
        assert "CUSTOM_ABCDEFGHIJ" not in result.redacted_content
        assert "[REDACTED:custom_secret]" in result.redacted_content


# ═══════════════════════════════════════════════════════════════════════
# Change detection
# ═══════════════════════════════════════════════════════════════════════


class TestComputeHash:
    def test_same_content_same_hash(self) -> None:
        content = "def hello(): pass"
        assert compute_hash(content) == compute_hash(content)

    def test_different_content_different_hash(self) -> None:
        assert compute_hash("aaa") != compute_hash("bbb")

    def test_has_changed_false_for_same(self) -> None:
        content = "def hello(): pass"
        h = compute_hash(content)
        assert not has_changed(content, h)

    def test_has_changed_true_for_different(self) -> None:
        h = compute_hash("old content")
        assert has_changed("new content", h)


class TestChangeDetector:
    @pytest.mark.asyncio
    async def test_unchanged_file_skipped(self) -> None:
        content = "def hello(): pass"
        h = compute_hash(content)
        detector = ChangeDetector()
        report = await detector.detect(
            current_files={"a.py": content},
            previous_hashes={"a.py": h},
        )
        assert report.unchanged_count == 1
        assert len(report.changed) == 0
        assert report.files_to_extract == []

    @pytest.mark.asyncio
    async def test_changed_file_included(self) -> None:
        detector = ChangeDetector()
        report = await detector.detect(
            current_files={"a.py": "new code"},
            previous_hashes={"a.py": compute_hash("old code")},
        )
        assert len(report.changed) == 1
        assert report.changed[0].status == "changed"
        assert report.changed[0].path == "a.py"
        assert "a.py" in report.files_to_extract

    @pytest.mark.asyncio
    async def test_new_file_detected(self) -> None:
        detector = ChangeDetector()
        report = await detector.detect(
            current_files={"new.py": "code"},
            previous_hashes={},
        )
        assert len(report.changed) == 1
        assert report.changed[0].status == "new"

    @pytest.mark.asyncio
    async def test_deleted_file_detected(self) -> None:
        detector = ChangeDetector()
        report = await detector.detect(
            current_files={},
            previous_hashes={"old.py": compute_hash("code")},
        )
        assert len(report.changed) == 1
        assert report.changed[0].status == "deleted"

    @pytest.mark.asyncio
    async def test_renamed_file_detected(self) -> None:
        content = "def hello(): pass"
        h = compute_hash(content)
        detector = ChangeDetector()
        report = await detector.detect(
            current_files={"new_name.py": content},
            previous_hashes={"old_name.py": h},
        )
        assert len(report.changed) == 1
        assert report.changed[0].status == "renamed"
        assert report.changed[0].previous_path == "old_name.py"
        assert report.changed[0].path == "new_name.py"

    @pytest.mark.asyncio
    async def test_mixed_changes(self) -> None:
        unchanged_content = "unchanged"
        changed_content_old = "old version"
        changed_content_new = "new version"
        new_content = "brand new"

        detector = ChangeDetector()
        report = await detector.detect(
            current_files={
                "stable.py": unchanged_content,
                "modified.py": changed_content_new,
                "added.py": new_content,
            },
            previous_hashes={
                "stable.py": compute_hash(unchanged_content),
                "modified.py": compute_hash(changed_content_old),
                "removed.py": compute_hash("dead code"),
            },
        )
        assert report.unchanged_count == 1
        statuses = {f.status for f in report.changed}
        assert "changed" in statuses
        assert "new" in statuses
        assert "deleted" in statuses
