/**
 * Smoke test that renders <Prose> with the actual handover body
 * Markdown that rendered as a blank page. If <Prose>
 * (ReactMarkdown + remark-gfm + the syntax-highlighter / Mermaid
 * code overrides) throws on this content, this test will fail and
 * expose the exact stack — without us needing the browser console.
 *
 * The bodies are inlined verbatim so the test is hermetic (no
 * network call to the dev API at test time).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { Prose } from "./prose";
import { BODY_1, BODY_2 } from "./__handover-fixtures";
import * as api from "@/lib/api";
import { HandoverDocDetailPage } from "@/pages/collaboration";

describe("Prose — handover smoke", () => {
  it("renders demo-training-svc (BODY_1) handover body without throwing", () => {
    expect(() => render(<Prose>{BODY_1}</Prose>)).not.toThrow();
  });

  it("renders order-fulfillment (BODY_2) handover body — full real fixture, with mermaid", async () => {
    // Surface uncaught errors so the test fails loudly instead of swallowing.
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let crash: unknown = null;
    try {
      await act(async () => {
        render(<Prose>{BODY_2}</Prose>);
        // Let microtasks (mermaid render Promise) flush.
        await new Promise((r) => setTimeout(r, 50));
      });
    } catch (e) {
      crash = e;
    }
    errSpy.mockRestore();
    expect(crash).toBeNull();
  });
});

/**
 * HandoverDocDetailPage regression test for the React hooks-order bug
 * that turned the page blank.
 *
 * Original failure mode:
 *   - First render: `doc.isLoading === true`, the component returned
 *     early *before* a downstream `useMemo(lensBodyMd)` was reached.
 *     React recorded N hooks called.
 *   - Second render (data resolved): no early return → that
 *     `useMemo` was now called for the first time → React saw
 *     "Rendered more hooks during this render than during the previous
 *     render" and threw → the page mounted nothing → blank screen.
 *
 * Fix: the `useMemo` was relocated above the conditional returns so
 * the hook-call sequence is identical across the loading→success
 * transition.
 *
 * This test mounts the real page through QueryClient + MemoryRouter,
 * lets the loading state pass, then asserts the resolved body is
 * actually present in the DOM. If the hooks-order regression returns
 * the second render will throw and the body text will never appear.
 */
describe("HandoverDocDetailPage — render across loading→success", () => {
  const HID = "00000000-0000-0000-0000-000000000001";

  let originalIO: typeof IntersectionObserver | undefined;
  beforeEach(() => {
    // jsdom doesn't ship IntersectionObserver; the Mermaid component
    // falls back gracefully but we mute it explicitly to keep the
    // test deterministic.
    originalIO = (globalThis as { IntersectionObserver?: typeof IntersectionObserver }).IntersectionObserver;
    (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserver | undefined }).IntersectionObserver =
      undefined;
  });
  afterEach(() => {
    (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserver | undefined }).IntersectionObserver = originalIO;
    vi.restoreAllMocks();
  });

  it("does not blank-screen on loading → success transition (hooks-order regression)", async () => {
    // Stub the network calls the page (and its sibling panels) make.
    vi.spyOn(api, "getHandoverDoc").mockResolvedValue({
      id: HID,
      scope_kind: "repo",
      scope_value: "demo-training-svc",
      parent_id: null,
      fs_path: null,
      freshness_state: "fresh",
      last_generated_at: "2026-04-26T12:00:00Z",
      last_validated_at: "2026-04-26T12:00:00Z",
      generation_cost_usd: 0,
      archived_at: null,
      created_at: "2026-04-26T12:00:00Z",
      updated_at: "2026-04-26T12:00:00Z",
      body_md: BODY_1,
    } as unknown as Awaited<ReturnType<typeof api.getHandoverDoc>>);
    vi.spyOn(api, "listRepos").mockResolvedValue([]);
    vi.spyOn(api, "getHandoverAudits").mockResolvedValue([]);
    vi.spyOn(api, "getHandoverDoubtCandidates").mockResolvedValue([]);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    const { container } = render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/collaboration/handovers/${HID}`]}>
          <Routes>
            <Route
              path="/collaboration/handovers/:id"
              element={<HandoverDocDetailPage />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // Page must reach the resolved render. The scope_value only
    // appears once doc.data is non-null AND the post-early-return
    // useMemo has run without violating hooks-order — i.e. on the
    // second render. If hooks-order was broken React would unmount
    // and the container would stay empty.
    await waitFor(
      () => {
        expect(container.textContent ?? "").toContain("demo-training-svc");
      },
      { timeout: 1500 },
    );
  });
});
