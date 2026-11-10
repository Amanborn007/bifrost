import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { getConversation, appendMessage } from "@/lib/api";
import { QueryPage } from "./query";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    // Network-bound functions get stubbed; types/classes/utilities pass
    // through from the real module.
    listRepos: vi.fn().mockResolvedValue([]),
    listConversations: vi.fn().mockResolvedValue([]),
    listArchivedConversations: vi.fn().mockResolvedValue([]),
    listOrphanSessions: vi.fn().mockResolvedValue([]),
    adoptOrphanSession: vi.fn().mockResolvedValue({}),
    getConversation: vi.fn().mockResolvedValue(null),
    createConversation: vi.fn().mockResolvedValue({ id: "conv-1" }),
    renameConversation: vi.fn().mockResolvedValue({}),
    deleteConversation: vi.fn().mockResolvedValue({}),
    restoreConversation: vi.fn().mockResolvedValue({}),
    cancelConversation: vi.fn().mockResolvedValue({}),
    appendMessage: vi.fn().mockResolvedValue({
      user_message_id: "msg-1",
      stream_url: "/api/v1/conversations/conv-1/stream",
    }),
    submitMessageFeedback: vi.fn().mockResolvedValue({}),
    spawnTaskFromChat: vi.fn().mockResolvedValue({ id: "task-1" }),
    getConversationTasks: vi.fn().mockResolvedValue([]),
    classifyIntent: vi.fn().mockResolvedValue({ intent: "ask", confidence: 1 }),
    runQuick: vi.fn().mockResolvedValue({ status: "ok" }),
  };
});

vi.mock("@/lib/sse", () => ({
  subscribeToConversationStream: vi.fn(() => () => {}),
  subscribeToTaskStream: vi.fn(() => () => {}),
}));

function renderWithProviders(
  ui: React.ReactElement,
  { initialEntries = ["/"] }: { initialEntries?: string[] } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// A conversation whose last (and only) message is the user's question with
// no assistant reply — i.e. a failed/interrupted turn as it looks AFTER a
// reload, once the ephemeral in-store error is gone.
function danglingConversation() {
  return {
    id: "conv-1",
    title: "what is the dispatcher",
    scope_repo_ids: [],
    messages: [
      {
        id: "msg-1",
        conversation_id: "conv-1",
        turn_index: 0,
        role: "user",
        content: "what is the dispatcher and what does it do?",
        sources: [],
        confidence: null,
        reasoning: null,
        gaps: [],
        token_counts: {},
        cost_usd: null,
        created_at: new Date().toISOString(),
      },
    ],
  };
}

// A completed conversation whose assistant turn carries server-generated
// "Ask next" follow-up suggestions (the persisted suggested_followups
// round-tripped from the conversation GET endpoint).
function conversationWithFollowups() {
  return {
    id: "conv-fup",
    title: "auth flow",
    scope_repo_ids: [],
    messages: [
      {
        id: "msg-1",
        conversation_id: "conv-fup",
        turn_index: 0,
        role: "user",
        content: "How does the auth flow work?",
        sources: [],
        confidence: null,
        reasoning: null,
        gaps: [],
        token_counts: {},
        cost_usd: null,
        created_at: new Date().toISOString(),
      },
      {
        id: "msg-2",
        conversation_id: "conv-fup",
        turn_index: 1,
        role: "assistant",
        content: "The auth flow validates tokens in auth-svc.",
        sources: [],
        confidence: "inferred",
        reasoning: null,
        gaps: [],
        token_counts: {},
        suggested_followups: [
          {
            prompt: "How does auth-svc verify JWT signatures?",
            rationale: "closes the signature-verification gap",
            repos_to_index: [],
          },
        ],
        cost_usd: null,
        created_at: new Date().toISOString(),
      },
    ],
  };
}

describe("QueryPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the empty chat starter heading when no conversation exists", () => {
    renderWithProviders(<QueryPage />);
    expect(screen.getByText("Bifrost Chat")).toBeInTheDocument();
  });

  it("renders the chat composer placeholder", () => {
    renderWithProviders(<QueryPage />);
    expect(
      screen.getByPlaceholderText("Ask a question about your codebase…"),
    ).toBeInTheDocument();
  });

  it("renders the conversations sidebar heading", () => {
    renderWithProviders(<QueryPage />);
    expect(screen.getByText("Conversations")).toBeInTheDocument();
  });

  it("shows a Retry affordance on a dangling (unanswered) user turn", async () => {
    vi.mocked(getConversation).mockResolvedValue(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      danglingConversation() as any,
    );
    renderWithProviders(<QueryPage />, {
      initialEntries: ["/query?conv=conv-1"],
    });

    // The failed/interrupted turn surfaces a clear retry affordance even
    // though there is no live error in the store anymore.
    expect(
      await screen.findByText(/didn't get an answer/i),
    ).toBeInTheDocument();
    const retryBtn = await screen.findByRole("button", { name: /^retry$/i });
    expect(retryBtn).toBeInTheDocument();
  });

  it("re-submits the last question when Retry is clicked", async () => {
    vi.mocked(getConversation).mockResolvedValue(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      danglingConversation() as any,
    );
    renderWithProviders(<QueryPage />, {
      initialEntries: ["/query?conv=conv-1"],
    });

    const retryBtn = await screen.findByRole("button", { name: /^retry$/i });
    fireEvent.click(retryBtn);

    // onRegenerate re-submits the last user message via appendMessage.
    await waitFor(() => {
      expect(vi.mocked(appendMessage)).toHaveBeenCalled();
    });
    const [convArg, contentArg] = vi.mocked(appendMessage).mock.calls[0];
    expect(convArg).toBe("conv-1");
    expect(contentArg).toMatch(/dispatcher/i);
  });

  it("renders server-generated suggested-followup chips on the last assistant turn", async () => {
    vi.mocked(getConversation).mockResolvedValue(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      conversationWithFollowups() as any,
    );
    renderWithProviders(<QueryPage />, {
      initialEntries: ["/query?conv=conv-fup"],
    });

    const chip = await screen.findByRole("button", {
      name: /verify JWT signatures/i,
    });
    expect(chip).toBeInTheDocument();
    // Rationale surfaces as the tooltip.
    expect(chip).toHaveAttribute(
      "title",
      "closes the signature-verification gap",
    );
  });

  it("re-submits the prompt as a new turn when a followup chip is clicked", async () => {
    vi.mocked(getConversation).mockResolvedValue(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      conversationWithFollowups() as any,
    );
    renderWithProviders(<QueryPage />, {
      initialEntries: ["/query?conv=conv-fup"],
    });

    const chip = await screen.findByRole("button", {
      name: /verify JWT signatures/i,
    });
    fireEvent.click(chip);

    // Clicking the chip routes through onSend → appendMessage with the
    // chip's prompt as a fresh user turn in the same conversation.
    await waitFor(() => {
      expect(vi.mocked(appendMessage)).toHaveBeenCalled();
    });
    const [convArg, contentArg] = vi.mocked(appendMessage).mock.calls[0];
    expect(convArg).toBe("conv-fup");
    expect(contentArg).toBe("How does auth-svc verify JWT signatures?");
  });
});
