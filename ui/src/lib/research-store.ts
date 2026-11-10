/**
 * Module-level store for live chat research state.
 *
 * The problem this solves: `QueryPage` used to hold streaming state
 * (research pipeline, status text, synthesis delta, tool uses) in
 * component-local `useState`. Navigating to another page unmounted
 * the component, destroyed the state, AND closed the SSE connection
 * — so returning to the chat mid-query showed a blank pane while the
 * backend was still producing events.
 *
 * This store keeps one LiveState per conversation id, survives
 * mount/unmount, and owns the SSE subscription directly. Components
 * subscribe via `useLiveResearch(conversationId)` (React 18
 * `useSyncExternalStore`). Unmount just drops a listener — the
 * stream stays open, state keeps advancing, and remount rehydrates
 * instantly.
 */

import { useSyncExternalStore } from "react";
import { getResearchStatus } from "@/lib/api";
import { subscribeToConversationStream, type SSEEvent } from "@/lib/sse";
import {
  applyResearchEvent,
  EMPTY_RESEARCH_STATE,
  type ResearchState,
} from "@/components/research-progress";

export interface LiveResearchState {
  status: string;
  text: string;
  tools: Array<{ name: string; input: Record<string, unknown> }>;
  research: ResearchState;
  isStreaming: boolean;
  /** Live "Ask next" follow-up prompts emitted on the
   *  ``suggested_followups`` SSE event, so chips can render under the
   *  streaming answer before the persisted message refetch lands. */
  suggestedFollowups: Array<{
    prompt: string;
    rationale?: string;
    repos_to_index?: string[];
  }>;
  completedAt: number | null;  // unix ms when done/error arrived; for TTL cleanup
  /** Set when the coordinator emitted an ``error`` SSE event. The
   *  string is user-actionable (multi-line allowed) so the UI can
   *  render it as a clear error block instead of a console-only
   *  toast. Cleared when a fresh turn starts. */
  errorMessage: string | null;
}

const EMPTY: LiveResearchState = {
  status: "",
  text: "",
  tools: [],
  research: EMPTY_RESEARCH_STATE,
  isStreaming: false,
  suggestedFollowups: [],
  completedAt: null,
  errorMessage: null,
};

// Per-conversation state, keyed by conversation id.
const store = new Map<string, LiveResearchState>();
// Listeners per conversation id — notified on state change.
const listeners = new Map<string, Set<() => void>>();
// One SSE unsubscribe per conversation id.
const subscriptions = new Map<string, () => void>();
// Marker: this conv's stream finished (assistant_message / error /
// done seen). Used to short-circuit reconnect loops — without this,
// the SSE replay buffer keeps re-firing the terminal ``done`` event
// on every fresh subscribe, our onClose drops the subscription, and
// the next mount/visibility tick re-subscribes → infinite loop that
// hammers the server with /research-status + /stream every few ms.
// Cleared in markSubmitting(true) when a fresh turn begins.
const finishedStreams = new Set<string>();
// One-shot guard for the page-mount /research-status probe — without
// this, every reconnect re-fired the probe.
const statusProbed = new Set<string>();
// onMessage callbacks per conversation (e.g., to invalidate react-query
// caches on `assistant_message`). Fired in addition to state updates.
type MessageHook = (event: SSEEvent) => void;
const hooks = new Map<string, Set<MessageHook>>();

function getSnapshot(conversationId: string): LiveResearchState {
  return store.get(conversationId) ?? EMPTY;
}

function emit(conversationId: string): void {
  listeners.get(conversationId)?.forEach((l) => l());
}

function update(
  conversationId: string,
  updater: (s: LiveResearchState) => LiveResearchState,
): void {
  const prev = store.get(conversationId) ?? EMPTY;
  const next = updater(prev);
  if (next !== prev) {
    store.set(conversationId, next);
    emit(conversationId);
  }
}

function fireHooks(conversationId: string, event: SSEEvent): void {
  hooks.get(conversationId)?.forEach((h) => {
    try {
      h(event);
    } catch {
      /* hook errors shouldn't crash the stream */
    }
  });
}

