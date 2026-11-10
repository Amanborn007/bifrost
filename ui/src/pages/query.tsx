import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import {
  ArrowUp,
  Loader2,
  Plus,
  Pencil,
  Trash2,
  Check,
  X,
  AlertTriangle,
  Copy,
  RefreshCcw,
  User,
  Sparkles,
  ExternalLink,
  ChevronDown,
  ClipboardList,
  ThumbsUp,
  ThumbsDown,
  Zap,
  Wand2,
  Circle,
  Cable,
  Users,
  Hash,
  Clock,
  Database,
} from "lucide-react";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";
import { Prose } from "@/components/prose";
import {
  listRepos,
  listConversations,
  listArchivedConversations,
  getConversation,
  createConversation,
  renameConversation,
  deleteConversation,
  restoreConversation,
  cancelConversation,
  appendMessage,
  submitMessageFeedback,
  spawnTaskFromChat,
  getConversationTasks,
  classifyIntent,
  runQuick,
  lookupOwnership,
  isOwnershipQuestion,
  ApiError,
  type ConversationSummary,
  type ConversationMessage,
  type ConversationDetail,
  type ConvTaskLink,
  type IntentClassification,
  type LastResearchSummary,
  type OwnershipResult,
} from "@/lib/api";
import { ChatSettingsPanel } from "@/components/chat-settings-panel";
import { SpawnTaskModal } from "@/components/spawn-task-modal";
import { ChatTaskLineageCard } from "@/components/chat-task-lineage";
import { ChatConnectionsPanel } from "@/components/chat-connections-panel";
import { ConfidenceBadge } from "@/components/confidence-badge";
import { ChatActionsBar } from "@/components/chat-actions-panel";
import {
  ResearchProgress,
  type ResearchState,
} from "@/components/research-progress";
import {
  useLiveResearch,
  markSubmitting,
  subscribeResearchSideEffect,
  disconnectResearchStream,
} from "@/lib/research-store";
import { useNavigate, useSearchParams } from "react-router-dom";

/**
 * Query — a real chat product.
 *
 * Left: thread list (persistent, named, resumable).
 * Right: turn-by-turn conversation with user + assistant messages, inline source citations,
 *         composer pinned to the bottom.
 *
 * Backend: POST /conversations/{id}/messages returns a stream URL; the assistant message
 * arrives over SSE as the agent finishes reasoning. Turns persist to Postgres.
 */
