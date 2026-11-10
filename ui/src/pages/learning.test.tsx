import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { getBenchmarksSummary } from "@/lib/api";
import { LearningPage } from "./learning";

vi.mock("@/lib/api", () => ({
  getLearningStats: vi.fn().mockResolvedValue({
    deltas: {
      total: 0, auto_downgrades: 0, auto_upgrades: 0, pending_upgrades: 0,
      noise: 0, actionable: 0, flat: 0, last_7_days: 0,
      targets_touched: 0, tasks_involved: 0,
    },
    judgements: { total: 0, pending: 0, confirmed: 0, contradicted: 0 },
    config: { upgrade_confirmations_needed: 2, noise_floor: 0.05 },
  }),
  listLearningDeltas: vi.fn().mockResolvedValue([]),
  listLearningJudgements: vi.fn().mockResolvedValue([]),
  listPendingUpgrades: vi.fn().mockResolvedValue([]),
  getCacheStats: vi.fn().mockResolvedValue({
    lifetime: { hits: 0, misses: 0, hit_rate: 0 },
    window_hours: 24, window: { hits: 0, misses: 0 },
    by_kind: [], top_targets: [],
  }),
  getAgentQuality: vi.fn().mockResolvedValue({
    window_days: 30,
    trend_window_days: 7,
    verify_distribution: { total: 0, total_recent: 0, by_status: [] },
    plan_outcomes: { total: 0, by_status: [] },
    agent_metrics: [],
    assumption_tiers: { total_active: 0, by_tier: [], by_tier_source: [] },
    plan_readiness: {
      ready: { n_phases: 0, all_completed: 0, any_failed: 0 },
      not_ready: { n_phases: 0, all_completed: 0, any_failed: 0 },
      optimism_score: null,
    },
    quick_mode: { invocations: 0, successes: 0, blocked: 0, cancelled: 0, success_rate: 0 },
    tasks_by_state: {},
  }),
  // acme has 0 runs; dlvr has the most → default selection must pick dlvr.
  getBenchmarksDomains: vi.fn().mockResolvedValue([
    { domain: "acme", n_questions: 11, n_runs: 0, last_run_at: null },
    { domain: "dlvr", n_questions: 9, n_runs: 18, last_run_at: "2026-05-01T00:00:00Z" },
  ]),
  getBenchmarksSummary: vi.fn().mockResolvedValue({ domain: "dlvr", builds: [] }),
  getBenchmarksQuestions: vi.fn().mockResolvedValue([]),
  getBenchmarksRuns: vi.fn().mockResolvedValue([]),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{<LearningPage />}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LearningPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the Learning heading", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Learning", level: 1 })).toBeInTheDocument();
  });

  it("shows an empty-state (not 6 blank cards) when agent-quality has no signal", async () => {
    renderPage();
    expect(await screen.findByText(/No agent-quality signal yet/i)).toBeInTheDocument();
  });

  it("defaults the benchmark scoreboard to the most-populated domain, not empty 'acme'", async () => {
    renderPage();
    // effectiveDomain auto-picks the highest n_runs domain → summary fetched for dlvr.
    await waitFor(() => {
      expect(vi.mocked(getBenchmarksSummary)).toHaveBeenCalledWith("dlvr");
    });
    expect(vi.mocked(getBenchmarksSummary)).not.toHaveBeenCalledWith("acme");
  });
});
