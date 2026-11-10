/**
 * SSE client for live task progress streaming.
 *
 * Each subscribe returns a disposer. The stream is considered dead when:
 *   - caller calls the disposer
 *   - server sends a terminal event (``done``/``error``)
 *   - the underlying EventSource hits ``onerror`` with readyState=CLOSED
 *
 * An optional ``onClose`` callback fires exactly once in any of these
 * cases, so the research-store can clear its subscriptions map and
 * trigger a fresh reconnect on the next subscribe call (which in turn
 * lets the server's replay buffer hydrate state).
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export interface SSEEvent {
  event_type: string;
  data: Record<string, unknown>;
}

type EventHandler = (event: SSEEvent) => void;
type ErrorHandler = (error: Event) => void;
type CloseHandler = (reason: "done" | "error" | "dispose" | "server_closed") => void;

const MAX_RETRIES = 5;
const BASE_DELAY_MS = 1000;

export interface ConversationStreamOptions {
  onEvent: EventHandler;
  onError?: ErrorHandler;
  onClose?: CloseHandler;
}

export function subscribeToConversationStream(
  conversationId: string,
  onEventOrOptions: EventHandler | ConversationStreamOptions,
  onErrorMaybe?: ErrorHandler,
): () => void {
  // Back-compat: old call shape is (id, onEvent, onError). New shape
  // passes an options object so we can also wire onClose without
  // breaking every existing call site.
  const opts: ConversationStreamOptions = typeof onEventOrOptions === "function"
    ? { onEvent: onEventOrOptions, onError: onErrorMaybe }
    : onEventOrOptions;

  let source: EventSource | null = null;
  let closed = false;
  // Track every handler we register so fireClose can remove them all,
  // preventing listener accumulation across reconnects.
  const handlers = new Map<string, EventListener>();
  const fireClose = (reason: Parameters<CloseHandler>[0]) => {
    if (closed) return;
    closed = true;
    try {
      if (source) {
        handlers.forEach((fn, type) => source!.removeEventListener(type, fn));
        handlers.clear();
        source.close();
      }
    } finally {
      opts.onClose?.(reason);
    }
  };

  source = new EventSource(`${BASE_URL}/conversations/${conversationId}/stream`);

  for (const eventType of [
    "status",
    "token",
    "tool_use",
    "sources_partial",
    "assistant_message",
    "error",
    "done",
    // Research coordinator events — emitted during multi-repo fan-out
    "plan",            // Orchestrator's intent + reasoning + resolved walk
    "scope",
    "freshness",
    "staleness_warning",
    "route",
    "repo_roster",     // one per selected repo with enabled-agents map
    "agent_start",     // per-repo × per-agent-type
    "agent_done",
    "catalog_expansion", // "finding N more domain repos in the org catalog"
    "auto_registered",   // "registered X on the fly, will research it"
    "subagent_start",  // legacy shims (back-compat, safe to ignore)
    "subagent_done",
    "external_start",
    "external_done",
    "synthesis_delta",
    "synthesis_heartbeat",  // 10s tick during synthesis stream so the
                             // frontend watchdog rearms even if Claude
                             // is "thinking" between tokens.
  ]) {
    const h: EventListener = (msg) => {
      try {
        opts.onEvent({
          event_type: eventType,
          data: JSON.parse((msg as MessageEvent).data),
        });
      } catch {
        /* ignore */
      }
      if (eventType === "done") {
        fireClose("done");
      } else if (eventType === "error") {
        fireClose("error");
      }
    };
    handlers.set(eventType, h);
    source.addEventListener(eventType, h);
  }

  source.onerror = (err) => {
    // readyState 2 == CLOSED. Browsers also fire onerror during
    // reconnect attempts (readyState 0 == CONNECTING). We only
    // consider the stream dead when it's fully closed.
    if (closed) return;
    if (source && source.readyState === EventSource.CLOSED) {
      opts.onError?.(err);
      fireClose("server_closed");
    } else {
      // Transient — EventSource will auto-retry. Forward so callers
      // who care (analytics, status UI) can log it, but don't close.
      opts.onError?.(err);
    }
  };

  return () => {
    fireClose("dispose");
  };
}

export function subscribeToTaskStream(
  taskId: string,
  onEvent: EventHandler,
  onError?: ErrorHandler,
): () => void {
  let retries = 0;
  let source: EventSource | null = null;
  let closed = false;

  function connect() {
    if (closed) return;

    source = new EventSource(`${BASE_URL}/tasks/${taskId}/stream`);

    source.onopen = () => {
      retries = 0;
    };

    source.onmessage = (msg) => {
      try {
        const parsed: SSEEvent = {
          event_type: "message",
          data: JSON.parse(msg.data),
        };
        onEvent(parsed);
      } catch {
        // Ignore unparseable messages
      }
    };

    // Listen for typed events (extraction_progress, task_complete, etc.)
    for (const eventType of [
      "extraction_progress",
      "task_complete",
      "task_failed",
      "phase_complete",
      "cost_update",
      "heartbeat",
    ]) {
      source.addEventListener(eventType, (msg) => {
        try {
          onEvent({
            event_type: eventType,
            data: JSON.parse((msg as MessageEvent).data),
          });
        } catch {
          // Ignore unparseable
        }

        if (eventType === "task_complete" || eventType === "task_failed") {
          cleanup();
        }
      });
    }

    source.onerror = (err) => {
      if (closed) return;

      source?.close();
      source = null;

      if (retries < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, retries);
        retries++;
        setTimeout(connect, delay);
      } else {
        onError?.(err);
      }
    };
  }

  function cleanup() {
    closed = true;
    source?.close();
    source = null;
  }

  connect();
  return cleanup;
}

/**
 * Subscribe to live events for a collaboration session.
 *
 * Server emits ``session_updated`` (refetch the session), plus
 * ``agent_running`` / ``agent_done`` around clarifier + synthesizer
 * LLM calls (drives the typing indicator). Same auto-reconnect-with-
 * replay-buffer machinery as the task stream — caller doesn't need
 * to handle reconnects manually.
 */
export function subscribeToCollabStream(
  sessionId: string,
  onEvent: EventHandler,
  onError?: ErrorHandler,
): () => void {
  let retries = 0;
  let source: EventSource | null = null;
  let closed = false;

  function connect() {
    if (closed) return;

    source = new EventSource(`${BASE_URL}/collab-sessions/${sessionId}/stream`);

    source.onopen = () => {
      retries = 0;
    };

    // The collab event bus emits typed events (event_type set), so
    // ``onmessage`` is unused — we only listen for the specific names.
    for (const eventType of [
      "session_updated",
      "agent_running",
      "agent_done",
      "heartbeat",
    ]) {
      source.addEventListener(eventType, (msg) => {
        try {
          onEvent({
            event_type: eventType,
            data: JSON.parse((msg as MessageEvent).data),
          });
        } catch {
          // Ignore unparseable
        }
      });
    }

    source.onerror = (err) => {
      if (closed) return;

      source?.close();
      source = null;

      if (retries < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, retries);
        retries++;
        setTimeout(connect, delay);
      } else {
        onError?.(err);
      }
    };
  }

  function cleanup() {
    closed = true;
    source?.close();
    source = null;
  }

  connect();
  return cleanup;
}
