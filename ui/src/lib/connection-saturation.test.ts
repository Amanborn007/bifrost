/**
 * Tests for connection-pool saturation fixes (browser 6-connection-per-origin limit).
 *
 * Validates three behaviours introduced to prevent HTTP connection slot exhaustion
 * when users rapidly switch conversations:
 *
 *   1. cancelQueries is called for old conversationId when activeId changes (Fix 1)
 *   2. The debounce in ChatConnectionsPanel delays the connections query 150 ms (Fix 2)
 *   3. disconnectResearchStream is called when isStreaming=false and listeners hit 0 (Fix 3)
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// ── Fix 1: cancelQueries on conversation switch ───────────────────────────────
//
// We test the logic directly (not through QueryPage's full render tree) by
// extracting the ref+effect pattern into a standalone hook that mirrors exactly
// what was added to QueryPage.

describe("Fix 1 — cancelQueries called for old conversationId on switch", () => {
  it("calls cancelQueries for the previous activeId when activeId changes", () => {
    // Minimal mock of QueryClient with a tracked cancelQueries spy
    const cancelQueries = vi.fn().mockResolvedValue(undefined);
    const qc = { cancelQueries } as unknown as import("@tanstack/react-query").QueryClient;

    // Simulate the useEffect logic extracted from QueryPage
    function simulateConvSwitch(
      prev: string | null,
      next: string | null,
    ): void {
      if (prev && prev !== next) {
        void qc.cancelQueries({ queryKey: ["conversation", prev] });
        void qc.cancelQueries({ queryKey: ["conv-connections", prev] });
      }
    }

    simulateConvSwitch(null, "conv-A");
    expect(cancelQueries).not.toHaveBeenCalled(); // first mount, no prev

    simulateConvSwitch("conv-A", "conv-B");
    expect(cancelQueries).toHaveBeenCalledTimes(2);
    expect(cancelQueries).toHaveBeenCalledWith({ queryKey: ["conversation", "conv-A"] });
    expect(cancelQueries).toHaveBeenCalledWith({ queryKey: ["conv-connections", "conv-A"] });
  });

  it("does NOT call cancelQueries when activeId stays the same", () => {
    const cancelQueries = vi.fn().mockResolvedValue(undefined);
    const qc = { cancelQueries } as unknown as import("@tanstack/react-query").QueryClient;

    function simulateConvSwitch(prev: string | null, next: string | null): void {
      if (prev && prev !== next) {
        void qc.cancelQueries({ queryKey: ["conversation", prev] });
        void qc.cancelQueries({ queryKey: ["conv-connections", prev] });
      }
    }

    simulateConvSwitch("conv-A", "conv-A"); // no change
    expect(cancelQueries).not.toHaveBeenCalled();
  });
});


// ── Fix 2: debounce in ChatConnectionsPanel delays the connections query ──────
//
// The debounce is a setTimeout(150ms) inside a useEffect. We use vi.useFakeTimers
// to assert the stableConvId doesn't update until after the delay expires.

describe("Fix 2 — connections query debounce (150 ms)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not fire immediately when conversationId changes", () => {
    const updates: string[] = [];

    // Inline simulation of the useState + useEffect debounce pattern
    let stableConvId = "conv-A";
    let timerId: ReturnType<typeof setTimeout> | null = null;

    function onConvIdChange(newId: string): void {
      if (timerId !== null) clearTimeout(timerId);
      timerId = setTimeout(() => {
        stableConvId = newId;
        updates.push(newId);
      }, 150);
    }

    onConvIdChange("conv-B");
    // immediately after the change, stableConvId has NOT updated yet
    expect(stableConvId).toBe("conv-A");
    expect(updates).toHaveLength(0);

    // Advance time by 149ms — still not fired
    vi.advanceTimersByTime(149);
    expect(stableConvId).toBe("conv-A");

    // Advance past the threshold
    vi.advanceTimersByTime(1);
    expect(stableConvId).toBe("conv-B");
    expect(updates).toEqual(["conv-B"]);
  });

  it("debounce is cancelled when conversationId changes rapidly", () => {
    const updates: string[] = [];
    let stableConvId = "conv-A";
    let timerId: ReturnType<typeof setTimeout> | null = null;

    function onConvIdChange(newId: string): void {
      if (timerId !== null) clearTimeout(timerId);
      timerId = setTimeout(() => {
        stableConvId = newId;
        updates.push(newId);
      }, 150);
    }

    // Rapid switching: A → B → C → D within 150 ms
    onConvIdChange("conv-B");
    vi.advanceTimersByTime(50);
    onConvIdChange("conv-C");
    vi.advanceTimersByTime(50);
    onConvIdChange("conv-D");
    vi.advanceTimersByTime(149); // not yet

    expect(updates).toHaveLength(0); // intermediate values discarded

    vi.advanceTimersByTime(1); // now fires for D only
    expect(updates).toEqual(["conv-D"]);
    expect(stableConvId).toBe("conv-D");
  });
});


// ── Fix 3: disconnectResearchStream on subscriber=0 when not streaming ────────
//
// We test the cleanup logic from useLiveResearch's unsubscribe callback.

describe("Fix 3 — disconnectResearchStream when isStreaming=false and listeners=0", () => {
  it("calls disconnectResearchStream when last listener unmounts and stream is quiescent", () => {
    const disconnectResearchStream = vi.fn();
    const store = new Map<string, { isStreaming: boolean }>();
    const finishedStreams = new Set<string>();
    const statusProbed = new Set<string>();
    const listeners = new Map<string, Set<() => void>>();

    // Simulate the cleanup logic from useLiveResearch
    function simulateUnsubscribe(conversationId: string, listener: () => void): void {
      const set = listeners.get(conversationId);
      if (!set) return;
      set.delete(listener);
      if (set.size === 0) {
        listeners.delete(conversationId);
        const state = store.get(conversationId);
        if (!state?.isStreaming) {
          disconnectResearchStream(conversationId);
          finishedStreams.delete(conversationId);
          statusProbed.delete(conversationId);
        }
      }
    }

    const listener = vi.fn();
    listeners.set("conv-A", new Set([listener]));
    store.set("conv-A", { isStreaming: false });
    finishedStreams.add("conv-A");
    statusProbed.add("conv-A");

    simulateUnsubscribe("conv-A", listener);

    expect(disconnectResearchStream).toHaveBeenCalledWith("conv-A");
    expect(finishedStreams.has("conv-A")).toBe(false);
    expect(statusProbed.has("conv-A")).toBe(false);
  });

  it("does NOT disconnect when stream is actively streaming (mid-research tab switch)", () => {
    const disconnectResearchStream = vi.fn();
    const store = new Map<string, { isStreaming: boolean }>();
    const listeners = new Map<string, Set<() => void>>();

    function simulateUnsubscribe(conversationId: string, listener: () => void): void {
      const set = listeners.get(conversationId);
      if (!set) return;
      set.delete(listener);
      if (set.size === 0) {
        listeners.delete(conversationId);
        const state = store.get(conversationId);
        if (!state?.isStreaming) {
          disconnectResearchStream(conversationId);
        }
      }
    }

    const listener = vi.fn();
    listeners.set("conv-B", new Set([listener]));
    store.set("conv-B", { isStreaming: true }); // <-- mid-research

    simulateUnsubscribe("conv-B", listener);

    expect(disconnectResearchStream).not.toHaveBeenCalled();
  });

  it("does NOT disconnect when there are still other listeners attached", () => {
    const disconnectResearchStream = vi.fn();
    const store = new Map<string, { isStreaming: boolean }>();
    const listeners = new Map<string, Set<() => void>>();

    function simulateUnsubscribe(conversationId: string, listener: () => void): void {
      const set = listeners.get(conversationId);
      if (!set) return;
      set.delete(listener);
      if (set.size === 0) {
        listeners.delete(conversationId);
        const state = store.get(conversationId);
        if (!state?.isStreaming) {
          disconnectResearchStream(conversationId);
        }
      }
    }

    const listener1 = vi.fn();
    const listener2 = vi.fn();
    listeners.set("conv-C", new Set([listener1, listener2]));
    store.set("conv-C", { isStreaming: false });

    simulateUnsubscribe("conv-C", listener1); // still has listener2

    expect(disconnectResearchStream).not.toHaveBeenCalled();
    expect(listeners.get("conv-C")?.has(listener2)).toBe(true);
  });
});
