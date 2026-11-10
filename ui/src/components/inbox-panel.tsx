/**
 * InboxPanel — "what's mine to do right now."
 *
 * The dashboard hero. Operator opens the app and immediately sees
 * the actionable surface across the whole product: escalated asks
 * they posted, tasks they own that are blocked, asks routed to their
 * squad, PRs awaiting their review, open work in flight.
 *
 * Single backend call (`/collaboration/inbox`) → five sections.
 * Empty sections collapse so the operator's eyes only land on
 * non-empty rows. Total-actionable badge in the corner shows the
 * grand-sum so the value is comparable across days.
 *
 * Visual language: the five sections map 1:1 to the global tone
 * tokens (err/warn/info/review/ok) declared in ``index.css``. Every
 * status pill is a ``.badge-{tone}`` primitive — no hardcoded
 * Tailwind colour classes. This is what keeps the inbox, sidebar
 * badges, command palette, and learning page visually coherent.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertTriangle, GitPullRequest, Hourglass, Inbox, Sparkles, Zap,
  CheckCircle2, RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { JiraKeyBadge } from "@/components/jira-key-badge";
import { getInbox, type InboxAsk, type InboxTask, type InboxReviewPR } from "@/lib/api";


export function InboxPanel({ email, refetchMs = 30_000 }: { email?: string; refetchMs?: number }) {
  const inbox = useQuery({
    queryKey: ["inbox", email ?? "me"],
    queryFn: () => getInbox(email),
    refetchInterval: refetchMs,
    staleTime: 10_000,
  });

  if (inbox.isLoading) {
    return (
      <div className="rounded-xl border border-border bg-bg-1 p-6">
        <div className="flex items-center gap-2 text-[12.5px] text-fg-2">
          <RefreshCw className="h-3.5 w-3.5 animate-spin" strokeWidth={1.75} />
          Loading your inbox…
        </div>
      </div>
    );
  }

  const data = inbox.data;
  if (!data) return null;
  const c = data.counts;
  const isClean = c.total_actionable === 0;

  return (
    <section className="rounded-xl border border-border bg-bg-1 overflow-hidden">
      {/* Header — accent-tinted gradient + live-pulse dot signals
          "this view is auto-refreshing in the background." */}
      <header className="flex items-center justify-between border-b border-border bg-gradient-to-r from-accent/5 to-transparent px-5 py-3">
        <div className="flex items-center gap-2.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-accent-fg shadow-sm">
            <Inbox className="h-4 w-4" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="flex items-center gap-2 text-[14px] font-semibold tracking-tight text-fg-0">
              Your inbox
              <span
                className={cn("dot bg-accent", !inbox.isFetching && "live-pulse")}
                aria-hidden
              />
            </h2>
            <div className="text-[11px] text-fg-3">
              {data.email} · {isClean ? "all clear" : `${c.total_actionable} actionable item${c.total_actionable === 1 ? "" : "s"}`}
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={() => inbox.refetch()}
          className="rounded-md px-2 py-1 text-[11px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          title="Refresh"
        >
          <RefreshCw className={cn("inline h-3 w-3 mr-1", inbox.isFetching && "animate-spin")} strokeWidth={1.75} />
          Refresh
        </button>
      </header>

      {isClean ? (
        <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-ok-soft text-ok">
            <CheckCircle2 className="h-6 w-6" strokeWidth={1.5} />
          </div>
          <div className="text-[14px] font-medium text-fg-0">Inbox zero — nothing waits on you</div>
          <div className="max-w-md text-[12px] text-fg-3">
            No escalated asks, no blocked tasks, no PRs in review, no asks routed to your squad.
            Spawn a task or open a chat to start something new.
          </div>
        </div>
      ) : (
        <div className="divide-y divide-border">
          <SectionRow
            label="Escalated"
            count={c.escalated}
            icon={<AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />}
            tone="err"
          >
            {data.escalated_asks.map((a) => <AskLine key={a.id} a={a} />)}
          </SectionRow>

          <SectionRow
            label="Blocked"
            count={c.blocked}
            icon={<Zap className="h-3.5 w-3.5" strokeWidth={1.75} />}
            tone="warn"
          >
            {data.blocked_tasks.map((t) => <TaskLine key={t.id} t={t} />)}
          </SectionRow>

          <SectionRow
            label="Awaiting your squad"
            count={c.awaiting_me}
            icon={<Hourglass className="h-3.5 w-3.5" strokeWidth={1.75} />}
            tone="info"
          >
            {data.awaiting_my_squad.map((a) => <AskLine key={a.id} a={a} />)}
          </SectionRow>

          <SectionRow
            label="PRs awaiting review"
            count={c.in_review}
            icon={<GitPullRequest className="h-3.5 w-3.5" strokeWidth={1.75} />}
            tone="review"
          >
            {data.my_prs_in_review.map((pr) => <ReviewPRLine key={pr.pr_url} pr={pr} />)}
          </SectionRow>

          <SectionRow
            label="Open work"
            count={c.open_work}
            icon={<Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />}
            tone="ok"
          >
            {data.open_work.map((t) => <TaskLine key={t.id} t={t} />)}
          </SectionRow>
        </div>
      )}
    </section>
  );
}


