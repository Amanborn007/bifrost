/**
 * ChatConnectionsPanel — right-rail "what does this chat connect to."
 *
 * The chat is the origin of the work graph: every conversation can
 * spawn a task, route an ask, open a Jira, ship a PR. Today those
 * connections live in three different places (task page, asks board,
 * Jira). This panel pulls them back into the chat itself so the
 * operator sees the *outgoing edges* of the current thread without
 * leaving it.
 *
 * Three sections, each rendered only when populated:
 *
 *   1. Spawned — tasks created from this chat (Linear-style row:
 *      title + state + jira/PR badges). Click → /tasks/:id.
 *   2. Asked — collab asks whose answer landed in this conversation.
 *      Click → /collaboration#asks.
 *   3. Related — other chats touching the same scope (repo overlap).
 *      Click → that chat (selected via parent ``onJumpToChat``).
 *
 * Live-pulse dot when fetching. 30s refetch keeps it fresh while
 * the operator is mid-conversation.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Cable,
  ExternalLink,
  GitPullRequest,
  Hourglass,
  MessageSquare,
  Sparkles,
} from "lucide-react";
import { cn, formatRelativeDate } from "@/lib/utils";
import {
  getConversationConnections,
  type ConnAsk,
  type ConnRelatedConv,
  type ConnTask,
} from "@/lib/api";


export function ChatConnectionsPanel({
  conversationId,
  onJumpToChat,
}: {
  conversationId: string;
  /** Callback when the operator clicks a related-conversation row.
   *  Lets the parent set ``activeId`` without a full route change. */
  onJumpToChat?: (id: string) => void;
}) {
  // FIX 2: Debounce the connections query so it doesn't fire for every
  // intermediate conversationId during React's render cycle. The 150ms
  // delay means we only hit the server once the conversation switch
  // has settled, preventing connection-slot contention with the
  // conversation detail fetch.
  const [stableConvId, setStableConvId] = useState(conversationId);
  useEffect(() => {
    const t = setTimeout(() => setStableConvId(conversationId), 150);
    return () => clearTimeout(t);
  }, [conversationId]);

  const conn = useQuery({
    queryKey: ["conv-connections", stableConvId],
    queryFn: () => getConversationConnections(stableConvId),
    refetchInterval: 60_000,  // 60s — graph edges change infrequently; was 30s
    staleTime: 10_000,
  });

  const data = conn.data;
  const isClean =
    !data ||
    (data.counts.tasks === 0 &&
      data.counts.asks === 0 &&
      data.counts.related === 0);

  return (
    <aside className="flex h-full w-[320px] shrink-0 flex-col overflow-hidden border-l border-border bg-bg-1">
      {/* Header — gradient + live-pulse dot signals refresh state */}
      <header className="flex items-center justify-between border-b border-border bg-gradient-to-r from-accent/5 to-transparent px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-accent/15 text-accent">
            <Cable className="h-3.5 w-3.5" strokeWidth={1.75} />
          </div>
          <div>
            <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold tracking-tight text-fg-0">
              Connections
              <span
                className={cn(
                  "dot bg-accent",
                  conn.isFetching ? "animate-pulse" : "live-pulse",
                )}
                aria-hidden
              />
            </h3>
            <div className="text-[10.5px] text-fg-3">
              {data
                ? `${data.counts.tasks} tasks · ${data.counts.asks} asks · ${data.counts.related} related`
                : "loading…"}
            </div>
          </div>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto">
        {conn.isLoading && (
          <div className="space-y-2 p-3">
            <div className="skel h-12 rounded-md" />
            <div className="skel h-12 rounded-md" />
            <div className="skel h-12 rounded-md" />
          </div>
        )}

        {!conn.isLoading && isClean && (
          <div className="flex flex-col items-center justify-center px-6 py-10 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-bg-2 text-fg-3">
              <Cable className="h-5 w-5" strokeWidth={1.5} />
            </div>
            <p className="mt-2 text-[12.5px] font-medium text-fg-1">
              Nothing linked yet
            </p>
            <p className="mt-1 max-w-[240px] text-[11px] text-fg-3">
              Spawn a task, route an ask, or pivot to a related chat —
              every action you take here lands in this panel.
            </p>
          </div>
        )}

        {data && data.counts.tasks > 0 && (
          <Section
            title="Spawned"
            icon={<Sparkles className="h-3 w-3" strokeWidth={1.75} />}
            count={data.counts.tasks}
          >
            {data.tasks.map((t) => (
              <TaskRow key={t.task_id} t={t} />
            ))}
          </Section>
        )}

        {data && data.counts.asks > 0 && (
          <Section
            title="Asked"
            icon={<Hourglass className="h-3 w-3" strokeWidth={1.75} />}
            count={data.counts.asks}
          >
            {data.asks.map((a) => (
              <AskRow key={a.id} a={a} />
            ))}
          </Section>
        )}

        {data && data.counts.related > 0 && (
          <Section
            title="Related chats"
            icon={<MessageSquare className="h-3 w-3" strokeWidth={1.75} />}
            count={data.counts.related}
          >
            {data.related.map((c) => (
              <RelatedRow key={c.id} c={c} onJump={onJumpToChat} />
            ))}
          </Section>
        )}
      </div>
    </aside>
  );
}


