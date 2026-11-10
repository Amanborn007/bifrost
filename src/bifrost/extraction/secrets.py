"""Secret scanning and redaction for file content before LLM calls.

Detects common secret patterns (AWS keys, GitHub tokens, private keys,
connection strings, JWTs) and replaces them inline with
``[REDACTED:secret_type]`` placeholders. Line structure is preserved —
no lines are removed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Built-in secret patterns ─────────────────────────────────────────

_BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # AWS access key IDs — always start with AKIA
    (
        "aws_access_key",
        re.compile(
            r"(?:^|[^A-Za-z0-9])(?P<secret>AKIA[0-9A-Z]{16})(?:[^A-Za-z0-9]|$)",
        ),
    ),
    # AWS secret access keys — 40-char base64 after key identifier
    (
        "aws_secret_key",
        re.compile(
            r"(?:aws_secret_access_key|secret_access_key|SecretAccessKey)"
            r"\s*[=:]\s*['\"]?(?P<secret>[A-Za-z0-9/+=]{40})['\"]?"
        ),
    ),
    # GitHub personal access tokens (classic and fine-grained)
    ("github_token", re.compile(r"(?P<secret>ghp_[A-Za-z0-9]{36,})")),
    ("github_oauth", re.compile(r"(?P<secret>gho_[A-Za-z0-9]{36,})")),
    ("github_app_token", re.compile(r"(?P<secret>ghu_[A-Za-z0-9]{36,})")),
    ("github_refresh", re.compile(r"(?P<secret>ghr_[A-Za-z0-9]{36,})")),
    # Generic API key/token patterns — catches `api_key = "..."` style
    (
        "generic_api_key",
        re.compile(
            r"(?:api[_-]?key|api[_-]?token|api[_-]?secret|access[_-]?token|auth[_-]?token)"
            r"\s*[=:]\s*['\"](?P<secret>[A-Za-z0-9_\-./+=]{20,})['\"]",
            re.IGNORECASE,
        ),
    ),
    # RSA / EC / DSA private keys (PEM format)
    (
        "private_key",
        re.compile(
            r"(?P<secret>-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |EC |DSA )?PRIVATE KEY-----)"
        ),
    ),
    # JSON Web Tokens (three base64url sections separated by dots)
    (
        "jwt",
        re.compile(r"(?P<secret>eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
    ),
    # Database connection strings with embedded credentials
    (
        "connection_string",
        re.compile(
            r"(?P<secret>(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)"
            r"://[^\s'\"]+:[^\s'\"]+@[^\s'\"]+)"
        ),
    ),
    # Password assignments — `password = "..."` style
    (
        "password",
        re.compile(
            r"(?:password|passwd|pwd)\s*[=:]\s*['\"](?P<secret>[^'\"]{8,})['\"]",
            re.IGNORECASE,
        ),
    ),
]


@dataclass
class RedactionEvent:
    """One redaction that occurred during scanning."""

    file_path: str
    secret_type: str
    line_number: int
    # Never store the actual secret value


@dataclass
class ScanResult:
    """Result of scanning a piece of content."""

    redacted_content: str
    redactions: list[RedactionEvent] = field(default_factory=list)

    @property
    def has_redactions(self) -> bool:
        return len(self.redactions) > 0


class SecretScanner:
    """Scans text content and redacts detected secrets inline.

    Usage::

        scanner = SecretScanner()
        result = scanner.scan_and_redact(content, file_path="config.py")
        safe_content = result.redacted_content
    """

    def __init__(
        self,
        *,
        extra_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
        allowlist: list[re.Pattern[str]] | None = None,
    ) -> None:
        self._patterns = list(_BUILTIN_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)
        self._allowlist = allowlist or []

    def scan_and_redact(
        self,
        content: str,
        *,
        file_path: str = "<unknown>",
    ) -> ScanResult:
        """Scan content and return redacted version with event log.

        Lines are preserved — secrets are replaced inline, not deleted.
        Multi-line patterns (e.g. PEM private keys) are handled by running
        those patterns on the full content first, then single-line patterns
        are run per-line.
        """
        redactions: list[RedactionEvent] = []
        redacted = content

        # Pass 1: multi-line patterns (private keys) on full content
        for secret_type, pattern in self._patterns:
            if secret_type == "private_key":
                for match in pattern.finditer(redacted):
                    secret_value = match.group("secret")
                    if self._is_allowlisted(secret_value):
                        continue
                    # Compute line number of match start
                    line_num = redacted[: match.start()].count("\n") + 1
                    placeholder = f"[REDACTED:{secret_type}]"
                    redacted = redacted.replace(secret_value, placeholder, 1)
                    redactions.append(
                        RedactionEvent(
                            file_path=file_path,
                            secret_type=secret_type,
                            line_number=line_num,
                        )
                    )

        # Pass 2: single-line patterns per line
        result_lines: list[str] = []
        for line_idx, line in enumerate(redacted.splitlines(keepends=True), start=1):
            redacted_line = line
            for secret_type, pattern in self._patterns:
                if secret_type == "private_key":
                    continue  # already handled
                for match in pattern.finditer(redacted_line):
                    secret_value = match.group("secret")
                    if self._is_allowlisted(secret_value):
                        continue
                    placeholder = f"[REDACTED:{secret_type}]"
                    redacted_line = redacted_line.replace(
                        secret_value,
                        placeholder,
                        1,
                    )
                    redactions.append(
                        RedactionEvent(
                            file_path=file_path,
                            secret_type=secret_type,
                            line_number=line_idx,
                        )
                    )
            result_lines.append(redacted_line)

        redacted_content = "".join(result_lines)

        if redactions:
            logger.warning(
                "Redacted %d secret(s) in %s: %s",
                len(redactions),
                file_path,
                ", ".join(f"L{r.line_number}:{r.secret_type}" for r in redactions),
            )

        return ScanResult(redacted_content=redacted_content, redactions=redactions)

    def _is_allowlisted(self, value: str) -> bool:
        return any(p.search(value) for p in self._allowlist)