/**
 * Open (or reuse) the SSE stream for this conversation. Idempotent,
 * BUT: if the underlying EventSource has closed (server terminated,
 * tab suspended, network blip), the subscription entry is evicted and
 * a fresh EventSource is opened. The backend's replay buffer catches
 * the new connection up on any events it missed.
 */
export function connectResearchStream(conversationId: string): void {
  if (subscriptions.has(conversationId)) return;
  // Don't reconnect to a conv whose stream already finished. The
  // backend's SSE replay buffer holds terminal events for 5 min;
  // every reconnect would replay ``done``/``error``, fire onClose,
  // and re-enter this function — a tight loop. New turns clear
  // ``finishedStreams`` via markSubmitting(true).
  //
  // BUT — for sequenced/external-driven runs (the benchmark harness
  // POSTs each next turn directly via the API, bypassing the browser
  // composer), ``markSubmitting`` never fires in this tab. The browser
  // saw the prior turn's ``done`` (so this conv is in
  // ``finishedStreams``), then a new server-side turn started without
  // the browser knowing. Without a probe, the loader never reappears.
  // So when ``finishedStreams`` blocks reconnect, fire a one-shot
  // status probe; if a run is now in-flight, clear the finished mark
  // and recurse to actually open the EventSource.
  if (finishedStreams.has(conversationId)) {
    if (!statusProbed.has(conversationId)) {
      statusProbed.add(conversationId);
      void getResearchStatus(conversationId)
        .then((status) => {
          if (status.in_flight) {
            finishedStreams.delete(conversationId);
            statusProbed.delete(conversationId);
            connectResearchStream(conversationId);
          } else {
            // Stream is finished and no new run is in flight — clear
            // stale streaming text/tools/status so switching back to
            // this conversation doesn't show a prior turn's output.
            update(conversationId, (s) => ({ ...s, text: "", tools: [], status: "" }));
          }
        })
        .catch(() => {
          /* silently swallow */
        });
    }
    return;
  }

  // Resumability — on first subscribe ONLY (page mount), ask the
  // backend whether a research run is in flight for this conv. If
  // yes, flip ``isStreaming`` true so the progress widget reappears.
  // Guarded by ``statusProbed`` so reconnects don't re-fire the
  // probe — without this guard, a tab-visibility flip would
  // hammer /research-status every few ms.
  if (!statusProbed.has(conversationId)) {
    statusProbed.add(conversationId);
    void getResearchStatus(conversationId)
      .then((status) => {
        if (status.in_flight) {
          update(conversationId, (s) => {
            if (s.isStreaming) return s;
            return { ...s, isStreaming: true, errorMessage: null };
          });
          armWatchdog(conversationId);
        }
      })
      .catch(() => {
        /* silently swallow — pre-existing behaviour */
      });
  }

  const unsub = subscribeToConversationStream(conversationId, {
    onEvent: (e: SSEEvent) => {
      // Any inbound event proves the stream is alive,
      // so re-arm the silence watchdog. Terminal events clear it
      // explicitly via the markSubmitting(false) path below.
      const isTerminal =
        e.event_type === "assistant_message" ||
        e.event_type === "error" ||
        e.event_type === "done";
      // Page-refresh recovery: when a non-terminal event arrives but
      // ``isStreaming`` is currently false, the user just reconnected
      // (browser refresh, tab return, network blip) to a still-running
      // task on the server. The local-only ``submitting`` flag was
      // wiped by the page reload, so the SSE replay is the ONLY signal
      // that work is in flight. Flipping ``isStreaming`` back on here
      // makes the progress widget reappear after refresh — same UX
      // as if the user had never reloaded.
      const isReplayResume =
        !isTerminal && !(store.get(conversationId) ?? EMPTY).isStreaming;
      if (isReplayResume) {
        armWatchdog(conversationId);
      }
      if (!isTerminal) {
        rearmWatchdog(conversationId);
      } else {
        clearWatchdog(conversationId);
      }

      update(conversationId, (prev) => {
        const nextResearch = applyResearchEvent(
          prev.research,
          e.event_type,
          e.data,
        );

        // Keep ``isStreaming`` true for as long as
        // non-terminal events are arriving. Terminal events
        // (assistant_message / error / done) explicitly flip it back
        // to false in their dedicated branches below.
        const baseStreaming = isReplayResume ? true : prev.isStreaming;

        if (e.event_type === "status") {
          return {
            ...prev,
            research: nextResearch,
            status: (e.data.message as string) ?? "",
            isStreaming: baseStreaming,
          };
        }
        if (e.event_type === "token" || e.event_type === "synthesis_delta") {
          const delta = (e.data.text as string) ?? "";
          if (!delta) return prev;
          return {
            ...prev,
            research: nextResearch,
            text: prev.text + delta,
            status: "",  // tokens flowing → drop stage text
            isStreaming: baseStreaming,
          };
        }
        if (e.event_type === "tool_use") {
          return {
            ...prev,
            research: nextResearch,
            tools: [
              ...prev.tools,
              {
                name: (e.data.name as string) ?? "",
                input: (e.data.input as Record<string, unknown>) ?? {},
              },
            ],
            isStreaming: baseStreaming,
          };
        }
        if (e.event_type === "assistant_message") {
          // Don't reset to EMPTY when the
          // terminal message arrives. The persisted message comes
          // through react-query cache invalidation; if we wipe the
          // streaming text first, there's a render frame where the
          // user sees nothing. Keep the streaming text + research
          // state visible until the cache refetch lands; only flip
          // isStreaming + completedAt.
          return {
            ...prev,
            research: nextResearch,
            isStreaming: false,
            completedAt: Date.now(),
            status: "",
          };
        }
        if (e.event_type === "error") {
          // Capture the error message so the UI can render it as a
          // clear, formatted block. The coordinator emits multi-line
          // strings (e.g. with bullet points for actionable hints —
          // see the "no_keyword_match" handler) so we keep newlines
          // intact for the renderer to split on.
          const errMsg =
            (e.data.error as string | undefined)
            ?? (e.data.message as string | undefined)
            ?? "Research failed.";
          return {
            ...prev,
            research: nextResearch,
            isStreaming: false,
            completedAt: Date.now(),
            status: "",
            errorMessage: errMsg,
          };
        }
        if (e.event_type === "suggested_followups") {
          const raw = (e.data.suggestions as unknown[]) ?? [];
          const suggestions = raw
            .filter((s): s is Record<string, unknown> =>
              typeof s === "object" && s !== null,
            )
            .map((s) => ({
              prompt: String(s.prompt ?? ""),
              rationale:
                typeof s.rationale === "string" ? s.rationale : undefined,
              repos_to_index: Array.isArray(s.repos_to_index)
                ? (s.repos_to_index as string[])
                : undefined,
            }))
            .filter((s) => s.prompt);
          return {
            ...prev,
            research: nextResearch,
            suggestedFollowups: suggestions,
            isStreaming: baseStreaming,
          };
        }
        if (e.event_type === "done") {
          return {
            ...prev,
            research: nextResearch,
            status: "",
            completedAt: Date.now(),
            isStreaming: false,
          };
        }
        // Non-terminal event we don't have a dedicated branch for —
        // still bump isStreaming so the progress UI stays visible
        // through replay-resume.
        return {
          ...prev,
          research: nextResearch,
          isStreaming: baseStreaming,
        };
      });
      fireHooks(conversationId, e);
    },
    onClose: (reason) => {
      // Clear the subscription record so a fresh subscribe is
      // possible (next turn). For terminal close reasons (done/
      // error), also mark the stream finished — that prevents the
      // tab-visibility / mount loop from immediately re-subscribing
      // and getting the replayed terminal event over and over.
      subscriptions.delete(conversationId);
      if (reason === "done" || reason === "error") {
        finishedStreams.add(conversationId);
      }
    },
  });

  subscriptions.set(conversationId, unsub);
}


