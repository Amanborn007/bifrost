import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { DashboardPage } from "./dashboard";

// Mock the API module
// ExtractionProgressCard subscribes to SSE which isn't available in jsdom;
// stub it so the dashboard tests only exercise the card's presence, not its guts.
vi.mock("@/components/extraction-progress-card", () => ({
  ExtractionProgressCard: ({ task }: { task: { id: string; query: string } }) => (
    <div data-testid={`extraction-card-${task.id}`}>{task.query}</div>
  ),
}));

vi.mock("@/lib/api", () => ({
  listRepos: vi.fn().mockResolvedValue([
    {
      id: "repo-1",
      urn: "urn:bifrost:repo:test-repo",
      name: "test-repo",
      url: "https://github.com/org/test-repo",
      domain_id: null,
      freshness_state: "fresh",
      index_status: "complete",
      last_indexed_at: "2026-04-14T10:00:00Z",
    },
  ]),
  listTasks: vi.fn().mockResolvedValue([
    {
      id: "task-1",
      type: "extraction",
      status: "delivered",
      query: "Extract knowledge from test-repo",
      created_at: "2026-04-14T10:00:00Z",
      updated_at: "2026-04-14T10:05:00Z",
      result: { components: 12, flows: 3, dependencies: 7 },
      error_message: null,
    },
  ]),
  triggerExtraction: vi.fn().mockResolvedValue({ task_id: "task-2" }),
  listDomains: vi.fn().mockResolvedValue([]),
  registerRepo: vi.fn().mockResolvedValue({
    id: "repo-2",
    urn: "urn:bifrost:repo:new-repo",
    status: "registered",
  }),
}));

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DashboardPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the dashboard heading", () => {
    renderWithProviders(<DashboardPage />);
    expect(screen.getByText("State of the bridge")).toBeInTheDocument();
  });

  it("renders health summary cards", () => {
    renderWithProviders(<DashboardPage />);
    // "Repositories" appears in both the hero stat tile and section header,
    // so use getAllByText. "Fresh" and "Stale" are unique.
    expect(screen.getAllByText("Repositories").length).toBeGreaterThan(0);
    expect(screen.getByText("Fresh")).toBeInTheDocument();
    expect(screen.getByText("Stale")).toBeInTheDocument();
  });

  it("shows repo data after loading", async () => {
    renderWithProviders(<DashboardPage />);
    expect(await screen.findByText("test-repo")).toBeInTheDocument();
  });

  it("shows tasks after loading", async () => {
    renderWithProviders(<DashboardPage />);
    expect(
      await screen.findByText("Extract knowledge from test-repo"),
    ).toBeInTheDocument();
  });

  it("surfaces an error banner when the API is unreachable", async () => {
    const api = await import("@/lib/api");
    (api.listRepos as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("API 500: boom"),
    );
    (api.listTasks as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("API 500: boom"),
    );
    (api.listDomains as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("API 500: boom"),
    );
    renderWithProviders(<DashboardPage />);
    expect(
      await screen.findByText("Couldn't reach the Bifrost API"),
    ).toBeInTheDocument();
  });

  it("splits active extractions into 'Running now' and 'Queued'", async () => {
    const api = await import("@/lib/api");
    (api.listTasks as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      {
        id: "task-running",
        type: "extraction",
        status: "running",
        query: "Extract knowledge from hot-repo",
        created_at: "2026-04-14T10:00:00Z",
        updated_at: "2026-04-14T10:05:00Z",
        result: null,
        error_message: null,
      },
      {
        id: "task-queued-1",
        type: "extraction",
        status: "received",
        query: "Extract knowledge from waiting-repo-1",
        created_at: "2026-04-14T10:00:00Z",
        updated_at: "2026-04-14T10:00:00Z",
        result: null,
        error_message: null,
      },
      {
        id: "task-queued-2",
        type: "extraction",
        status: "received",
        query: "Extract knowledge from waiting-repo-2",
        created_at: "2026-04-14T10:00:00Z",
        updated_at: "2026-04-14T10:00:00Z",
        result: null,
        error_message: null,
      },
    ]);
    renderWithProviders(<DashboardPage />);
    // Section headers are <h2> — scope to those to avoid colliding with
    // status-badge labels like "Queued" that also exist elsewhere on the page.
    expect(
      await screen.findByRole("heading", { name: "Running now", level: 2 }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Queued", level: 2 }),
    ).toBeInTheDocument();
    // Queued rows strip the "Extract knowledge from " prefix
    expect(screen.getByText("waiting-repo-1")).toBeInTheDocument();
    expect(screen.getByText("waiting-repo-2")).toBeInTheDocument();
  });
});