export function QueryPage() {
  const qc = useQueryClient();

  // ── State ─────────────────────────────────────────────────
  const [searchParams, setSearchParams] = useSearchParams();
  // ?prefill=<text> seeds the composer; ?scope=<repo_id> pre-selects
  // the repo scope. Both are consumed on mount and cleared from the
  // URL so back-navigation doesn't re-trigger them.
  const prefillParam = searchParams.get("prefill");
  const scopeParam = searchParams.get("scope");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [composer, setComposer] = useState(() => prefillParam ?? "");
  const [selectedRepo, setSelectedRepo] = useState<string>(() => scopeParam ?? "all");
  const [threadSearch, setThreadSearch] = useState("");
  // Optimistic user bubble: set immediately when onSend fires so the
  // selected suggestion appears in the thread before the server
  // refetch returns the persisted message. Cleared once messages updates.
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null);
  // Right-rail connections panel — shown by default so the operator
  // sees the work-graph spine alongside the conversation. Toggleable
  // via the "Connections" button in the chat header.
  const [connectionsOpen, setConnectionsOpen] = useState(true);
  // Per-message intent classifications. When a user message scores
  // ``quick`` / ``execute`` we surface an inline suggestion chip so
  // they can hand off to the action layer without leaving the chat.
  const [intentByMessage, setIntentByMessage] = useState<
    Record<string, IntentClassification>
  >({});
  const pageNavigate = useNavigate();

  // Live research state lives in a module-level store keyed by
  // conversation id — survives tab switches / page unmounts. The SSE
  // connection is owned by the store, not by this component, so
  // navigating to Spend and coming back doesn't drop in-progress
  // research.
  const live = useLiveResearch(activeId);
  const streamingStatus = live.status;
  const streamingText = live.text;
  const streamingTools = live.tools;
  const research: ResearchState = live.research;
  const submitting = live.isStreaming;
  const streamingError = live.errorMessage;
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const newChatRef = useRef<(() => void) | null>(null);

  // ── Data ──────────────────────────────────────────────────
  const repos = useQuery({ queryKey: ["repos"], queryFn: listRepos });
  const conversations = useQuery({
    queryKey: ["conversations"],
    queryFn: listConversations,
    refetchInterval: 60_000,  // 60s — list changes infrequently; was 30s
  });
  const active = useQuery<ConversationDetail>({
    queryKey: ["conversation", activeId],
    queryFn: () => getConversation(activeId!),
    enabled: !!activeId,
    refetchOnWindowFocus: false,  // don't re-fetch on alt-tab; SSE keeps data live
  });

  // FIX 1: Cancel stale in-flight queries for the OLD conversation when the
  // user switches conversations. Without this, the browser's 6-connection-
  // per-origin limit can be exhausted by a queue of pending fetches for
  // conversations the user is no longer viewing.
  const prevActiveIdRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevActiveIdRef.current;
    prevActiveIdRef.current = activeId;
    if (prev && prev !== activeId) {
      // Cancel pending fetches for old conversation so they don't hold a connection slot.
      void qc.cancelQueries({ queryKey: ["conversation", prev] });
      void qc.cancelQueries({ queryKey: ["conv-connections", prev] });
    }
  }, [activeId, qc]);

  // BUG 2 fix (intent-state-leaks): clear per-message intent classifications
  // whenever the active conversation changes. Without this, stale intent chips
  // from the previous thread leak into the new one until new messages arrive.
  useEffect(() => {
    setIntentByMessage({});
  }, [activeId]);

  // Clear the optimistic bubble once the real message lands from the server.
  // BUG 1 fix (pending-msg-race): use identity-based clearing instead of
  // content-equality — any new user message arriving with an id clears the
  // bubble regardless of whitespace normalization by the server.
  const activeMessages = active.data?.messages ?? [];
  useEffect(() => {
    if (pendingUserMessage && activeMessages.length > 0) {
      const last = activeMessages[activeMessages.length - 1];
      if (last.role === "user" && last.id !== undefined) {
        setPendingUserMessage(null);
      }
    }
  }, [activeMessages, pendingUserMessage]);

  // When a new user message lands, classify its intent once so we can
  // offer inline "Run as quick task" / "Create task" suggestions.
  // Only the latest unclassified user message is sent — avoids spamming
  // Haiku on every conversation refetch.
  useEffect(() => {
    const msgs = active.data?.messages ?? [];
    // Find the last user message that doesn't have an intent yet.
    let target: ConversationMessage | null = null;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "user") {
        if (!intentByMessage[msgs[i].id]) target = msgs[i];
        break;
      }
    }
    if (!target) return;
    const id = target.id;
    const content = target.content;
    let cancelled = false;
    (async () => {
      try {
        const result = await classifyIntent(content);
        if (cancelled) return;
        setIntentByMessage((prev) =>
          prev[id] ? prev : { ...prev, [id]: result },
        );
      } catch {
        // Silent — intent hints are an enhancement, not critical path.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [active.data?.messages, intentByMessage]);

  // Keep ``?conv=<id>`` in the URL synced with the
  // active conversation. The two effects below are intentionally
  // narrow on their dep arrays — the prior version listed
  // ``searchParams`` (a fresh object every render from
  // ``useSearchParams``) which made BOTH effects re-evaluate every
  // render, producing visible flickering as the URL re-wrote on
  // each frame. Now Effect 1 fires only when the URL's ``conv``
  // value changes; Effect 2 only when ``activeId`` does. Steady
  // state: no work, no flicker.
  const convParam = searchParams.get("conv");

  // URL → state (back/forward, deep link, copy-paste).
  useEffect(() => {
    if (convParam && convParam !== activeId) {
      setActiveId(convParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convParam]);

  // BUG 4 fix (prefill-param-on-error): only clear ?prefill / ?scope once
  // activeId is set — i.e. after conversation creation succeeds. The old
  // empty-dep [] version cleared on mount even when createConversation failed,
  // losing the prefill text and preventing a retry via back-navigation.
  useEffect(() => {
    if (!activeId) return;
    if (!prefillParam && !scopeParam) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("prefill");
        next.delete("scope");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // state → URL (thread switch, new chat, delete). Read the current
  // URL via the functional setter so we don't need ``searchParams``
  // in the dep array.
  useEffect(() => {
    setSearchParams(
      (prev) => {
        const current = prev.get("conv");
        if (activeId === current) return prev;
        const next = new URLSearchParams(prev);
        if (activeId) next.set("conv", activeId);
        else next.delete("conv");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // ── Auto-select first conversation on load ────────────────
  useEffect(() => {
    if (!activeId && conversations.data && conversations.data.length > 0) {
      setActiveId(conversations.data[0].id);
    }
  }, [activeId, conversations.data]);

  // ── Side-effect hook for SSE events ───────────────────────
  // The store handles message-stream state accumulation. This hook
  // reacts to finalization events. Invalidate caches on BOTH
  // ``assistant_message`` AND ``done`` — if the publish of the
  // assistant_message frame failed (process restart between commit
  // and publish, network hiccup, asyncio cancellation), the UI
  // would otherwise hang forever waiting for a frame that's already
  // a row in the DB. ``done`` ALWAYS fires (from the worker's
  // ``finally`` block), so it's the safer trigger.
  useEffect(() => {
    if (!activeId) return;
    return subscribeResearchSideEffect(activeId, (e) => {
      if (e.event_type === "assistant_message" || e.event_type === "done") {
        qc.invalidateQueries({ queryKey: ["conversation", activeId] });
        qc.invalidateQueries({ queryKey: ["conversations"] });
      } else if (e.event_type === "error") {
        // Stable id so sonner dedupes — the coordinator can yield
        // multiple error events in one turn (e.g. fan-out fails on
        // N agents). Without dedup, each becomes a separate toast
        // and the bottom-right corner stacks dozens of dialogs.
        const errMsg = (e.data.error as string) ?? "Query failed";
        // Cancellation isn't a failure — show as info, not error.
        if (errMsg.toLowerCase().startsWith("cancelled")) {
          toast.info("Cancelled", { id: `research-error-${activeId}` });
        } else {
          toast.error(errMsg, { id: `research-error-${activeId}` });
        }
      }
    });
  }, [activeId, qc]);

  // ── Actions ───────────────────────────────────────────────
  const onNewChat = useCallback(async () => {
    try {
      const scope =
        selectedRepo !== "all" ? [selectedRepo] : undefined;
      const { id } = await createConversation({ scope_repo_ids: scope });
      setActiveId(id);
      qc.invalidateQueries({ queryKey: ["conversations"] });
    } catch (err) {
      toast.error(
        `Couldn't start a new chat: ${describeError(err)}`,
      );
    }
  }, [qc, selectedRepo]);

  useEffect(() => {
    newChatRef.current = onNewChat;
  }, [onNewChat]);

  // Cmd/Ctrl+K focuses composer; Cmd/Ctrl+Shift+K focuses thread search;
  // Cmd/Ctrl+Shift+N starts a new chat.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        if (e.shiftKey) searchRef.current?.focus();
        else composerRef.current?.focus();
      } else if ((e.key === "n" || e.key === "N") && e.shiftKey) {
        e.preventDefault();
        newChatRef.current?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Synchronous guard against rapid double-sends. `submitting` comes
  // from a React state read; a double-tap on Enter can see the stale
  // value on both invocations before React re-renders, so both call
  // appendMessage → two user rows in the DB. This ref flips
  // atomically so only the first onSend call proceeds past the guard.
  const sendInFlightRef = useRef(false);
  // BUG 1 fix (pending-msg-race): timestamp set at the top of onSend;
  // used to identify the send epoch so we clear the bubble by message
  // identity (id !== undefined) rather than content equality, avoiding
  // whitespace-normalization mismatches from the server.
  const sendStartRef = useRef<number>(0);

  // Idempotency_key per turn — generated on submit, persisted in
  // localStorage so a page-refresh-mid-stream retry can reuse it
  // (Phase 3 per-agent cache reads from the same key to skip
  // already-completed agents). Storage shape:
  //   bifrost.idem.<convId> = "<uuid>"
  const _idemStorageKey = (convId: string) => `bifrost.idem.${convId}`;
  const generateIdempotencyKey = useCallback(
    (convId: string): string => {
      const k = (window.crypto?.randomUUID?.() ?? `idk-${Date.now()}-${Math.random()}`);
      try {
        window.localStorage.setItem(_idemStorageKey(convId), k);
      } catch { /* private mode etc. — non-fatal */ }
      return k;
    },
    [],
  );
  const lastIdempotencyKey = useCallback(
    (convId: string): string | null => {
      try {
        return window.localStorage.getItem(_idemStorageKey(convId));
      } catch {
        return null;
      }
    },
    [],
  );

  const onSend = useCallback(
    async (override?: string) => {
      const text = (override ?? composer).trim();
      if (!text || submitting) return;
      if (sendInFlightRef.current) return;
      sendInFlightRef.current = true;
      // BUG 1 fix (pending-msg-race): stamp the send epoch so the clear
      // condition can use identity (last.id !== undefined) not content equality.
      sendStartRef.current = Date.now();
      // Optimistic bubble: show the message immediately so the user sees
      // their selected suggestion in the thread before the refetch lands.
      setPendingUserMessage(text);
      let convId = activeId;
      // Preserve the composer text until we KNOW the message
      // was persisted server-side. The previous order cleared the
      // composer on the optimistic-success path then had no way to
      // restore it on a 5xx failure — the user lost their question.
      const composerSnapshot = composer;
      try {
        if (!convId) {
          const scope =
            selectedRepo !== "all" ? [selectedRepo] : undefined;
          const { id } = await createConversation({ scope_repo_ids: scope });
          convId = id;
          setActiveId(id);
        }
        markSubmitting(convId!, true);
        const idempotencyKey = generateIdempotencyKey(convId!);
        await appendMessage(convId!, text, { idempotencyKey });
        // Server accepted — safe to clear composer now.
        if (!override) setComposer("");
        qc.invalidateQueries({ queryKey: ["conversation", convId] });
      } catch (err) {
        // HTTP 409 "query_in_flight" means the server already has a
        // research task running for this conversation — another tab
        // or a fast double-send raced us. Silently clear the composer
        // (the message the user typed is already persisted as the
        // first sender's POST) and let the SSE stream hydrate the UI.
        // The replay buffer catches us up on events we missed.
        if (
          err instanceof ApiError &&
          err.status === 409 &&
          typeof err.detail === "object" &&
          err.detail !== null &&
          (err.detail as { code?: string }).code === "query_in_flight"
        ) {
          if (!override) setComposer("");
          if (convId) {
            markSubmitting(convId, true);
            qc.invalidateQueries({ queryKey: ["conversation", convId] });
          }
        } else {
          // Real failure — restore the composer to what the user
          // typed so they can retry without re-typing.
          if (!override) setComposer(composerSnapshot);
          toast.error(
            `Failed: ${describeError(err)}. Your message is still in the box — press send to retry.`,
          );
          if (convId) markSubmitting(convId, false);
        }
      } finally {
        sendInFlightRef.current = false;
      }
    },
    [activeId, composer, qc, selectedRepo, submitting, generateIdempotencyKey],
  );

  const onRunQuickFromChat = useCallback(
    async (
      _messageId: string,
      content: string,
      intent: IntentClassification | undefined,
    ) => {
      // Prompt for target repo once — most chats happen against a
      // specific repo/domain scope but Bifrost doesn't know the local
      // checkout path, so we ask inline.
      const savedPath =
        (typeof window !== "undefined" &&
          window.localStorage.getItem("bifrost.quickTargetRepo")) ||
        "";
      const path = window.prompt(
        "Target repo path for quick mode (absolute path of a git repo):",
        savedPath,
      );
      // `null` means the user pressed Cancel — give explicit feedback so
      // they know the action was not submitted (empty string means they
      // cleared the field and clicked OK, which is also a no-op).
      if (path === null) {
        toast.info("Quick mode cancelled.");
        return;
      }
      if (!path.trim()) return;
      const trimmed = path.trim();
      try {
        window.localStorage?.setItem("bifrost.quickTargetRepo", trimmed);
      } catch {
        // ignore quota errors
      }
      const title = content.split("\n")[0].slice(0, 120);
      try {
        toast.info("Quick mode running — this takes ~45–90s");
        const r = await runQuick({
          message: content,
          target_repo_path: trimmed,
          title: title || null,
          jira_key: intent?.jira_key ?? null,
        });
        if (r.fallback_to_full) {
          toast.info(
            "Ambiguous for quick mode — use 'Create task' instead",
          );
          return;
        }
        toast.success(
          `Quick done in ${(r.duration_ms / 1000).toFixed(1)}s · ` +
            `${r.plan_status}${r.ready_for_pr ? " · ready for PR" : ""}`,
        );
        if (r.task_id) pageNavigate(`/tasks/${r.task_id}`);
      } catch (err) {
        toast.error(
          `Quick failed: ${describeError(err)}`,
        );
      }
    },
    [pageNavigate],
  );

  // The SDLC flow replaces the silent spawn with an
  // opt-in modal that previews title / project / acceptance criteria
  // and writes to Jira ONLY when the operator clicks "Create + go".
  // The modal is the single mutation point — every Jira write is
  // logged in the task's audit trail.
  const [spawnModalState, setSpawnModalState] = useState<
    { messageId: string | null } | null
  >(null);
  const onSpawnTaskFromChat = useCallback(
    // content + intent are part of the onSpawnTask contract but the modal
    // only needs the messageId; omitting the unused trailing params keeps
    // the callback type-compatible (a fn with fewer args satisfies more).
    (messageId: string) => {
      if (!activeId) return;
      setSpawnModalState({ messageId });
    },
    [activeId],
  );

  // Undo on delete. Soft-deletes already preserve the row +
  // history, so "undo" is just a restore call. Surface it as a
  // sonner toast action so the user can recover from a misclick
  // without hunting through an archive view.
  const onDeleteConversation = useCallback(
    async (id: string, title?: string) => {
      try {
        // BUG 3 fix (orphaned-active-conv-after-delete): clear activeId BEFORE
        // the async delete so back-navigation during the request window doesn't
        // land on a blank/stale conversation view.
        if (activeId === id) setActiveId(null);
        await deleteConversation(id);
        disconnectResearchStream(id);
        qc.invalidateQueries({ queryKey: ["conversations"] });
        toast.success(
          title ? `Hid "${title}"` : "Hid conversation",
          {
            description:
              "Moved to archive — you can restore it for 90 days.",
            action: {
              label: "Undo",
              onClick: async () => {
                try {
                  await restoreConversation(id);
                  // refetchQueries forces an immediate network request so
                  // the restored conversation appears right away, rather
                  // than waiting for the next background poll interval.
                  // invalidateQueries alone only marks stale — the actual
                  // refetch is deferred until the next render/focus event.
                  await qc.refetchQueries({ queryKey: ["conversations"] });
                  toast.success("Restored");
                } catch (err) {
                  toast.error(`Restore failed: ${describeError(err)}`);
                }
              },
            },
          },
        );
      } catch (err) {
        toast.error(`Failed: ${describeError(err)}`);
      }
    },
    [activeId, qc],
  );

  // Cancel an in-flight query mid-stream. Best-effort —
  // the server publishes a synthetic error+done so the watchdog
  // terminates immediately even if the cancel HTTP call lands after
  // the task has already finished naturally.
  const onCancelGeneration = useCallback(async () => {
    if (!activeId || !submitting) return;
    try {
      const res = await cancelConversation(activeId);
      // When the server reports
      // ``cancelled: false`` (e.g., the task already died from a
      // backend restart but the UI still thinks isStreaming=true
      // from a stale SSE replay), force-clear the local submitting
      // flag. Without this the user is locked out — Stop does
      // nothing visible and the send button stays disabled.
      if (res.cancelled === false) {
        markSubmitting(activeId, false);
        toast.info(
          "No active research found — cleared streaming state. You can send a new message.",
        );
      }
      // For the cancelled=true path, the SSE error+done events
      // drive the local state reset (preserves any partial
      // streaming text up to the cancel).
    } catch (err) {
      toast.error(`Couldn't cancel: ${describeError(err)}`);
      // Still force-clear so the user isn't permanently stuck on a
      // network error mid-cancel.
      markSubmitting(activeId, false);
    }
  }, [activeId, submitting]);

  // Archived list. Loaded lazily when the user toggles the
  // "Archived" view in the sidebar. Held in component-local state
  // (rather than react-query) because we render archived items with
  // a different action set; mixing them into the main query would
  // muddy the cache key.
  const [showArchived, setShowArchived] = useState(false);
  const archived = useQuery({
    queryKey: ["archived-conversations"],
    queryFn: listArchivedConversations,
    enabled: showArchived,
    refetchOnWindowFocus: false,
  });
  const onRestore = useCallback(
    async (id: string) => {
      try {
        await restoreConversation(id);
        qc.invalidateQueries({ queryKey: ["conversations"] });
        qc.invalidateQueries({ queryKey: ["archived-conversations"] });
        toast.success("Restored");
        setShowArchived(false);
        setActiveId(id);
      } catch (err) {
        toast.error(`Restore failed: ${describeError(err)}`);
      }
    },
    [qc],
  );

  const onRename = useCallback(
    async (id: string, title: string) => {
      try {
        await renameConversation(id, title);
        qc.invalidateQueries({ queryKey: ["conversations"] });
        qc.invalidateQueries({ queryKey: ["conversation", id] });
      } catch (err) {
        toast.error(`Failed: ${describeError(err)}`);
      }
    },
    [qc],
  );

  const conversationList = useMemo(
    () => conversations.data ?? [],
    [conversations.data],
  );
  const repoOptions = repos.data ?? [];

  // Client-side thread search — match title OR last message preview
  const filteredConversations = useMemo(() => {
    const q = threadSearch.trim().toLowerCase();
    if (!q) return conversationList;
    return conversationList.filter(
      (c) =>
        c.title.toLowerCase().includes(q) ||
        (c.last_message_preview?.toLowerCase().includes(q) ?? false),
    );
  }, [conversationList, threadSearch]);

  // Regenerate the last assistant message: replay the last user message
  const onRegenerate = useCallback(async () => {
    if (!activeId || submitting) return;
    const msgs = active.data?.messages ?? [];
    const lastUser = [...msgs].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    try {
      markSubmitting(activeId, true);
      // Reuse the prior turn's idempotency_key so the cache layer
      // (Phase 3) can return the cached agents instead of re-running
      // them. If the key is gone (cleared localStorage, different
      // browser), fall back to a fresh key — server still spawns a
      // fresh run, same as before.
      const idempotencyKey =
        lastIdempotencyKey(activeId) ?? generateIdempotencyKey(activeId);
      await appendMessage(activeId, lastUser.content, { idempotencyKey });
      qc.invalidateQueries({ queryKey: ["conversation", activeId] });
    } catch (err) {
      toast.error(
        `Regenerate failed: ${describeError(err)}`,
      );
      markSubmitting(activeId, false);
    }
  }, [activeId, active.data, qc, submitting, lastIdempotencyKey, generateIdempotencyKey]);

  // Stable callback so MessageList memoization works — passing an
  // inline arrow here re-creates the prop on every parent render and
  // defeats React.memo on AssistantMessage.
  const onFollowUp = useCallback(
    (q: string) => {
      // Re-submit the chip as a fresh user turn in the same
      // conversation (don't just prefill the composer — clicking an
      // "Ask next" chip is an explicit ask). ``onSend(override)``
      // reuses the full submit path (idempotency key, persistence,
      // SSE stream) without touching the composer text.
      if (submitting) {
        // A turn is already streaming — fall back to prefilling so the
        // click isn't silently dropped by onSend's submitting guard.
        setComposer(q);
        composerRef.current?.focus();
        return;
      }
      void onSend(q);
    },
    [onSend, submitting],
  );

  return (
    <div className="flex h-full min-h-0">
      {/* ── Thread sidebar ─────────────────────────────── */}
      <aside className="flex w-[280px] shrink-0 flex-col border-r border-border bg-bg-1">
        <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-fg-0">Conversations</h2>
          <button
            onClick={onNewChat}
            className="inline-flex items-center gap-1 rounded-md bg-accent px-2.5 py-1 text-[12px] font-medium text-accent-fg hover:bg-accent-hover"
            title="New chat"
          >
            <Plus className="h-3.5 w-3.5" strokeWidth={2} />
            New
          </button>
        </div>

        {/* Repo scope */}
        <div className="border-b border-border px-4 py-3">
          <label className="mb-1 block text-[11px] font-medium text-fg-2">
            Scope
          </label>
          <select
            value={selectedRepo}
            onChange={(e) => setSelectedRepo(e.target.value)}
            className="w-full rounded-md border border-border bg-bg-2 px-2.5 py-1.5 text-sm text-fg-0 focus:border-border-focus focus:outline-none"
          >
            <option value="all">All repositories</option>
            {repoOptions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[11px] text-fg-3">
            Applied to new conversations.
          </p>
        </div>

        {/* Thread search */}
        {conversationList.length > 0 && (
          <div className="border-b border-border px-4 py-2">
            <div className="relative">
              <svg
                className="pointer-events-none absolute left-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-fg-3"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <circle cx="11" cy="11" r="8" />
                <line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
              <input
                ref={searchRef}
                value={threadSearch}
                onChange={(e) => setThreadSearch(e.target.value)}
                placeholder="Search threads…"
                className="h-7 w-full rounded border border-border bg-bg-2 pl-7 pr-2 text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
              />
            </div>
          </div>
        )}

        {/* Threads */}
        <div className="flex-1 overflow-y-auto">
          {conversations.isLoading ? (
            <div className="p-4 text-sm text-fg-2">Loading…</div>
          ) : conversationList.length === 0 ? (
            <div className="p-4 text-sm text-fg-2">
              No conversations yet. Click <span className="font-medium">New</span>{" "}
              to start.
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className="p-4 text-sm text-fg-3">
              No threads match <span className="font-mono">"{threadSearch}"</span>.
            </div>
          ) : (
            <ul className="py-1.5">
              {filteredConversations.map((c) => (
                <ThreadItem
                  key={c.id}
                  conv={c}
                  active={c.id === activeId}
                  isStreaming={c.id === activeId && submitting}
                  onSelect={() => setActiveId(c.id)}
                  onDelete={() => onDeleteConversation(c.id, c.title)}
                  onRename={(t) => onRename(c.id, t)}
                />
              ))}
            </ul>
          )}
        </div>

        {/* Chat settings panel.
            Sits between the threads list and the archive view —
            collapsed by default; on expand exposes the per-conv
            knobs (depth / breadth / confidence floor / edge
            directions / external sources / agents-per-repo / auto
            extract). Defaults match today's hardcoded behaviour
            so nothing changes on day-one. */}
        <ChatSettingsPanel convId={activeId} />

        {/* Archive view toggle. Reveals soft-deleted threads
            so the user can restore. Hidden by default to keep the
            sidebar focused on active work. */}
        <div className="border-t border-border px-3 py-2">
          <button
            onClick={() => setShowArchived((s) => !s)}
            className="flex w-full items-center justify-between rounded px-2 py-1.5 text-[12px] text-fg-2 transition-colors hover:bg-bg-2 hover:text-fg-0"
          >
            <span className="inline-flex items-center gap-1.5">
              <Trash2 className="h-3 w-3" strokeWidth={1.75} />
              Archive
              {(archived.data?.length ?? 0) > 0 && (
                <span className="rounded bg-bg-3 px-1.5 text-[10px] text-fg-2">
                  {archived.data?.length}
                </span>
              )}
            </span>
            <ChevronDown
              className={cn(
                "h-3 w-3 transition-transform",
                showArchived && "rotate-180",
              )}
              strokeWidth={1.75}
            />
          </button>
          {showArchived && (
            <div className="mt-1 max-h-[200px] overflow-y-auto">
              {archived.isLoading ? (
                <p className="px-2 py-1 text-[11px] text-fg-3">Loading…</p>
              ) : (archived.data?.length ?? 0) === 0 ? (
                <p className="px-2 py-1 text-[11px] text-fg-3">
                  No archived conversations.
                </p>
              ) : (
                <ul className="space-y-0.5">
                  {archived.data!
                    .filter((c) => c.archived)
                    .map((c) => (
                      <li
                        key={c.id}
                        className="group flex items-center justify-between gap-1 rounded px-2 py-1 text-[12px] text-fg-1 hover:bg-bg-2"
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {c.title}
                        </span>
                        <button
                          onClick={() => onRestore(c.id)}
                          className="opacity-0 transition-opacity group-hover:opacity-100"
                          title="Restore"
                        >
                          <RefreshCcw
                            className="h-3 w-3 text-fg-2 hover:text-accent"
                            strokeWidth={1.75}
                          />
                        </button>
                      </li>
                    ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </aside>

      {/* ── Main pane ──────────────────────────────────── */}
      <section className="flex min-w-0 flex-1 flex-col">
        {/* Topbar */}
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-bg-0 px-6">
          <div className="min-w-0">
            <h1 className="truncate text-[15px] font-semibold text-fg-0">
              {active.data?.title ?? "Ask Bifrost"}
            </h1>
            <ChatScopeLine
              scopeRepoIds={active.data?.scope_repo_ids}
              lastResearch={active.data?.last_research}
            />
          </div>
          {/* Connections toggle — only meaningful once there's a
              live conversation. Hides the right rail to give the
              messages area more horizontal room when desired. */}
          {activeId && (
            <button
              type="button"
              onClick={() => setConnectionsOpen((v) => !v)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11.5px] font-medium transition-colors",
                connectionsOpen
                  ? "border-accent bg-accent-soft text-accent"
                  : "border-border bg-bg-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0",
              )}
              title="Toggle connections panel — tasks · asks · related chats"
            >
              <Cable className="h-3.5 w-3.5" strokeWidth={1.75} />
              Connections
            </button>
          )}
        </header>

        {/* Messages — will-change: transform promotes the scroll container
            to its own compositor layer, eliminating main-thread paint
            during scroll. overscroll-behavior-y: contain stops scroll
            chaining to parent frames. */}
        <div className="flex-1 overflow-y-auto overscroll-y-contain" style={{ willChange: "transform" }}>
          {!activeId ? (
            <EmptyStarterView
              repoCount={repoOptions.length}
              onAsk={(q) => {
                setComposer(q);
                onSend(q);
              }}
            />
          ) : active.isLoading && !submitting && !active.data ? (
            // Render the bare loading state ONLY when there's no
            // streaming work in flight. Fix for "no progress widget
            // appears when I send a message on a freshly-created
            // conversation": the conversation fetch can still be
            // pending while the SSE stream is already producing
            // tokens — if we hide MessageList we hide the streaming
            // progress widget too, which is exactly what the widget
            // is for. So as long as a stream is active, fall through
            // to MessageList with whatever messages are loaded
            // (empty array on first turn) and let the streaming
            // text + research progress render.
            <div className="p-8 text-sm text-fg-2">Loading conversation…</div>
          ) : (
            <MessageList
              convId={activeId}
              messages={active.data?.messages ?? []}
              conversationLoaded={!active.isLoading && !!active.data}
              pendingUserMessage={pendingUserMessage}
              streamingStatus={streamingStatus}
              streamingText={streamingText}
              streamingTools={streamingTools}
              streamingError={streamingError}
              research={research}
              submitting={submitting}
              liveFollowups={live.suggestedFollowups}
              onRegenerate={onRegenerate}
              onFollowUp={onFollowUp}
              intentByMessage={intentByMessage}
              onRunQuick={onRunQuickFromChat}
              onSpawnTask={onSpawnTaskFromChat}
            />
          )}
        </div>

        {/* "What's next?" action strip — shown after assistant replies */}
        {activeId && (
          <ChatActionsBar
            conversationId={activeId}
            messages={active.data?.messages ?? []}
            scopeRepoIds={active.data?.scope_repo_ids ?? []}
            reposUsedNames={active.data?.last_research?.repos_used_names ?? []}
            isStreaming={submitting}
          />
        )}

        {/* Composer */}
        <Composer
          textareaRef={composerRef}
          value={composer}
          onChange={setComposer}
          onSend={() => onSend()}
          onCancel={onCancelGeneration}
          submitting={submitting}
          placeholder={
            activeId
              ? "Ask a follow-up…"
              : "Ask a question about your codebase…"
          }
        />
      </section>
      {/* Connections rail — third flex sibling, only when a chat is
          active and the user hasn't dismissed it. Pulls every
          downstream object (tasks, asks, related chats) into one
          surface so the operator never has to leave the chat to see
          what got built from it. */}
      {/* FIX 4: Only mount ChatConnectionsPanel after the conversation detail
          has loaded. Previously the panel mounted immediately on conversationId
          change and fired its /connections request concurrently with the
          /conversation fetch, contending for the same connection slots.
          Gating on active.data means the connections query only enters the
          queue once the primary request has resolved and freed its slot. */}
      {active.data && connectionsOpen && (
        <ChatConnectionsPanel
          conversationId={activeId!}
          onJumpToChat={(id) => setActiveId(id)}
        />
      )}
      {/* Spawn-task modal — mounted at QueryPage level (not inside
          MessageList) so spawnModalState lives in the same scope it's
          declared in. The MessageList child only opens it via the
          onSpawnTask callback, which calls setSpawnModalState. */}
      {spawnModalState && activeId && (
        <SpawnTaskModal
          conversationId={activeId}
          messageId={spawnModalState.messageId}
          onClose={() => setSpawnModalState(null)}
          onSpawned={(task) => {
            toast.success(`Task spawned: ${task.title}`);
            pageNavigate(`/tasks/${task.id}`);
          }}
        />
      )}
    </div>
  );
}

/* ─── Chat scope line (chat header subtitle) ───────────────
 *
 * The previous subtitle rendered ``conversations.scope_repo_ids``
 * verbatim — i.e. the *input* scope set at creation time:
 *
 *   - ``[]``        → "All repositories"  (sounds reassuring; the
 *                     router actually fanned out to top-K, often
 *                     3 of ~100 — sometimes the fallback set when
 *                     routing failed)
 *   - ``[a,b,c,d]`` → "Scoped to 4 repos" (silently true even when
 *                     only 1 of the 4 ended up contributing)
 *
 * Both readings panicked operators who later saw the assistant
 * admit "all 3 routed repos are off-topic". This component prefers
 * ``last_research`` (the after-the-fact view sourced from the
 * persisted bundle) so the subtitle reflects what the system
 * actually did:
 *
 *   - "3 repos routed: name1, name2, name3"         (open scope)
 *   - "Scoped to 3 repos: name1, name2, name3"      (closed, full)
 *   - "2 of 4 repos contributed: name1, name2"      (closed, partial — amber)
 *   - "All repositories" / "Scoped to N repos"      (no turn yet)
 *
 * When ``router_fallback`` is true we append an amber badge whose
 * ``title`` carries the raw ``scope_rationale`` — visible upfront
 * that the routing is suspect, hover-discoverable for the why. */

function ChatScopeLine({
  scopeRepoIds,
  lastResearch,
}: {
  scopeRepoIds: string[] | undefined;
  lastResearch: LastResearchSummary | null | undefined;
}) {
  const scopeCount = scopeRepoIds?.length ?? 0;
  const names = lastResearch?.repos_used_names ?? [];
  const usedCount = names.length;
  const hasRouting = !!lastResearch && usedCount > 0;

  // No research turn yet — fall back to the input-scope display.
  if (!hasRouting) {
    return (
      <p className="mt-0.5 truncate text-[11px] text-fg-2">
        {scopeCount > 0
          ? `Scoped to ${scopeCount} ${scopeCount === 1 ? "repo" : "repos"}`
          : "All repositories"}
      </p>
    );
  }

  // Three repos max inline; rest as a "+N" chip. Long org repos like
  // ``foo-bar-baz-svc`` blow past the truncate threshold if we list
  // more than a handful, and the user can hover/click the chip for
  // the full set.
  const visible = names.slice(0, 3);
  const overflow = usedCount - visible.length;

  // Closed scope with silent drops — amber. The user thought 4 repos
  // were going to answer; only 2 actually did.
  const partial = scopeCount > 0 && usedCount < scopeCount;

  const headlinePrefix =
    scopeCount > 0
      ? partial
        ? `${usedCount} of ${scopeCount} repos contributed`
        : `Scoped to ${scopeCount} ${scopeCount === 1 ? "repo" : "repos"}`
      : `${usedCount} ${usedCount === 1 ? "repo" : "repos"} routed`;

  // Single-line layout: the header is ``h-14`` and already carries the
  // chat title above. Wrapping pushed the title out of frame in early
  // builds; truncation + a hover-title that lists every repo keeps the
  // subtitle tight while preserving full visibility on demand.
  const allNames = names.join(", ");
  return (
    <div
      className={cn(
        "mt-0.5 flex min-w-0 items-center gap-2 text-[11px]",
        partial ? "text-warn" : "text-fg-2",
      )}
      title={lastResearch?.scope_rationale ?? allNames}
    >
      <span className="shrink-0">{headlinePrefix}:</span>
      <span className="min-w-0 flex-1 truncate font-mono text-fg-1">
        {visible.map((name, i) => (
          <span key={name}>
            <Link
              to={`/knowledge?search=${encodeURIComponent(name)}`}
              className="hover:text-accent hover:underline"
            >
              {name}
            </Link>
            {i < visible.length - 1 && (
              <span className="text-fg-3"> · </span>
            )}
          </span>
        ))}
        {overflow > 0 && (
          <span className="ml-1.5 rounded bg-bg-2 px-1 text-[10px] text-fg-2">
            +{overflow}
          </span>
        )}
      </span>
      {lastResearch?.router_fallback && (
        <span
          className="inline-flex shrink-0 items-center gap-1 rounded border border-warn/40 bg-warn-soft px-1.5 py-0.5 text-[10px] font-medium text-warn"
          title={
            lastResearch.scope_rationale
              ? `Router fallback — ${lastResearch.scope_rationale}`
              : "Router fell back to defaults — answer may be off-topic"
          }
        >
          <AlertTriangle className="h-2.5 w-2.5" strokeWidth={2} />
          router fallback
        </span>
      )}
    </div>
  );
}


/* ─── Thread item ──────────────────────────────────────── */

function ThreadItem({
  conv,
  active,
  isStreaming = false,
  onSelect,
  onDelete,
  onRename,
}: {
  conv: ConversationSummary;
  active: boolean;
  isStreaming?: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onRename: (title: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conv.title);
  // Re-seed the rename draft when the conversation title changes upstream.
  // Uses the "adjust state during render" pattern instead of an effect to
  // avoid a cascading re-render (https://react.dev/reference/react/useState#storing-information-from-previous-renders).
  const [prevTitle, setPrevTitle] = useState(conv.title);
  if (prevTitle !== conv.title) {
    setPrevTitle(conv.title);
    setDraft(conv.title);
  }

  return (
    <li
      className={cn(
        "group relative mx-1.5 rounded-md transition-colors",
        active ? "bg-bg-3" : "hover:bg-bg-2",
      )}
    >
      {editing ? (
        <div className="flex items-center gap-1 px-2 py-1.5">
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                onRename(draft);
                setEditing(false);
              } else if (e.key === "Escape") {
                setDraft(conv.title);
                setEditing(false);
              }
            }}
            className="flex-1 rounded border border-border-focus bg-bg-2 px-2 py-1 text-sm text-fg-0 focus:outline-none"
          />
          <button
            onClick={() => {
              onRename(draft);
              setEditing(false);
            }}
            className="rounded p-1 text-fg-2 hover:bg-bg-3 hover:text-fg-0"
            aria-label="Save thread name"
            title="Save (Enter)"
          >
            <Check className="h-3.5 w-3.5" strokeWidth={2} />
          </button>
          <button
            onClick={() => {
              setDraft(conv.title);
              setEditing(false);
            }}
            className="rounded p-1 text-fg-2 hover:bg-bg-3 hover:text-fg-0"
            aria-label="Cancel rename"
            title="Cancel (Esc)"
          >
            <X className="h-3.5 w-3.5" strokeWidth={2} />
          </button>
        </div>
      ) : (
        <button
          onClick={onSelect}
          className="block w-full px-3 py-2 text-left"
        >
          <div className="flex items-center justify-between gap-2">
            <span
              className={cn(
                "flex min-w-0 items-center gap-1.5 truncate text-sm",
                active ? "font-medium text-fg-0" : "text-fg-1",
              )}
            >
              {/* Pulse dot when this thread is mid-stream so the
                  user can see at a glance which conversation is busy.
                  Helpful when juggling multiple threads. */}
              {isStreaming && (
                <span
                  className="inline-flex h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent"
                  aria-label="Generating"
                  title="Generating answer…"
                />
              )}
              <span className="truncate">{conv.title}</span>
            </span>
            <span className="shrink-0 text-[10px] text-fg-3">
              {formatRelativeDate(conv.updated_at)}
            </span>
          </div>
          {conv.last_message_preview && (
            <p className="mt-0.5 truncate text-[11px] text-fg-3">
              {conv.last_message_preview}
            </p>
          )}
          {/* Hover actions */}
          <div className="absolute right-1.5 top-1.5 flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                setEditing(true);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.stopPropagation();
                  e.preventDefault();
                  setEditing(true);
                }
              }}
              className="rounded p-1 text-fg-2 hover:bg-bg-3 hover:text-fg-0 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
              title="Rename thread"
              aria-label="Rename thread"
            >
              <Pencil className="h-3 w-3" strokeWidth={1.75} />
            </span>
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.stopPropagation();
                  e.preventDefault();
                  onDelete();
                }
              }}
              className="rounded p-1 text-fg-2 hover:bg-err-soft hover:text-err focus-visible:outline focus-visible:outline-1 focus-visible:outline-err"
              title="Delete thread"
              aria-label="Delete thread"
            >
              <Trash2 className="h-3 w-3" strokeWidth={1.75} />
            </span>
          </div>
        </button>
      )}
    </li>
  );
}

/* ─── Empty starter ────────────────────────────────────── */

const STARTERS = [
  "How do we onboard a new carrier end-to-end?",
  "What are the most recent open Jira tickets related to the orders service?",
  "How does retry and timeout handling work across our order services?",
  "What flows touch both SigningKeys and DeliveryGateway?",
  "Show me the architecture of the delivery pipeline.",
];

function EmptyStarterView({
  repoCount,
  onAsk,
}: {
  repoCount: number;
  onAsk: (q: string) => void;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6">
      <div className="max-w-2xl text-center">
        <div className="mb-3 inline-flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent-soft px-3 py-0.5 text-[11px] font-medium text-accent">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
          org-aware research agent
        </div>
        <h2 className="text-[28px] font-semibold tracking-tight text-fg-0">
          Bifrost Chat
        </h2>
        <p className="mt-3 text-[14.5px] leading-relaxed text-fg-2">
          Ask anything about your codebase. For each question, Bifrost fans out
          per-repo research agents that read live source + Jira + Confluence,
          then synthesizes one cited answer. Not just Claude — Claude that
          knows your systems, squads, and docs.
        </p>

        {repoCount === 0 ? (
          <div className="mt-6 inline-flex items-center gap-2 rounded-md border border-warn-soft bg-warn-soft px-3 py-2 text-[13px] text-warn">
            <AlertTriangle className="h-4 w-4" strokeWidth={1.75} />
            <span>
              No repositories registered yet.{" "}
              <Link
                to="/knowledge"
                className="underline underline-offset-2"
              >
                Register one first
              </Link>
              .
            </span>
          </div>
        ) : (
          <>
            <div className="mt-2 text-[11.5px] text-fg-3">
              Try one of these, or type your own below:
            </div>
            <div className="mt-4 grid gap-2 text-left">
              {STARTERS.map((s) => (
                <button
                  key={s}
                  onClick={() => onAsk(s)}
                  className="flex items-center gap-3 rounded-lg border border-border bg-bg-1 px-4 py-3 text-[13.5px] text-fg-1 transition-colors hover:border-accent hover:bg-accent-soft hover:text-accent-fg"
                >
                  <Sparkles
                    className="h-3.5 w-3.5 shrink-0 text-accent"
                    strokeWidth={1.75}
                  />
                  <span>{s}</span>
                </button>
              ))}
            </div>
            <div className="mt-4 text-[10.5px] text-fg-3">
              Scope to a domain via the dropdown on the left · typical turn
              ~60-120s · ~$1.5-3 per question
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ─── Message list ─────────────────────────────────────── */

const MessageList = memo(function MessageList({
  convId,
  messages,
  conversationLoaded,
  pendingUserMessage,
  streamingStatus,
  streamingText,
  streamingTools,
  streamingError,
  research,
  submitting,
  liveFollowups,
  onRegenerate,
  onFollowUp,
  intentByMessage,
  onRunQuick,
  onSpawnTask,
}: {
  convId: string | null;
  messages: ConversationMessage[];
  /** True once the conversation fetch has settled (not loading, has data).
   * Prevents the "didn't get an answer" retry banner from firing on a
   * stale in-flight fetch when the user switches conversations. */
  conversationLoaded: boolean;
  pendingUserMessage: string | null;
  streamingStatus: string;
  streamingText: string;
  streamingTools: Array<{ name: string; input: Record<string, unknown> }>;
  streamingError: string | null;
  research: ResearchState;
  submitting: boolean;
  liveFollowups: Array<{ prompt: string; rationale?: string }>;
  onRegenerate: () => void;
  onFollowUp: (q: string) => void;
  intentByMessage: Record<string, IntentClassification>;
  onRunQuick: (
    messageId: string,
    content: string,
    intent: IntentClassification,
  ) => void;
  onSpawnTask: (
    messageId: string,
    content: string,
    intent: IntentClassification,
  ) => void;
}) {
  const endRef = useRef<HTMLDivElement | null>(null);
  // ``followBottom`` tracks whether we should auto-stick to the
  // bottom. Toggled on when the user is within 120px of the bottom
  // (= they want to follow the stream); off when they scroll up.
  // We use a ref instead of state so updating it doesn't re-render.
  const followBottomRef = useRef(true);
  // Auto-scroll plumbing. The previous version called
  // ``el.scrollIntoView`` inside a useEffect with ``streamingText`` in
  // the dependency array — which meant 250+ token-rate effect fires
  // per turn, each of which interrupted the previous scroll mid-flight.
  // Result: the page visibly shook/oscillated as the messages and
  // research-pipeline blocks competed for "I'm the new bottom!" claims.
  //
  // Fix:
  //   * Coalesce all the dep-change spam into ONE scrollTop write per
  //     animation frame via requestAnimationFrame.
  //   * Set scrollTop directly on the scroll container instead of
  //     calling scrollIntoView, which can also scroll outer ancestors.
  //   * Track the "should I auto-follow" state in a ref + scroll
  //     listener, so the reader's manual scroll-up disengages the
  //     follow without forcing a state update.
  useEffect(() => {
    const sentinel = endRef.current;
    if (!sentinel) return;
    const scroller = sentinel.parentElement;
    if (!scroller) return;
    const onScroll = () => {
      const dist =
        scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      followBottomRef.current = dist < 120;
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    if (!followBottomRef.current) return;
    const sentinel = endRef.current;
    if (!sentinel) return;
    const scroller = sentinel.parentElement;
    if (!scroller) return;
    // Coalesce multiple dep changes (back-to-back token streams,
    // status events, research updates) into one scroll write per
    // browser frame.
    // BUG 5 fix (raf-cancel-leak): guard against unmount — the RAF
    // callback can fire after the component unmounts if the cancel
    // arrives on the same frame. Check isConnected + null scroller ref.
    const handle = requestAnimationFrame(() => {
      if (sentinel.isConnected && scroller) scroller.scrollTop = scroller.scrollHeight;
    });
    return () => cancelAnimationFrame(handle);
  // Intentionally omit streamingText and streamingStatus from deps — both
  // change every ~100ms during token streaming, causing RAF cancel/requeue
  // 5-10× per second which creates jank and competes with scroll events.
  // messages.length handles new message arrival; synthesisStarted handles
  // the layout shift when the answer starts streaming. The scroll
  // container's own height changes (caused by new tokens) are handled by
  // the native scroll behaviour — we only need to re-anchor once per turn.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.length, research.synthesisStarted, submitting]);

  // Show the research pipeline view whenever the coordinator has emitted
  // ANY progress (scope/freshness/route/agents). During single-repo chats
  // or pre-pipeline phases we fall back to the simpler streaming view.
  const hasResearchProgress =
    submitting &&
    (research.scope.length > 0 ||
      research.freshness.length > 0 ||
      research.route.length > 0 ||
      Object.keys(research.agents).length > 0);

  const showStreaming =
    submitting &&
    !hasResearchProgress &&
    (streamingText.length > 0 || streamingTools.length > 0);
  const showPending =
    submitting &&
    !hasResearchProgress &&
    streamingText.length === 0 &&
    streamingTools.length === 0;

  // Identify the last assistant message — show regenerate + follow-ups there only
  const lastAssistantIdx = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return i;
    }
    return -1;
  })();

  // A "dangling" turn: the last message is the user's question with no
  // assistant reply, and we're idle (not streaming) with no live error in
  // the store. This is what a *persisted* failed/interrupted turn looks like
  // after a reload — the ephemeral streamingError is gone, so without this
  // the user sees their question with no answer and no way to retry.
  const lastMsg = messages[messages.length - 1];
  // Guard: only show the retry banner when the conversation data is fully
  // loaded. During a conversation switch the fetch is in-flight and
  // `messages` may still be the previous (or empty) value — without this
  // guard a healthy conversation briefly shows "didn't get an answer".
  // `conversationLoaded` is passed as a prop by the parent.
  const danglingUserTurn =
    conversationLoaded &&
    !submitting &&
    !streamingError &&
    lastMsg?.role === "user";

  // Index conversation tasks by their spawning message_id
  // so we can render the lineage card inline at the exact turn that
  // produced each task. Hook polls every 30s for live state changes.
  const { byMessage: tasksByMessage } = useConvTasksByMessage(convId);

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 py-4 lg:px-8">
      {convId && <LinkedTasksBar conversationId={convId} />}
      <div className="space-y-8">
        {messages.map((m, idx) =>
          m.role === "user" ? (
            <div key={m.id}>
              <UserMessage
                msg={m}
                intent={intentByMessage[m.id]}
                onRunQuick={onRunQuick}
                onSpawnTask={onSpawnTask}
              />
              {/* Ownership fast-path: only fire for the LAST unanswered user
                  message (no assistant reply yet). Historical messages already
                  have their answer; mounting OwnershipFastPath for every
                  ownership question in a long thread fires N concurrent DB
                  queries on load — the primary cause of the frozen scroll +
                  navigation-blocked bug with 10-15 messages. */}
              {isOwnershipQuestion(m.content) &&
                messages[idx + 1] === undefined && (
                  <OwnershipFastPath query={m.content} />
                )}
              {(tasksByMessage[m.id] ?? []).map((link) => (
                <ChatTaskLineageCard key={link.task_id} link={link} />
              ))}
            </div>
          ) : (
            <AssistantMessage
              key={m.id}
              convId={convId}
              msg={m}
              isLast={idx === lastAssistantIdx}
              onRegenerate={onRegenerate}
              onFollowUp={onFollowUp}
            />
          ),
        )}
        {hasResearchProgress && (
          <ResearchInProgress
            research={research}
            streamingText={streamingText}
            conversationId={convId ?? ""}
          />
        )}
        {showStreaming && (
          <AssistantStreaming text={streamingText} tools={streamingTools} />
        )}
        {/* Live "Ask next" chips — appear the moment the coordinator
            emits ``suggested_followups``, before the persisted message
            refetch lands. Once the message is persisted, the chips
            render from ``msg.suggested_followups`` on AssistantMessage. */}
        {submitting && liveFollowups.length > 0 && (
          <div
            className="flex flex-wrap gap-1.5 pt-1 pl-10"
            data-testid="live-suggested-followups"
          >
            {liveFollowups.map((q) => (
              <button
                key={q.prompt}
                onClick={() => onFollowUp(q.prompt)}
                title={q.rationale || undefined}
                className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-1 px-3 py-1 text-[11.5px] text-fg-1 transition-colors hover:border-accent hover:bg-accent-soft hover:text-accent"
              >
                <Sparkles className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
                {q.prompt}
              </button>
            ))}
          </div>
        )}
        {/* Optimistic user bubble — show the selected suggestion immediately
            while the server persists it and the refetch is in-flight. */}
        {pendingUserMessage && submitting && (
          <div className="flex justify-end">
            <div className="max-w-[75%] rounded-2xl rounded-br-sm bg-accent px-4 py-2.5 text-[13.5px] leading-relaxed text-accent-fg">
              {pendingUserMessage}
            </div>
          </div>
        )}
        {showPending && (
          <ResearchPipelineSkeleton
            status={streamingStatus || "Resolving scope…"}
            researchStatus={research.status}
          />
        )}
        {streamingError && !submitting && (
          <ResearchErrorBlock message={streamingError} onRetry={onRegenerate} />
        )}
        {danglingUserTurn && (
          <ResearchErrorBlock
            title="This question didn't get an answer"
            message={
              "The previous attempt didn't complete (it may have failed or " +
              "been interrupted). Retry to run it again — any cached agent " +
              "results from the earlier attempt are reused."
            }
            retryLabel="Retry"
            onRetry={onRegenerate}
          />
        )}
      </div>
      <div ref={endRef} className="h-6" />
    </div>
  );
});

/**
 * Minimal 3-line status indicator shown while routing/agents are running
 * (before synthesis starts). Replaces the full ResearchProgress panel
 * during the pre-answer phase so the user sees a calm progress hint
 * rather than the full pipeline grid.
 */
function ResearchMiniStatus({ research }: { research: ResearchState }) {
  const hasScope = research.scope.length > 0;
  const hasAgents = Object.keys(research.agents).length > 0;
  const synthesisStarted = research.synthesisStarted;

  const steps: { label: string; done: boolean; active: boolean }[] = [
    {
      label: "Identifying repos…",
      done: hasScope,
      active: !hasScope,
    },
    {
      label: "Reading code…",
      done: hasAgents && synthesisStarted,
      active: hasScope && !synthesisStarted,
    },
    {
      label: "Writing answer…",
      done: synthesisStarted,
      active: hasAgents && !synthesisStarted,
    },
  ];

  return (
    <div className="space-y-1 py-1">
      {steps.map((s) => (
        <div key={s.label} className="flex items-center gap-2 text-[12px]">
          {s.done ? (
            <Check className="h-3 w-3 shrink-0 text-ok" strokeWidth={2.5} />
          ) : s.active ? (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-accent" strokeWidth={1.75} />
          ) : (
            <Circle className="h-3 w-3 shrink-0 text-fg-3" strokeWidth={1.75} />
          )}
          <span className={s.done ? "text-fg-2 line-through" : s.active ? "text-fg-0" : "text-fg-3"}>
            {s.label}
          </span>
        </div>
      ))}
      {research.status && (
        <p className="pl-5 text-[11px] text-fg-3 italic">{research.status}</p>
      )}
    </div>
  );
}

function ResearchInProgress({
  research,
  streamingText,
  conversationId,
}: {
  research: ResearchState;
  streamingText: string;
  conversationId: string;
}) {
  // Auto-collapse the detail panel once the answer starts streaming.
  // The user can re-expand manually at any time.
  const [detailOpen, setDetailOpen] = useState(false);
  const synthesisStreaming = research.synthesisStarted && streamingText.length > 0;

  // When synthesis begins, ensure the panel starts collapsed.
  // We track a ref so this effect only fires once on the transition.
  const didAutoCollapseRef = useRef(false);
  useEffect(() => {
    if (synthesisStreaming && !didAutoCollapseRef.current) {
      didAutoCollapseRef.current = true;
      setDetailOpen(false);
    }
  }, [synthesisStreaming]);

  return (
    <div className="flex items-start gap-3">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg">
        <Sparkles
          className={cn("h-3.5 w-3.5", !synthesisStreaming && "animate-pulse")}
          strokeWidth={1.75}
        />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-medium text-fg-2">Bifrost</span>
          <span className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent">
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent" />
            {synthesisStreaming ? "writing" : "researching"}
          </span>
        </div>

        {/* ── Primary: streaming answer (once synthesis begins) ── */}
        {synthesisStreaming && (
          <div className="max-w-[96ch]">
            <Prose>{streamingText + "▍"}</Prose>
          </div>
        )}

        {/* ── Secondary: research details ── */}
        {synthesisStreaming ? (
          /* Collapsible pipeline details — collapsed by default once answer starts */
          <div className="rounded-md border border-border bg-bg-1">
            <button
              type="button"
              onClick={() => setDetailOpen((v) => !v)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-[11.5px] text-fg-2 hover:bg-bg-2 transition-colors rounded-md"
            >
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 shrink-0 text-fg-3 transition-transform",
                  detailOpen && "rotate-180",
                )}
                strokeWidth={1.75}
              />
              <span className="font-medium">Research details</span>
              <span className="ml-auto text-[10.5px] text-fg-3">
                {Object.keys(research.agents).length} agent{Object.keys(research.agents).length !== 1 ? "s" : ""}
                {research.route.length > 0 ? ` · ${research.route.length} repo${research.route.length !== 1 ? "s" : ""}` : ""}
              </span>
            </button>
            {detailOpen && (
              <div className="border-t border-border px-3 py-3">
                <ResearchProgress state={research} conversationId={conversationId} />
              </div>
            )}
          </div>
        ) : (
          /* Pre-synthesis: show minimal 3-line progress instead of full grid */
          <ResearchMiniStatus research={research} />
        )}
      </div>
    </div>
  );
}

function AssistantStreaming({
  text,
  tools,
}: {
  text: string;
  tools: Array<{ name: string; input: Record<string, unknown> }>;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg">
        <Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-medium text-fg-2">Bifrost</span>
          <span className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent">
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent" />
            {tools.length > 0 ? "exploring" : "streaming"}
          </span>
        </div>
        {tools.length > 0 && <ToolUseList tools={tools} />}
        {text.length > 0 && (
          <div className="max-w-[96ch]">
            <Prose>{text + "▍"}</Prose>
          </div>
        )}
      </div>
    </div>
  );
}

function ToolUseList({
  tools,
}: {
  tools: Array<{ name: string; input: Record<string, unknown> }>;
}) {
  return (
    <ul className="space-y-1">
      {tools.map((t, i) => (
        <li
          key={i}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-2 py-1 font-mono text-[11px] text-fg-2"
        >
          <span className="font-semibold text-fg-1">{toolVerb(t.name)}</span>
          <span className="truncate text-fg-2">{toolLabel(t.name, t.input)}</span>
        </li>
      ))}
    </ul>
  );
}

function toolVerb(name: string): string {
  return (
    { Read: "read", Grep: "grep", Glob: "glob", Bash: "bash" }[name] ?? name
  );
}

function toolLabel(name: string, input: Record<string, unknown>): string {
  if (name === "Read") {
    return String(input.file_path ?? "");
  }
  if (name === "Grep") {
    const pattern = input.pattern ? `"${input.pattern}"` : "";
    const where = input.path ? ` in ${input.path}` : "";
    return `${pattern}${where}`;
  }
  if (name === "Glob") {
    return String(input.pattern ?? "");
  }
  return JSON.stringify(input).slice(0, 80);
}

/**
 * Memoized so a parent re-render (e.g., a streaming token landing in
 * the bubble below) doesn't force every prior user bubble to repaint.
 * Parent passes STABLE callbacks (useCallback'd in QueryPage) that we
 * dispatch to with this message's own id/content/intent; that keeps
 * memo's shallow prop compare meaningful instead of always-different.
 */
const UserMessage = memo(function UserMessage({
  msg,
  intent,
  onRunQuick,
  onSpawnTask,
}: {
  msg: ConversationMessage;
  intent: IntentClassification | undefined;
  onRunQuick: (
    messageId: string,
    content: string,
    intent: IntentClassification,
  ) => void;
  onSpawnTask: (
    messageId: string,
    content: string,
    intent: IntentClassification,
  ) => void;
}) {
  return (
    <div className="flex items-start gap-3">
      <div
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-bg-3 text-fg-1"
        title="You"
      >
        <User className="h-3.5 w-3.5" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 text-[11px] font-medium text-fg-2">You</div>
        <div className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg-0">
          {msg.content}
        </div>
        {intent && (
          <ActionIntentChip
            intent={intent}
            onRunQuick={() => onRunQuick(msg.id, msg.content, intent)}
            onSpawnTask={() => onSpawnTask(msg.id, msg.content, intent)}
          />
        )}
      </div>
    </div>
  );
});

/**
 * Intent-aware suggestion chip rendered under user messages.
 *
 * We render only when the classifier is reasonably confident the user
 * wants an *action* (``quick``/``execute``) — pure-Q&A messages get
 * no chip so the regular chat UX isn't cluttered. ``clarify`` leaves
 * the decision open: we show a softer prompt asking which way to go.
 */
function ActionIntentChip({
  intent,
  onRunQuick,
  onSpawnTask,
}: {
  intent: IntentClassification;
  onRunQuick: () => void;
  onSpawnTask: () => void;
}) {
  const kind = intent.intent;
  if (kind === "ask") return null;          // ordinary question — no chip
  if (intent.confidence < 0.55 && kind !== "clarify") return null;

  const isQuick = kind === "quick";
  const isExecute = kind === "execute" || kind === "plan";
  const label =
    kind === "quick"
      ? "Looks like a trivial change"
      : kind === "execute"
        ? "Looks like a task to implement"
        : kind === "plan"
          ? "Looks like something to plan"
          : "Ambiguous — want to turn this into a task?";

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-dashed border-accent/40 bg-accent-soft/20 px-2.5 py-1.5">
      <Wand2 className="h-3 w-3 shrink-0 text-accent" strokeWidth={1.75} />
      <span className="text-[11.5px] text-fg-1">
        {label}{" "}
        <span className="font-mono text-[10.5px] text-fg-3">
          ({Math.round(intent.confidence * 100)}% {kind})
        </span>
      </span>
      {isQuick && (
        <button
          onClick={onRunQuick}
          className="inline-flex items-center gap-1 rounded-md border border-ok bg-ok-soft px-2 py-0.5 text-[11px] font-semibold text-ok hover:bg-ok hover:text-bg-0"
          title="Run quick mode — one Haiku plan + execute + verify inline"
        >
          <Zap className="h-2.5 w-2.5" strokeWidth={2} />
          Run quick
        </button>
      )}
      <button
        onClick={onSpawnTask}
        className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent-soft px-2 py-0.5 text-[11px] font-semibold text-accent hover:bg-accent hover:text-bg-0"
        title="Create a task linked to this conversation — discuss/plan/execute"
      >
        <ClipboardList className="h-2.5 w-2.5" strokeWidth={2} />
        {isExecute || isQuick ? "Create task" : "Make a task"}
      </button>
    </div>
  );
}

/* ── Linked tasks bar (chat ← tasks) ───────────────────────── */

function LinkedTasksBar({ conversationId }: { conversationId: string }) {
  const { data: tasks } = useQuery({
    queryKey: ["conv-tasks", conversationId],
    queryFn: () => getConversationTasks(conversationId),
    staleTime: 10_000,
    refetchInterval: 30_000,                     // keep state pills live
  });
  const list: ConvTaskLink[] = tasks ?? [];
  if (list.length === 0) return null;
  return (
    <div className="mb-5 flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-bg-1 px-3 py-2">
      <ClipboardList className="h-3.5 w-3.5 shrink-0 text-fg-2" strokeWidth={1.75} />
      <span className="text-[11.5px] font-medium text-fg-2">
        Linked tasks
      </span>
      <span className="font-mono text-[10.5px] text-fg-3">
        ({list.length})
      </span>
      <div className="flex flex-wrap gap-1">
        {list.slice(0, 8).map((b) => (
          <Link
            key={b.task_id + b.link_type}
            to={`/tasks/${b.task_id}`}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-medium",
              b.state === "pr_merged"
                ? "border-ok/50 bg-ok-soft text-ok"
                : b.state === "success"
                  ? "border-ok/40 bg-ok-soft text-ok"
                  : b.state === "blocked" || b.state === "cancelled"
                    ? "border-err/40 bg-err-soft text-err"
                    : "border-accent/40 bg-accent-soft text-accent",
            )}
            title={`${b.state} · linked via ${b.link_type}`}
          >
            <span className="truncate max-w-[160px]">
              {b.title.slice(0, 36)}{b.title.length > 36 ? "…" : ""}
            </span>
            {b.jira_key && (
              <span className="font-mono text-[9.5px] opacity-80">
                {b.jira_key}
              </span>
            )}
            {b.pr_number && (
              <span className="font-mono text-[9.5px] opacity-80">
                #{b.pr_number}
              </span>
            )}
          </Link>
        ))}
        {list.length > 8 && (
          <span className="text-[10.5px] text-fg-3">+{list.length - 8}</span>
        )}
      </div>
    </div>
  );
}

/** Hook: index conversation tasks by their spawning message_id so the
 * UserMessage component can render an inline lineage card at the
 * exact turn that produced each task. Polls every 30s so a Jira move
 * (state→Done after PR merge) reflects without a manual reload. */
function useConvTasksByMessage(conversationId: string | null): {
  byMessage: Record<string, ConvTaskLink[]>;
  all: ConvTaskLink[];
} {
  const { data } = useQuery({
    queryKey: ["conv-tasks", conversationId],
    queryFn: () =>
      conversationId ? getConversationTasks(conversationId) : Promise.resolve([] as ConvTaskLink[]),
    enabled: !!conversationId,
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
  const all = data ?? [];
  const byMessage: Record<string, ConvTaskLink[]> = {};
  for (const link of all) {
    const key = link.link_message_id ?? "";
    if (!key) continue;
    (byMessage[key] = byMessage[key] || []).push(link);
  }
  return { byMessage, all };
}

/**
 * Memoized for the same reason as UserMessage — without it, every
 * streaming-token re-render of the parent rebuilds the markdown DOM
 * + syntax highlighter for every prior assistant turn in the thread.
 * Props are all stable (msg by identity, isLast/convId primitives,
 * onRegenerate/onFollowUp useCallback'd in the parent).
 */
const AssistantMessage = memo(function AssistantMessage({
  msg,
  isLast,
  onRegenerate,
  onFollowUp,
  convId,
}: {
  msg: ConversationMessage;
  isLast: boolean;
  onRegenerate: () => void;
  onFollowUp: (q: string) => void;
  convId: string | null;
}) {
  const [copied, setCopied] = useState(false);
  // Sources default to COLLAPSED — users asked for a cleaner answer
  // view. Click the bar to expand when they want to dig in.
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  // Per-message feedback state. Once submitted, the buttons disable
  // and show the verdict so the user knows the click registered.
  // ``null`` = not yet submitted, ``"up" | "down"`` = recorded.
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const [feedbackPending, setFeedbackPending] = useState(false);

  const onFeedback = useCallback(
    async (verdict: "up" | "down") => {
      if (!convId || feedback || feedbackPending) return;
      setFeedbackPending(true);
      try {
        const res = await submitMessageFeedback(convId, msg.id, verdict);
        setFeedback(verdict);
        if (res.recorded > 0) {
          toast.success(
            verdict === "up"
              ? `Thanks — ${res.recorded} citation${res.recorded === 1 ? "" : "s"} confirmed`
              : `Recorded — ${res.recorded} citation${res.recorded === 1 ? "" : "s"} flagged for review`,
          );
        } else {
          toast.success(
            verdict === "up" ? "Thanks for the feedback" : "Feedback noted",
          );
        }
      } catch (e: unknown) {
        toast.error(`Couldn't record feedback: ${describeError(e)}`);
      } finally {
        setFeedbackPending(false);
      }
    },
    [convId, msg.id, feedback, feedbackPending],
  );

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(msg.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Couldn't copy to clipboard");
    }
  };

  // Server-generated "Ask next" prompts (from research.deepening) win
  // when present — they're grounded in the answer's actual gaps /
  // unwalked repos. Fall back to the client-side heuristic only when
  // the backend produced none (e.g. older messages, or a turn with no
  // gaps). Live-streamed suggestions arrive on the message via the SSE
  // assistant_message frame; persisted ones via the conversation GET.
  const followUps: FollowUpChip[] = useMemo(() => {
    if (!isLast) return [];
    if (msg.suggested_followups && msg.suggested_followups.length > 0)
      return msg.suggested_followups.map((s) => ({ prompt: s.prompt, rationale: s.rationale }));
    return buildFollowUps(msg).map((q) => ({ prompt: q }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLast, msg.id, msg.suggested_followups]);

  return (
    <div className="flex items-start gap-3">
      <div
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg"
        title="Bifrost"
      >
        <Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        {/* Row: label + meta. Reserve a fixed
            min-height so the row doesn't shift between messages with
            and without confidence/sources metadata. The placeholders
            render as faded em-dashes when data is missing so the
            absence is visible without jarring layout. */}
        <div className="flex min-h-[22px] flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-fg-2">Bifrost</span>
          {msg.confidence ? (
            <ConfidenceBadge level={msg.confidence} />
          ) : (
            <span
              className="text-[11px] text-fg-3"
              title="No confidence reported"
            >
              · —
            </span>
          )}
          <span className="text-[11px] text-fg-3">
            ·{" "}
            {msg.sources.length > 0
              ? `${msg.sources.length} ${msg.sources.length === 1 ? "source" : "sources"}`
              : "no sources"}
          </span>
        </div>

        {/* Body — clean editorial typography, no card frame. The
            chat surface itself is the page; bordered cards inside
            chat surfaces just create visual noise and make the
            answer feel cramped. Reading column ~70ch for line-
            length comfort; the Prose component handles all internal
            spacing/hierarchy.
            Empty/whitespace-only content gets an explicit
            fallback hint with a one-click retry. */}
        <div className="max-w-[96ch]">
          {msg.content && msg.content.trim().length > 0 ? (
            <Prose>{msg.content}</Prose>
          ) : (
            <div className="flex items-center justify-between gap-3 rounded-lg border border-warn-soft bg-warn-soft px-3 py-2.5">
              <div className="flex items-center gap-2 text-[12.5px] text-warn">
                <AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />
                <span>
                  This answer came back empty.{" "}
                  {msg.gaps && msg.gaps.length > 0
                    ? "See knowledge gaps below."
                    : "The agent may have run into a tool failure mid-stream."}
                </span>
              </div>
              {isLast && (
                <button
                  onClick={onRegenerate}
                  className="inline-flex items-center gap-1 rounded border border-border bg-bg-1 px-2 py-1 text-[11.5px] text-fg-1 hover:border-accent hover:text-accent"
                >
                  <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
                  Retry
                </button>
              )}
            </div>
          )}
        </div>

        {/* Sources — default collapsed, grouped by kind. */}
        {msg.sources.length > 0 && (
          <SourcesPanel
            sources={msg.sources}
            expanded={sourcesExpanded}
            onToggle={() => setSourcesExpanded(!sourcesExpanded)}
          />
        )}

        {/* Gaps */}
        {msg.gaps && msg.gaps.length > 0 && (
          <div className="rounded-lg border border-warn-soft bg-warn-soft px-3 py-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-[11.5px] font-medium text-warn">
              <AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />
              Knowledge gaps
            </div>
            <ul className="space-y-0.5 text-[12.5px] text-fg-1">
              {msg.gaps.map((g, i) => (
                <li key={i}>· {g}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Action row */}
        <div className="flex items-center gap-0.5 pt-1">
          {/* Feedback thumbs — feeds the continuous-learning loop.
              👍 confirms cited URNs (small confidence bump);
              👎 contradicts (immediate downgrade applied by
              learning governance). One-shot per message; once
              clicked, both buttons lock + show the verdict. */}
          {convId && (
            <ActionBtn
              onClick={() => void onFeedback("up")}
              title={
                feedback === "up"
                  ? "Marked helpful"
                  : feedback === "down"
                    ? "Already flagged"
                    : "Helpful — confirms the cited URNs"
              }
              disabled={feedback !== null || feedbackPending}
            >
              <ThumbsUp
                className={cn(
                  "h-3.5 w-3.5",
                  feedback === "up" && "text-ok",
                )}
                strokeWidth={1.75}
              />
            </ActionBtn>
          )}
          {convId && (
            <ActionBtn
              onClick={() => void onFeedback("down")}
              title={
                feedback === "down"
                  ? "Flagged for review"
                  : feedback === "up"
                    ? "Already marked helpful"
                    : "Wrong / unhelpful — flags the cited URNs"
              }
              disabled={feedback !== null || feedbackPending}
            >
              <ThumbsDown
                className={cn(
                  "h-3.5 w-3.5",
                  feedback === "down" && "text-warn",
                )}
                strokeWidth={1.75}
              />
            </ActionBtn>
          )}
          <ActionBtn onClick={onCopy} title={copied ? "Copied" : "Copy answer"}>
            {copied ? (
              <Check className="h-3.5 w-3.5 text-ok" strokeWidth={1.75} />
            ) : (
              <Copy className="h-3.5 w-3.5" strokeWidth={1.75} />
            )}
          </ActionBtn>
          {msg.reasoning && (
            <ReasoningPopover reasoning={msg.reasoning} />
          )}
          {isLast && (
            <ActionBtn onClick={onRegenerate} title="Regenerate answer">
              <RefreshCcw className="h-3.5 w-3.5" strokeWidth={1.75} />
              <span>Regenerate</span>
            </ActionBtn>
          )}
        </div>

        {/* Follow-up suggestions — last assistant message only */}
        {isLast && followUps.length > 0 && (
          <div
            className="flex flex-wrap gap-1.5 pt-1"
            data-testid="suggested-followups"
          >
            {followUps.map((q) => (
              <button
                key={q.prompt}
                onClick={() => onFollowUp(q.prompt)}
                title={q.rationale || undefined}
                className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-1 px-3 py-1 text-[11.5px] text-fg-1 transition-colors hover:border-accent hover:bg-accent-soft hover:text-accent"
              >
                <Sparkles className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
                {q.prompt}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

/* ─── Sources panel (collapsible, grouped by kind) ───────────────── */

interface SourceGroup {
  key: string;
  label: string;
  accent: "accent" | "warn" | "ok" | "info" | "fg-2";
  items: Array<{ src: import("@/lib/api").SourceCitation; idx: number }>;
}

function groupSources(
  sources: import("@/lib/api").SourceCitation[],
): SourceGroup[] {
  const buckets: Record<string, SourceGroup> = {
    code: { key: "code", label: "Code", accent: "accent", items: [] },
    jira: { key: "jira", label: "Jira", accent: "info", items: [] },
    confluence: {
      key: "confluence",
      label: "Confluence",
      accent: "ok",
      items: [],
    },
    github: { key: "github", label: "GitHub", accent: "fg-2", items: [] },
    slack: { key: "slack", label: "Slack", accent: "warn", items: [] },
    other: { key: "other", label: "Other", accent: "fg-2", items: [] },
  };

  sources.forEach((src, idx) => {
    // Skip malformed entries — without urn or name there's nothing to render
    if (!src || (!src.urn && !src.name)) return;
    const t = (src.entity_type || "").toLowerCase();
    let b: keyof typeof buckets = "other";
    if (
      ["component", "flow", "dependency", "interface", "pattern",
       "service", "repo", "file", "code"].includes(t)
    ) b = "code";
    else if (t === "jira") b = "jira";
    else if (t === "confluence") b = "confluence";
    else if (t === "github_pr" || t === "github_issue" || t === "github") b = "github";
    else if (t === "slack") b = "slack";
    buckets[b].items.push({ src, idx });
  });

  return Object.values(buckets).filter((g) => g.items.length > 0);
}

function SourcesPanel({
  sources,
  expanded,
  onToggle,
}: {
  sources: import("@/lib/api").SourceCitation[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const groups = groupSources(sources);
  // Nothing renderable — bail before drawing the empty header
  if (groups.length === 0) return null;
  const renderableCount = groups.reduce((n, g) => n + g.items.length, 0);

  return (
    <div className="max-w-[96ch] rounded-lg border border-border bg-bg-1">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left transition-colors hover:bg-bg-2"
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-medium text-fg-1">
            {expanded ? "Hide" : "View"} source citations
          </span>
          <span className="font-mono text-[10.5px] text-fg-3">
            {renderableCount}
          </span>
          {!expanded && (
            <div className="flex items-center gap-1">
              {groups.map((g) => (
                <span
                  key={g.key}
                  className={cn(
                    "inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[9.5px] font-medium",
                    g.accent === "accent" && "bg-accent-soft text-accent",
                    g.accent === "info" && "bg-info-soft text-info",
                    g.accent === "ok" && "bg-ok-soft text-ok",
                    g.accent === "warn" && "bg-warn-soft text-warn",
                    g.accent === "fg-2" && "bg-bg-2 text-fg-2",
                  )}
                >
                  {g.label}
                  <span className="font-mono opacity-70">{g.items.length}</span>
                </span>
              ))}
            </div>
          )}
        </div>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 text-fg-2 transition-transform",
            expanded && "rotate-180",
          )}
          strokeWidth={1.75}
        />
      </button>
      {expanded && (
        <div className="divide-y divide-border border-t border-border">
          {groups.map((g) => (
            <div key={g.key}>
              <div className="flex items-center gap-2 bg-bg-2/40 px-3 py-1.5">
                <span
                  className={cn(
                    "text-[10px] font-semibold uppercase tracking-wide",
                    g.accent === "accent" && "text-accent",
                    g.accent === "info" && "text-info",
                    g.accent === "ok" && "text-ok",
                    g.accent === "warn" && "text-warn",
                    g.accent === "fg-2" && "text-fg-2",
                  )}
                >
                  {g.label}
                </span>
                <span className="font-mono text-[10.5px] text-fg-3">
                  {g.items.length}
                </span>
              </div>
              <ul>
                {g.items.map(({ src, idx }) => (
                  <li key={idx}>
                    <Link
                      to={
                        src.entity_type === "jira" ||
                        src.entity_type === "confluence" ||
                        src.entity_type === "slack" ||
                        src.entity_type === "github_pr" ||
                        src.entity_type === "github_issue"
                          ? src.urn  // external URL
                          : `/knowledge?search=${encodeURIComponent(src.name)}`
                      }
                      target={
                        src.urn?.startsWith("http") ? "_blank" : undefined
                      }
                      rel={
                        src.urn?.startsWith("http")
                          ? "noreferrer"
                          : undefined
                      }
                      className="group flex items-center gap-2.5 px-3 py-2 hover:bg-bg-2"
                    >
                      <span className="tabular w-6 shrink-0 text-[10.5px] text-fg-3">
                        {String(idx + 1).padStart(2, "0")}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg-0 group-hover:text-accent">
                        {src.name}
                      </span>
                      <span className="hidden shrink-0 truncate font-mono text-[10.5px] text-fg-3 md:block md:max-w-[320px]">
                        {src.urn}
                      </span>
                      <ExternalLink
                        className="h-3 w-3 shrink-0 text-fg-3 group-hover:text-accent"
                        strokeWidth={1.75}
                      />
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** A rendered follow-up chip: the prompt to re-submit + optional why. */
interface FollowUpChip {
  prompt: string;
  rationale?: string;
}

// Client-side follow-up suggestions based on entities in the answer's sources
function buildFollowUps(msg: ConversationMessage): string[] {
  const suggestions: string[] = [];
  const srcs = msg.sources ?? [];
  const components = srcs.filter((s) => s.entity_type === "component");
  const flows = srcs.filter((s) => s.entity_type === "flow");
  const deps = srcs.filter((s) => s.entity_type === "dependency");

  // Entity-specific follow-ups
  if (components.length > 0) {
    const name = components[0].name;
    suggestions.push(`Where is ${name} used?`);
  }
  if (flows.length > 0) {
    const name = flows[0].name;
    suggestions.push(`Trace the ${name} flow end-to-end`);
  }
  if (deps.length > 0) {
    const name = deps[0].name;
    suggestions.push(`What uses the ${name} dependency?`);
  }

  // Content-based follow-ups
  const hasCode = /```/.test(msg.content);
  if (hasCode && suggestions.length < 3) {
    suggestions.push("Explain the error handling");
  }

  // Gap-based follow-ups
  if (msg.gaps && msg.gaps.length > 0 && suggestions.length < 3) {
    suggestions.push("Can you fill the knowledge gaps you flagged?");
  }

  // Generic fallbacks
  const fallbacks = [
    "Show me where this is called from",
    "What tests cover this?",
    "What could break if we change this?",
  ];
  for (const f of fallbacks) {
    if (suggestions.length >= 3) break;
    if (!suggestions.includes(f)) suggestions.push(f);
  }

  return suggestions.slice(0, 3);
}

function ActionBtn({
  onClick,
  children,
  title,
  disabled = false,
}: {
  onClick?: () => void;
  children: React.ReactNode;
  title: string;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      disabled={disabled}
      className="flex h-7 items-center gap-1.5 rounded-md px-2 text-[11.5px] font-medium text-fg-2 hover:bg-bg-2 hover:text-fg-0 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent disabled:hover:text-fg-2"
    >
      {children}
    </button>
  );
}

function ReasoningPopover({ reasoning }: { reasoning: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <ActionBtn onClick={() => setOpen(!open)} title="Show reasoning">
        <Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />
        <span>Reasoning</span>
      </ActionBtn>
      {open && (
        <div className="absolute left-0 top-8 z-10 w-[420px] rounded-lg border border-border bg-bg-2 p-3 shadow-lg">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] font-medium text-fg-2">Reasoning</span>
            <button
              onClick={() => setOpen(false)}
              className="text-fg-3 hover:text-fg-1"
            >
              <X className="h-3 w-3" strokeWidth={2} />
            </button>
          </div>
          <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-fg-1">
            {reasoning}
          </p>
        </div>
      )}
    </div>
  );
}

/**
 * Early-phase pending indicator shown from t=0 until the first SSE
 * research event arrives (at which point ``hasResearchProgress`` becomes
 * true and ``ResearchInProgress`` takes over).
 *
 * Previously this rendered a full five-row pipeline skeleton that
 * popped in and then immediately transitioned to the live grid, causing
 * a jarring double-render. Now it shows the same minimal 3-line status
 * that ``ResearchInProgress`` displays during the pre-synthesis phase —
 * the transition from skeleton → live component is seamless because both
 * render the same visual structure.
 */
function ResearchPipelineSkeleton({
  status,
  researchStatus,
}: {
  status: string;
  researchStatus: string | null;
}) {
  const displayStatus = researchStatus || status;
  return (
    <div className="flex items-start gap-3">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg">
        <Sparkles className="h-3.5 w-3.5 animate-pulse" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-medium text-fg-2">Bifrost</span>
          <span className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent">
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent" />
            researching
          </span>
        </div>
        <div className="space-y-1 py-1">
          <div className="flex items-center gap-2 text-[12px]">
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-accent" strokeWidth={1.75} />
            <span className="text-fg-0">Identifying repos…</span>
          </div>
          <div className="flex items-center gap-2 text-[12px]">
            <Circle className="h-3 w-3 shrink-0 text-fg-3" strokeWidth={1.75} />
            <span className="text-fg-3">Reading code…</span>
          </div>
          <div className="flex items-center gap-2 text-[12px]">
            <Circle className="h-3 w-3 shrink-0 text-fg-3" strokeWidth={1.75} />
            <span className="text-fg-3">Writing answer…</span>
          </div>
          {displayStatus && (
            <p className="pl-5 text-[11px] text-fg-3 italic">{displayStatus}</p>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Inline error block surfaced when the coordinator emits an
 * ``error`` SSE event mid-turn (e.g. "no_keyword_match" — no repo
 * matches the user's question, so research can't proceed). The
 * coordinator's error strings are user-actionable and may include
 * bullet-pointed hints separated by newlines; we split on those so
 * the bullets render as a real list, not a wall of text.
 */
function ResearchErrorBlock({
  message,
  onRetry,
  title = "Bifrost couldn't run research",
  retryLabel = "Retry search",
}: {
  message: string;
  onRetry?: () => void;
  title?: string;
  retryLabel?: string;
}) {
  const lines = message.split("\n").map((s) => s.trimEnd());
  const headLines: string[] = [];
  const bulletLines: string[] = [];
  for (const ln of lines) {
    if (ln.startsWith("• ") || ln.startsWith("  • ")) {
      bulletLines.push(ln.replace(/^\s*•\s*/, ""));
    } else if (ln.trim()) {
      headLines.push(ln);
    }
  }
  return (
    <div className="flex items-start gap-3">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-warn-soft text-warn">
        <AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-center gap-2 text-[11px] font-medium text-warn">
          <span>{title}</span>
        </div>
        <div className="rounded-lg border border-warn/30 bg-warn-soft/50 px-3 py-2.5">
          {headLines.length > 0 && (
            <p className="text-[12.5px] text-fg-1">
              {headLines.join(" ")}
            </p>
          )}
          {bulletLines.length > 0 && (
            <ul className="mt-2 space-y-1 text-[12px] text-fg-1">
              {bulletLines.map((b, i) => (
                <li key={i} className="flex items-start gap-2">
                  <span className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full bg-warn" />
                  <span>{b}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {/* Retry re-submits the last question reusing the prior turn's
            idempotency key, so cached agent work is reused (effectively a
            resume) and only the failed part re-runs. */}
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-2.5 py-1 text-[12px] font-medium text-fg-1 hover:bg-bg-2"
            title="Retry this search (reuses any cached agent results)"
          >
            <RefreshCcw className="h-3.5 w-3.5" strokeWidth={1.75} />
            <span>{retryLabel}</span>
          </button>
        )}
      </div>
    </div>
  );
}

/* ─── Composer ─────────────────────────────────────────── */

function Composer({
  value,
  onChange,
  onSend,
  onCancel,
  submitting,
  placeholder,
  textareaRef: externalRef,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onCancel?: () => void;
  submitting: boolean;
  placeholder: string;
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
}) {
  const localRef = useRef<HTMLTextAreaElement | null>(null);
  const textareaRef = externalRef ?? localRef;

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [value, textareaRef]);

  // ESC cancels an in-flight generation. The keyboard shortcut
  // matches every other modern chat (ChatGPT, Claude.ai). Only fires
  // when the textarea is focused so it doesn't fight other shortcuts.
  useEffect(() => {
    if (!submitting || !onCancel) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [submitting, onCancel]);

  return (
    <div className="shrink-0 border-t border-border bg-bg-0 px-6 py-4">
      <div className="mx-auto w-full max-w-[1200px]">
        <div className="relative flex items-end gap-2 rounded-xl border border-border bg-bg-1 focus-within:border-border-strong">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!submitting && value.trim()) onSend();
              }
            }}
            placeholder={placeholder}
            rows={1}
            className="flex-1 resize-none bg-transparent px-4 py-3 text-[14px] leading-relaxed text-fg-0 placeholder:text-fg-3 focus:outline-none"
            // Keep the textarea editable while a query is in
            // flight so the user can stage their next question. The
            // send button is what's gated by ``submitting``, not the
            // input itself.
          />
          {submitting && onCancel ? (
            <button
              onClick={onCancel}
              className="m-2 inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg bg-err px-3 text-[12px] font-medium text-white transition-colors hover:bg-err/90"
              aria-label="Stop generating"
              title="Stop generating (Esc)"
            >
              <X className="h-3.5 w-3.5" strokeWidth={2} />
              <span>Stop</span>
            </button>
          ) : (
            <button
              onClick={onSend}
              disabled={!value.trim()}
              className={cn(
                "m-2 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors",
                !value.trim()
                  ? "bg-bg-3 text-fg-3"
                  : "bg-accent text-accent-fg hover:bg-accent-hover",
              )}
              aria-label="Send"
            >
              <ArrowUp className="h-4 w-4" strokeWidth={2} />
            </button>
          )}
        </div>
        <p className="mt-2 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-center text-[11px] text-fg-3">
          <span>
            <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">Enter</kbd>{" "}
            send
          </span>
          <span>
            <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">Shift+Enter</kbd>{" "}
            newline
          </span>
          <span>
            <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">⌘K</kbd>{" "}
            focus
          </span>
          <span>
            <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">⌘⇧N</kbd>{" "}
            new chat
          </span>
        </p>
      </div>
    </div>
  );
}

/* ─── Ownership fast-path ──────────────────────────────── */

/**
 * Wrapper that runs the ownership lookup and renders cards inline.
 * Mounted only when isOwnershipQuestion() is true for a user message.
 * The query runs once per message (staleTime=Infinity) so navigating
 * back to the thread doesn't re-hit the DB.
 */
function OwnershipFastPath({ query }: { query: string }) {
  // Extract the search term: everything after the ownership verb.
  // e.g. "who owns order-service" → "order-service"
  const searchTerm = query
    .replace(
      /^.*?(?:who\s+(?:owns?|maintains?|is\s+responsible\s+for|works?\s+on|runs?)|what\s+(?:team|squad|group)\s+(?:owns?|maintains?|is\s+responsible\s+for|works?\s+on)|which\s+(?:team|squad|group)\s+(?:owns?|maintains?|is\s+responsible\s+for)|ownership\s+of|owner\s+of|point\s+of\s+contact\s+for|who\s+do\s+I\s+(?:contact|talk\s+to|ping)\s+about)\s*/i,
      "",
    )
    .replace(/[?!.]+$/, "")
    .trim();

  const { data, isLoading, isError } = useQuery({
    queryKey: ["ownership", searchTerm],
    queryFn: () => lookupOwnership(searchTerm || query),
    enabled: !!searchTerm || !!query,
    staleTime: Infinity, // ownership data doesn't change mid-session
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-3 py-2 text-[11.5px] text-fg-2">
        <Loader2 className="h-3 w-3 animate-spin shrink-0" strokeWidth={1.75} />
        Looking up ownership…
      </div>
    );
  }

  if (isError || !data || data.results.length === 0) {
    return null; // Fall through to the LLM answer silently
  }

  return (
    <div className="mt-3 space-y-2">
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-fg-2">
        <Database className="h-3 w-3 shrink-0" strokeWidth={1.75} />
        <span>Instant lookup · {data.total} match{data.total !== 1 ? "es" : ""} from org catalog</span>
        <span className="ml-1 inline-flex items-center gap-0.5 rounded-full bg-ok-soft px-1.5 py-0.5 text-[9.5px] font-semibold text-ok">
          fast
        </span>
      </div>
      {data.results.slice(0, 5).map((result) => (
        <OwnershipCard key={result.repo_name} result={result} />
      ))}
    </div>
  );
}

/**
 * Ownership card — one card per matching repo.
 * Shows: repo name, squad badge, Slack channel link, last activity,
 * Jira key, description, and a link to the repo.
 */
function OwnershipCard({ result }: { result: OwnershipResult }) {
  const lastActivity = result.last_commit_at
    ? formatRelativeDate(result.last_commit_at)
    : null;

  return (
    <div className="rounded-lg border border-border bg-bg-1 px-4 py-3 shadow-sm">
      {/* Header row: repo name + source badge */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="truncate text-[14px] font-semibold text-fg-0">
            {result.repo_name}
          </span>
          {result.source === "registered" && (
            <span className="inline-flex shrink-0 items-center rounded-full bg-accent-soft px-1.5 py-0.5 text-[9.5px] font-semibold text-accent">
              registered
            </span>
          )}
        </div>
        {result.url && (
          <a
            href={result.url}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 rounded p-1 text-fg-2 hover:bg-bg-2 hover:text-accent"
            title="Open repository"
          >
            <ExternalLink className="h-3.5 w-3.5" strokeWidth={1.75} />
          </a>
        )}
      </div>

      {/* Description */}
      {result.description && (
        <p className="mt-1 text-[12.5px] leading-snug text-fg-2">
          {result.description}
        </p>
      )}

      {/* Metadata chips row */}
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        {/* Squad badge */}
        {result.squad ? (
          <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent-soft px-2 py-0.5 text-[11px] font-medium text-accent">
            <Users className="h-3 w-3" strokeWidth={1.75} />
            {result.squad}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-2 px-2 py-0.5 text-[11px] text-fg-3">
            <Users className="h-3 w-3" strokeWidth={1.75} />
            no squad
          </span>
        )}

        {/* Slack channel */}
        {result.slack_channel && (
          <a
            href={`slack://channel?team=&id=${result.slack_channel.replace(/^#/, "")}`}
            className="inline-flex items-center gap-1 rounded-full border border-ok/30 bg-ok-soft px-2 py-0.5 text-[11px] font-medium text-ok hover:bg-ok hover:text-bg-0 transition-colors"
            title={`Open ${result.slack_channel} in Slack`}
          >
            <Hash className="h-3 w-3" strokeWidth={1.75} />
            {result.slack_channel}
          </a>
        )}

        {/* Service tier */}
        {result.service_tier && (
          <span className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-2 px-2 py-0.5 text-[11px] text-fg-2">
            tier-{result.service_tier}
          </span>
        )}

        {/* Last activity */}
        {lastActivity && (
          <span
            className="inline-flex items-center gap-1 text-[11px] text-fg-3"
            title={result.last_commit_at ?? undefined}
          >
            <Clock className="h-3 w-3" strokeWidth={1.75} />
            {lastActivity}
          </span>
        )}
      </div>

      {/* Contact row */}
      {(result.primary_contact || result.jira_project_key || result.confluence_space_key) && (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-fg-2">
          {result.primary_contact && (
            <span>
              <span className="font-medium text-fg-1">Contact:</span>{" "}
              {result.primary_contact}
            </span>
          )}
          {result.jira_project_key && (
            <span>
              <span className="font-medium text-fg-1">Jira:</span>{" "}
              <span className="font-mono">{result.jira_project_key}</span>
            </span>
          )}
          {result.confluence_space_key && (
            <span>
              <span className="font-medium text-fg-1">Confluence:</span>{" "}
              <span className="font-mono">{result.confluence_space_key}</span>
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Helpers ──────────────────────────────────────────── */