/**
 * Ensure every known conversation id has a live stream. Called when
 * the tab becomes visible — if the browser suspended EventSources
 * while hidden, this re-opens them so the UI hydrates via the
 * backend's replay buffer.
 */
export function reconnectAllStreams(): void {
  for (const conversationId of listeners.keys()) {
    if (!subscriptions.has(conversationId)) {
      connectResearchStream(conversationId);
    }
  }
}

// Browser suspends EventSources on some platforms when tabs go to
// background. On return, best-effort reconnect every stream that has
// live listeners but no open subscription. Safe to call repeatedly;
// connectResearchStream is a no-op if an entry already exists.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      reconnectAllStreams();
    }
  });
}

/**
 * Close the SSE stream for a conversation. Usually you don't need
 * this — streams are cheap to leave open. Call it on
 * deleteConversation so we don't leak.
 */
export function disconnectResearchStream(conversationId: string): void {
  const unsub = subscriptions.get(conversationId);
  if (unsub) {
    unsub();
    subscriptions.delete(conversationId);
  }
  store.delete(conversationId);
  listeners.delete(conversationId);
  hooks.delete(conversationId);
  finishedStreams.delete(conversationId);
  statusProbed.delete(conversationId);
}

/** Caller-visible submit flag — flips on send, flips off on
 * assistant_message/error arrival. Independent of SSE events so the
 * UI can set it before the first delta arrives.
 *
 * Watchdog: when set to `true` we also start a
 * silent-stream watchdog. If 6 minutes pass with NO new SSE event
 * (even a `status` ping), we force `isStreaming = false` + emit a
 * synthetic toast so the UI doesn't hang forever on a dead stream.
 * The backend has its own 5-minute timeout in conversations.py; the
 * client gives one extra minute of grace before giving up.
 */