// ── Subcomponents ────────────────────────────────────────────────


function Section({
  title,
  icon,
  count,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-1.5 px-4 pb-1.5 pt-3">
        <span className="text-fg-2">{icon}</span>
        <span className="section-title">{title}</span>
        <span className="font-mono text-[10.5px] text-fg-3">{count}</span>
      </div>
      <ul className="px-2 pb-2">{children}</ul>
    </section>
  );
}


function TaskRow({ t }: { t: ConnTask }) {
  const tone = stateTone(t.state);
  return (
    <li>
      <Link
        to={`/tasks/${t.task_id}`}
        className="group block rounded-md px-2 py-1.5 transition-colors hover:bg-bg-2"
      >
        <div className="flex items-start gap-2">
          <span
            className={cn(
              "dot mt-1.5 shrink-0",
              tone === "ok" && "bg-ok",
              tone === "warn" && "bg-warn",
              tone === "err" && "bg-err",
              tone === "review" && "bg-review",
              tone === "info" && "bg-info",
              tone === "default" && "bg-fg-3",
            )}
          />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12px] font-medium text-fg-0 group-hover:text-accent">
              {t.title}
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
              <span className={cn("badge", `badge-${tone}`)}>
                {stateLabel(t.state)}
              </span>
              {t.jira_key && (
                <span className="badge badge-info font-mono">
                  {t.jira_key}
                </span>
              )}
              {t.has_pr && t.pr_number && (
                <span className="badge badge-review font-mono">
                  <GitPullRequest className="h-2.5 w-2.5" />#{t.pr_number}
                </span>
              )}
              {t.clarifications_pending > 0 && (
                <span className="badge badge-warn">
                  <AlertTriangle className="h-2.5 w-2.5" />
                  {t.clarifications_pending}
                </span>
              )}
            </div>
          </div>
        </div>
      </Link>
    </li>
  );
}


function AskRow({ a }: { a: ConnAsk }) {
  const tone = askTone(a.status);
  return (
    <li>
      <Link
        to="/collaboration#asks"
        className="block rounded-md px-2 py-1.5 transition-colors hover:bg-bg-2"
      >
        <div className="truncate text-[12px] text-fg-0">{a.question}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 font-mono text-[10.5px] text-fg-3">
          <span className={cn("badge", `badge-${tone}`)}>{a.status}</span>
          {a.target_squad && <span>→ {a.target_squad}</span>}
          {a.answered_at && <span>· {formatRelativeDate(a.answered_at)}</span>}
        </div>
      </Link>
    </li>
  );
}


function RelatedRow({
  c,
  onJump,
}: {
  c: ConnRelatedConv;
  onJump?: (id: string) => void;
}) {
  const inner = (
    <>
      <div className="truncate text-[12px] font-medium text-fg-0">
        {c.title || "Untitled chat"}
      </div>
      <div className="mt-0.5 flex items-center gap-2 font-mono text-[10.5px] text-fg-3">
        <span>{c.message_count} msg</span>
        <span>· {formatRelativeDate(c.updated_at)}</span>
      </div>
    </>
  );
  if (onJump) {
    return (
      <li>
        <button
          type="button"
          onClick={() => onJump(c.id)}
          className="block w-full rounded-md px-2 py-1.5 text-left transition-colors hover:bg-bg-2"
        >
          {inner}
        </button>
      </li>
    );
  }
  return (
    <li>
      <Link
        to={`/query?conversation=${c.id}`}
        className="block rounded-md px-2 py-1.5 transition-colors hover:bg-bg-2"
      >
        {inner}
      </Link>
    </li>
  );
}


// ── Tone + label helpers — same vocabulary as TaskQuickView ──────


function stateTone(state: string): "ok" | "warn" | "err" | "info" | "review" | "default" {
  if (state === "blocked") return "warn";
  if (state === "failed" || state === "cancelled") return "err";
  if (state === "pr_merged") return "ok";
  if (state === "success") return "review";
  if (state === "executing" || state === "verifying") return "info";
  if (
    state === "drafting_plan" ||
    state === "plan_ready" ||
    state === "discussing" ||
    state === "discuss_done"
  )
    return "review";
  return "default";
}


function stateLabel(state: string): string {
  return (
    {
      blocked: "Blocked",
      failed: "Failed",
      cancelled: "Cancelled",
      pr_merged: "Shipped",
      success: "In review",
      executing: "Executing",
      verifying: "Verifying",
      drafting_plan: "Planning",
      plan_ready: "Plan ready",
      discussing: "Discussing",
      discuss_done: "Discussion done",
    }[state] ?? state
  );
}


function askTone(
  status: string,
): "ok" | "warn" | "err" | "info" | "review" | "default" {
  if (status === "answered") return "ok";
  if (status === "escalated") return "err";
  if (status === "routed" || status === "sent") return "info";
  return "default";
}

void ExternalLink;
