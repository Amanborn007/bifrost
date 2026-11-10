import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { CostsPage } from "./costs";

vi.mock("@/lib/api", () => ({
  getCostSummary: vi.fn().mockResolvedValue({
    total_cost_usd: 4.52,
    total_input_tokens: 123456,
    total_output_tokens: 45678,
    total_calls: 200,
    by_model: [
      {
        key: "claude-sonnet-4-6",
        label: "claude-sonnet-4-6",
        cost_usd: 4.0,
        input_tokens: 100000,
        output_tokens: 40000,
        calls: 180,
      },
    ],
    by_phase: [
      {
        key: "D",
        label: "Components",
        cost_usd: 3.2,
        input_tokens: 80000,
        output_tokens: 30000,
        calls: 150,
      },
    ],
    by_repo: [
      {
        key: "repo-1",
        label: "test-repo",
        cost_usd: 4.5,
        input_tokens: 120000,
        output_tokens: 45000,
        calls: 199,
      },
    ],
    last_7_days: [
      { date: "2026-04-14", cost_usd: 0.5 },
      { date: "2026-04-15", cost_usd: 1.2 },
      { date: "2026-04-16", cost_usd: 0.8 },
      { date: "2026-04-17", cost_usd: 0.4 },
      { date: "2026-04-18", cost_usd: 0.6 },
      { date: "2026-04-19", cost_usd: 0.3 },
      { date: "2026-04-20", cost_usd: 0.7 },
    ],
    window: "30d",
    as_of: "2026-04-20T12:00:00Z",
  }),
}));

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CostsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the spend heading", () => {
    renderWithProviders(<CostsPage />);
    expect(screen.getByText("Spend")).toBeInTheDocument();
  });

  it("renders aggregate stat cards", () => {
    renderWithProviders(<CostsPage />);
    expect(screen.getByText("Total spend")).toBeInTheDocument();
    expect(screen.getByText("LLM calls")).toBeInTheDocument();
    expect(screen.getByText("Input tokens")).toBeInTheDocument();
  });

  it("renders breakdown headings after loading", async () => {
    renderWithProviders(<CostsPage />);
    expect(await screen.findByText("By model")).toBeInTheDocument();
    expect(screen.getByText("By phase")).toBeInTheDocument();
    expect(screen.getByText("By repository")).toBeInTheDocument();
  });

  it("renders the 7-day section", () => {
    renderWithProviders(<CostsPage />);
    expect(
      screen.getByText("Daily spend · last 7 days"),
    ).toBeInTheDocument();
  });
});
