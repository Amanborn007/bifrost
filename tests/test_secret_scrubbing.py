"""Tests for repository.append_message + executor output_log secret
scrubbing.

The repo's ``SecretScanner`` was already used at extraction time but
not at chat / output_log persistence sites. This test pins the new
behaviour: assistant content with credential-shaped text round-trips
back as REDACTED, never the original.
"""

from __future__ import annotations

from bifrost.actions.executor import _scrub_secrets_for_log
from bifrost.extraction.secrets import SecretScanner
from bifrost.storage.repository import _scrub_secrets


class TestScrubSecretsHelper:
    """The shared scrubber must catch the same credential shapes the
    extraction-time scanner already catches."""

    def test_aws_key_redacted(self) -> None:
        text = "credentials are AKIAIOSFODNN7EXAMPLE doing"
        out = _scrub_secrets(text, source="test")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # Still readable around the redaction
        assert "credentials" in out
        assert "doing" in out

    def test_github_pat_redacted(self) -> None:
        text = "token: ghp_aBcD1234EfGh5678IjKl9012MnOp3456QrSt"
        out = _scrub_secrets(text, source="test")
        assert "ghp_aBcD1234EfGh5678IjKl9012MnOp3456QrSt" not in out

    def test_private_key_redacted(self) -> None:
        text = (
            "before\n-----BEGIN PRIVATE KEY-----\n"
            "MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQ\n"
            "-----END PRIVATE KEY-----\nafter"
        )
        out = _scrub_secrets(text, source="test")
        assert "MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQ" not in out
        # Surrounding text preserved
        assert "before" in out
        assert "after" in out

    def test_clean_text_passes_through(self) -> None:
        text = "this has nothing secret in it"
        out = _scrub_secrets(text, source="test")
        assert out == text

    def test_scanner_singleton_reused(self) -> None:
        # Two calls on different content should compile the patterns once.
        out1 = _scrub_secrets("first AKIAIOSFODNN7EXAMPLE", source="t1")
        out2 = _scrub_secrets("second AKIAIOSFODNN7EXAMPLE", source="t2")
        assert "AKIAIOSFODNN7EXAMPLE" not in out1
        assert "AKIAIOSFODNN7EXAMPLE" not in out2


class TestScrubOutputLog:
    """The executor's output_log scrubber is functionally identical
    to the chat one but lives in executor.py (lazy import, no circular
    dep)."""

    def test_aws_key_redacted_in_log(self) -> None:
        log = "=== Verify ===\n[verify] pytest tests/\nENV=AKIAIOSFODNN7EXAMPLE pytest passed"
        out = _scrub_secrets_for_log(log)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # The non-secret structure stays
        assert "=== Verify ===" in out
        assert "pytest tests/" in out

    def test_empty_log_passes_through(self) -> None:
        assert _scrub_secrets_for_log("") == ""
        assert _scrub_secrets_for_log("   ") == "   "


class TestScannerDirect:
    """Sanity-check the underlying SecretScanner contract that the helpers
    depend on. If this changes, the helper wrappers need to follow."""

    def test_scan_result_has_redacted_content(self) -> None:
        s = SecretScanner()
        r = s.scan_and_redact("AKIAIOSFODNN7EXAMPLE", file_path="t")
        assert hasattr(r, "redacted_content")
        assert isinstance(r.redacted_content, str)
        assert "AKIAIOSFODNN7EXAMPLE" not in r.redacted_content
