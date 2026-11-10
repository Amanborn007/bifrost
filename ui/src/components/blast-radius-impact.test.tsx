/**
 * Blast-radius granular reasoning tests (S6 dependency-impact).
 *
 * The old UI rendered "N services @ 88%" with no reasoning. These tests
 * lock in the new contract: each impact carries a code-grounded WHY —
 * the concrete edge PATH (seed → … → it), the rel_type chain, and the
 * file:line at each boundary — rendered in a ranked, expandable row.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ImpactRow,
  buildBlastSection,
} from "./chat-actions-panel";
import type { BlastImpact, BlastRadiusResponse } from "@/lib/api";

const DIRECT: BlastImpact = {
  urn: "urn:bifrost:repo/svc/component/OrderController",
  name: "OrderController",
  entity_type: "component",
  repo_id: "r1",
  is_cross_repo: false,
  distance_from_seed: 1,
  path_confidence: 0.92,
  source_file: "src/order_controller.py",
  line: 88,
  qualified_name: "svc.OrderController",
  path: [
    {
      from_urn: "urn:bifrost:repo/svc/component/PricingService",
      from_name: "PricingService",
      to_urn: "urn:bifrost:repo/svc/component/OrderController",
      to_name: "OrderController",
      rel_type: "calls",
      confidence: 0.92,
      is_cross_repo: false,
      to_source_file: "src/order_controller.py",
      to_line: 88,
      to_qualified_name: "svc.OrderController",
    },
  ],
  reason:
    "`PricingService` is called by `OrderController` (calls) at src/order_controller.py:88",
};

const INDIRECT: BlastImpact = {
  urn: "urn:bifrost:repo/other/component/InventoryWriter",
  name: "InventoryWriter",
  entity_type: "component",
  repo_id: "r2",
  is_cross_repo: true,
  distance_from_seed: 2,
  path_confidence: 0.61,
  source_file: "inventory/writer.py",
  line: 14,
  qualified_name: "inventory.InventoryWriter",
  path: [
    {
      from_urn: "urn:bifrost:repo/svc/component/PricingService",
      from_name: "PricingService",
      to_urn: "urn:bifrost:repo/svc/component/OrderController",
      to_name: "OrderController",
      rel_type: "calls",
      confidence: 0.92,
      is_cross_repo: false,
      to_source_file: "src/order_controller.py",
      to_line: 88,
    },
    {
      from_urn: "urn:bifrost:repo/svc/component/OrderController",
      from_name: "OrderController",
      to_urn: "urn:bifrost:repo/other/component/InventoryWriter",
      to_name: "InventoryWriter",
      rel_type: "publishes_to",
      confidence: 0.61,
      is_cross_repo: true,
      to_source_file: "inventory/writer.py",
      to_line: 14,
    },
  ],
  reason:
    "`PricingService` --calls--> `OrderController` --publishes_to--> `InventoryWriter`; surfaces at inventory/writer.py:14",
};

describe("ImpactRow", () => {
  it("renders the edge path with rel_type chain + file:line when expanded", () => {
    render(<ImpactRow impact={INDIRECT} defaultOpen />);
    // Directness tier + cross-repo flagged.
    expect(screen.getByText("2-hop")).toBeTruthy();
    expect(screen.getByText("cross-repo")).toBeTruthy();
    // The full reason (the WHY) is shown.
    expect(screen.getByText(/surfaces at inventory\/writer\.py:14/)).toBeTruthy();
    // Each hop's rel_type is rendered as the chain link.
    expect(screen.getByText("--calls-->")).toBeTruthy();
    expect(screen.getByText("--publishes_to-->")).toBeTruthy();
    // The exact file:line boundary is visible.
    expect(screen.getByText("src/order_controller.py:88")).toBeTruthy();
    expect(screen.getByText("inventory/writer.py:14")).toBeTruthy();
  });

  it("collapses the path detail until expanded", () => {
    render(<ImpactRow impact={DIRECT} defaultOpen={false} />);
    // Collapsed: name + confidence visible, but the path detail is not.
    expect(screen.getByText("OrderController")).toBeTruthy();
    expect(screen.getByText("92%")).toBeTruthy();
    expect(screen.queryByText("--calls-->")).toBeNull();
    // Expand → the grounded reason appears.
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("--calls-->")).toBeTruthy();
    expect(screen.getByText("src/order_controller.py:88")).toBeTruthy();
  });
});

describe("buildBlastSection (Jira description)", () => {
  const RESP: BlastRadiusResponse = {
    seed_urn: "urn:bifrost:repo/svc/component/PricingService",
    seed_name: "PricingService",
    direction: "downstream",
    depth: 3,
    impacted_count: 2,
    impacts: [DIRECT, INDIRECT],
    tail_count: 149,
    nodes: [],
    edges: [],
    confidence_buckets: { high: [], medium: [], low: [] },
    soft_cap_exceeded: false,
    stats: {},
  };

  it("embeds grounded reasons + paths, drops faint links, collapses the tail", () => {
    const md = buildBlastSection(RESP);
    // Ranked, with directness tier and confidence + grounded reason.
    expect(md).toContain("**OrderController** (direct, component, 92%)");
    // The concrete path with rel_type + file:line is embedded.
    expect(md).toContain(
      "--calls--> `OrderController` (src/order_controller.py:88)",
    );
    // Faint links (below 0.7) are dropped from the honest headline.
    expect(md).not.toContain("InventoryWriter");
    // The long tail is collapsed, not enumerated.
    expect(md).toContain("149 lower-impact entities (long tail)");
  });

  it("embeds the multi-hop path when the chain is high-confidence", () => {
    const strongChain: BlastImpact = {
      ...INDIRECT,
      path_confidence: 0.81,
      path: INDIRECT.path.map((h) =>
        h.rel_type === "publishes_to" ? { ...h, confidence: 0.81 } : h,
      ),
    };
    const md = buildBlastSection({ ...RESP, impacts: [DIRECT, strongChain] });
    expect(md).toContain("**InventoryWriter** (2-hop, component, 81%)");
    expect(md).toContain(
      "--publishes_to--> `InventoryWriter` (inventory/writer.py:14)",
    );
  });
});