// ── Section + line components ──────────────────────────────────────


type Tone = "ok" | "warn" | "err" | "info" | "review";

function SectionRow({
  label, count, icon, tone, children,
}: {
  label: string;
  count: number;
  icon: React.ReactNode;
  tone: Tone;
  children: React.ReactNode;
}) {
  if (count === 0) return null;            // collapse empty sections
  // Tones map 1:1 to global badge primitives — single vocabulary for
  // the inbox, sidebar badges, palette hints, and learning page.
  const toneClass: Record<Tone, string> = {
    ok: "badge-ok",
    warn: "badge-warn",
    err: "badge-err",
    info: "badge-info",
    review: "badge-review",
  };
  return (
    <div className="px-5 py-3">
      <div className="mb-2 flex items-center gap-2">
        <span
          className={cn(
            "badge inline-flex items-center gap-1 rounded-full px-2 py-0.5 uppercase tracking-wider",
            toneClass[tone],
          )}
        >
          {icon}
          {label}
        </span>
        <span className="font-mono text-[11px] text-fg-3">{count}</span>
      </div>
      <ul className="space-y-1">{children}</ul>
    </div>
  );
}


function TaskLine({ t }: { t: InboxTask }) {
  return (
    <li className="flex items-center gap-2 truncate text-[12.5px]">
      <Link to={`/tasks/${t.id}`} className="truncate font-medium text-fg-0 hover:underline">
        {t.title}
      </Link>
      <span className="font-mono text-[10px] text-fg-3">{t.state}</span>
      {t.jira_key && <JiraKeyBadge jiraKey={t.jira_key} url={t.jira_url} />}
      {t.pr_number && t.pr_url && (
        <a
          href={t.pr_url}
          target="_blank"
          rel="noreferrer"
          className="badge badge-review font-mono hover:opacity-80"
          title="Open PR"
        >
          <GitPullRequest className="h-2.5 w-2.5" />
          #{t.pr_number}
        </a>
      )}
    </li>
  );
}


function AskLine({ a }: { a: InboxAsk }) {
  return (
    <li className="flex items-start gap-2 text-[12.5px]">
      <div className="min-w-0 flex-1">
        <div className="truncate text-fg-0">{a.question}</div>
        <div className="mt-0.5 font-mono text-[10.5px] text-fg-3">
          {a.target_squad ? `→ ${a.target_squad}` : "no route"}
          {a.target_slack_channel && <span> · {a.target_slack_channel}</span>}
          {" · "}
          <span>{a.status}</span>
          {" · by "}
          <span>{a.asker_email.split("@")[0]}</span>
        </div>
      </div>
      <Link
        to="/collaboration#asks"
        className="rounded px-1.5 py-0.5 text-[10.5px] text-accent hover:underline"
      >
        Open
      </Link>
    </li>
  );
}


function ReviewPRLine({ pr }: { pr: InboxReviewPR }) {
  return (
    <li className="flex items-center gap-2 text-[12.5px]">
      <a
        href={pr.pr_url}
        target="_blank"
        rel="noreferrer"
        className="truncate font-medium text-fg-0 hover:underline"
      >
        {pr.title || pr.pr_url}
        {pr.pr_number && <span className="ml-1 font-mono text-[10px] text-fg-3">#{pr.pr_number}</span>}
      </a>
      {pr.task_id && (
        <Link to={`/tasks/${pr.task_id}`} className="text-[10.5px] text-fg-2 hover:underline">
          task →
        </Link>
      )}
    </li>
  );
}