const watchdogTimers = new Map<string, ReturnType<typeof setTimeout>>();
// Two-stage watchdog:
// Stage 1 (2 min): show "Connection idle" status — backend may still be alive.
// Stage 2 (additional 3 min): give up, reset isStreaming so the user can retry
// instead of being stuck on "researching" forever with no way to recover.
// The backend sends synthesis_heartbeat every 10s, so stage 1 only fires
// when truly idle (SSE dropped or backend died before synthesis).
const WATCHDOG_MS = 2 * 60 * 1000;         // stage 1: show idle warning
const WATCHDOG_GIVE_UP_MS = 3 * 60 * 1000; // stage 2: reset isStreaming
const giveUpTimers = new Map<string, ReturnType<typeof setTimeout>>();

function clearWatchdog(conversationId: string): void {
  const t = watchdogTimers.get(conversationId);
  if (t) { clearTimeout(t); watchdogTimers.delete(conversationId); }
  const g = giveUpTimers.get(conversationId);
  if (g) { clearTimeout(g); giveUpTimers.delete(conversationId); }
}

function armWatchdog(conversationId: string): void {
  clearWatchdog(conversationId);
  // Stage 1: show idle warning after 2 min of silence.
  const t = setTimeout(() => {
    watchdogTimers.delete(conversationId);
    update(conversationId, (s) => {
      if (!s.isStreaming) return s;
      return { ...s, status: "Connection idle — waiting for backend..." };
    });
    // Stage 2: after a further 3 min with still no events, give up and
    // reset isStreaming so the user isn't permanently stuck on a frozen
    // "researching" screen with no way to retry.
    const g = setTimeout(() => {
      giveUpTimers.delete(conversationId);
      update(conversationId, (s) => {
        if (!s.isStreaming) return s;
        return {
          ...s,
          isStreaming: false,
          errorMessage:
            "Research timed out — the backend stopped responding. " +
            "Send your question again to retry.",
        };
      });
    }, WATCHDOG_GIVE_UP_MS);
    giveUpTimers.set(conversationId, g);
  }, WATCHDOG_MS);
  watchdogTimers.set(conversationId, t);
}

/**
 * Re-arm the watchdog whenever a non-terminal event arrives. Unlike
 * the prior ``rearmWatchdogIfActive`` (which no-op'd if the timer
 * had already fired), this version always rearms when the
 * conversation is still streaming, so a late delta after a stall
 * brings the watchdog back from the dead.
 */
