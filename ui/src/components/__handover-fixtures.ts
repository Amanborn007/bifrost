/**
 * Trimmed handover-body fixtures used by ``prose-handover-smoke.test.tsx``.
 *
 * Two handover bodies (demo-training-svc and order-fulfillment). Each
 * preserves the load-bearing markdown shapes we want to assert <Prose>
 * can render without throwing:
 *
 *   - duplicate adjacent ``## Architecture`` headers (header splice bug)
 *   - blockquote callouts with emoji
 *   - GFM tables
 *   - mermaid fenced block with em-dash labels, asterisks inside
 *     square-bracket node names, and a node referenced but not
 *     declared (``ShipmentIssueReporter``)
 */

export const BODY_1 = [
  "# demo-training-svc — handover",
  "",
  "## Executive summary",
  "Sandbox repo. Limited extraction signal.",
  "",
  "## Architecture",
  "## Architecture",
  "",
  "> ⚠️ **Insufficient data.** Single top-level entity only.",
  "",
  "| Element | Value |",
  "|---|---|",
  "| Top-level service | `demo-training-svc` |",
  "",
  "## Glossary",
  "## Glossary",
  "",
  "| Term | Definition |",
  "|---|---|",
  "| **KTLO** | \"Keep The Lights On\" — operational responsibility surface. |",
  "",
].join("\n");

export const BODY_2 = [
  "# order-fulfillment — handover",
  "",
  "## Architecture",
  "## Component Relationships",
  "",
  "```mermaid",
  "flowchart TD",
  "    subgraph WebService[\"Web Service (FastAPI)\"]",
  "        API[HTTP API]",
  "    end",
  "    subgraph DataLayer[\"Data Layer (PostgreSQL)\"]",
  "        BULK[BulkOrderBatch*]",
  "    end",
  "    subgraph External[\"External Services\"]",
  "        SFTP[Partner Gateway — SFTP]",
  "    end",
  "    API --> SFTP",
  "    BULK --> ShipmentIssueReporter",
  "```",
  "",
  "## Critical flows",
  "## Critical Flows",
  "",
  "1. `FulfillmentScheduler` triggers polling.",
  "",
].join("\n");
