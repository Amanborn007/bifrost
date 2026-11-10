"""Extraction pipeline — AI-powered code analysis and entity extraction.

Architectural invariant (operator-mandated):
  This pipeline MUST NOT use browser automation (puppeteer, playwright,
  selenium, headless chrome, webdriver, etc.). All extraction works on
  source code that ``bifrost.extraction.clone`` has fetched via ``git
  clone`` to local disk; the per-phase ``claude -p`` subprocess reads
  files from that worktree.

  External data (Jira tickets, Confluence pages, GitHub PRs/issues,
  Slack threads) flows through MCP servers configured in the operator's
  Claude CLI session (the Atlassian, GitHub and Slack MCP servers named
  in ``mcp.tool_prefix``). Those are server-to-server API calls,
  not browser navigation.

  CI enforces this via the ``no-browser-automation`` job in
  ``.github/workflows/pr-check.yml`` and the rule lives at CLAUDE.md
  anti-pattern #10. Disabling either gate counts as violating the
  invariant.
"""

from bifrost.extraction.changes import ChangeDetector, ChangeReport, compute_hash
from bifrost.extraction.cost import BudgetExceededError, CostTracker, estimate_cost
from bifrost.extraction.pipeline import (
    ContentTooLargeError,
    ExtractedComponent,
    ExtractedDependency,
    ExtractedFlow,
    ExtractedFlowSegment,
    ExtractionOutput,
    ExtractionPipeline,
)
from bifrost.extraction.secrets import SecretScanner
from bifrost.extraction.triage import TriagedFile, TriageResult, triage_files

__all__ = [
    "BudgetExceededError",
    "ChangeDetector",
    "ChangeReport",
    "ContentTooLargeError",
    "CostTracker",
    "ExtractedComponent",
    "ExtractedDependency",
    "ExtractedFlow",
    "ExtractedFlowSegment",
    "ExtractionOutput",
    "ExtractionPipeline",
    "SecretScanner",
    "TriagedFile",
    "TriageResult",
    "compute_hash",
    "estimate_cost",
    "triage_files",
]