function rearmWatchdog(conversationId: string): void {
  const s = store.get(conversationId);
  if (!s || !s.isStreaming) return;
  armWatchdog(conversationId);
}

export function markSubmitting(conversationId: string, submitting: boolean): void {
  update(conversationId, (s) => {
    if (submitting) {
      // When a new turn starts, reset the per-turn
      // research state so the activity log + progress widget
      // reflect ONLY the current turn. Keep the text + tools
      // (those track streaming output, not pipeline state).
      return {
        ...EMPTY,
        isStreaming: true,
      };
    }
    return {
      ...s,
      isStreaming: false,
      errorMessage: s.errorMessage,
    };
  });
  if (submitting) {
    // Fresh turn — clear the "stream already finished" guard so
    // connectResearchStream actually opens a new EventSource for
    // the new run instead of staying short-circuited.
    finishedStreams.delete(conversationId);
    // Re-arm the status probe so the next reconnect (after the
    // fresh stream eventually closes) can re-detect any further
    // in-flight runs.
    statusProbed.delete(conversationId);
    armWatchdog(conversationId);
    // If the EventSource was previously closed (e.g., a prior turn
    // finished), reopen it now so the new turn's events stream in.
    connectResearchStream(conversationId);
  } else {
    clearWatchdog(conversationId);
  }
}

/** Reset everything for a conversation (e.g., on thread switch
 * within the same tab — rare). */
export function resetLive(conversationId: string): void {
  store.set(conversationId, EMPTY);
  emit(conversationId);
}

/** React hook: subscribe to live state for one conversation.
 * Automatically opens the SSE stream on first subscribe. */
export function useLiveResearch(
  conversationId: string | null,
): LiveResearchState {
  return useSyncExternalStore(
    (listener) => {
      if (!conversationId) return () => {};
      let arr = listeners.get(conversationId);
      if (!arr) {
        arr = new Set();
        listeners.set(conversationId, arr);
      }
      arr.add(listener);
      connectResearchStream(conversationId);
      return () => {
        const set = listeners.get(conversationId);
        if (!set) return;
        set.delete(listener);
        // FIX 3: Disconnect old stream immediately when the last listener
        // unmounts UNLESS a run is actively streaming. This prevents
        // conversation switches from holding open an EventSource (which
        // consumes one of the browser's 6 connections-per-origin) for
        // conversations the user is no longer viewing.
        //
        // Previously we only disconnected when both listeners AND
        // side-effect hooks were empty. That design kept idle streams alive
        // unnecessarily. Now: disconnect on zero listeners whenever the
        // stream is quiescent. A mid-research tab switch still keeps the
        // stream open because isStreaming=true.
        if (set.size === 0) {
          listeners.delete(conversationId);
          const state = store.get(conversationId);
          // Keep stream open only if actively streaming (user is mid-research
          // and switched tabs — dropping the connection would lose events).
          if (!state?.isStreaming) {
            // Clear stale streaming text/tools/status before fully
            // disconnecting — prevents them from bleeding into the
            // next time the user navigates back to this conversation.
            update(conversationId, (s) => ({ ...s, text: "", tools: [], status: "" }));
            disconnectResearchStream(conversationId);
            finishedStreams.delete(conversationId);
            statusProbed.delete(conversationId);
          }
        }
      };
    },
    () => (conversationId ? getSnapshot(conversationId) : EMPTY),
    () => (conversationId ? getSnapshot(conversationId) : EMPTY),
  );
}

/** Register a side-effect callback on SSE events (e.g., invalidate
 * react-query caches on `assistant_message`). Returns an unsubscribe
 * function. Stream stays open regardless. */
export function subscribeResearchSideEffect(
  conversationId: string,
  hook: MessageHook,
): () => void {
  let arr = hooks.get(conversationId);
  if (!arr) {
    arr = new Set();
    hooks.set(conversationId, arr);
  }
  arr.add(hook);
  // Ensure the stream is open so hooks actually fire
  connectResearchStream(conversationId);
  return () => {
    const set = hooks.get(conversationId);
    if (set) set.delete(hook);
  };
}
