/**
 * TaskQuickView — right-panel summary for the master-detail Tasks page.
 *
 * Linear/Notion pattern: the operator scans the list, lands on a row,
 * sees enough context here to triage in seconds:
 *
 *   • Title + state badge + Jira/PR pills
 *   • Stat strip: phases done, plans, ask-backs, cost
 *   • Progress bar (when plans exist)
 *   • Lineage row: chat → task → Jira → PR
 *   • Footer actions: Open Jira, Open PR, "View full →" deep link
 *
 * For deep workflows (run discussion, transition state, retract Jira,
 * comment on assumptions), the "View full" link drops the operator
 * into ``/tasks/:id`` where TaskDetailPage lives. We deliberately
 * keep this view read-mostly: triage here, act there.
 */

import { Link } from "react-router-dom";
import {
  ArrowUpRight,
  Bot,
  ExternalLink,
  GitPullRequest,
  Hammer,
  MessageSquare,
  Sparkles,
} from "lucide-react";
import { cn, formatRelativeDate } from "@/lib/utils";
import type { TaskSummary } from "@/lib/api";
import { JiraKeyBadge } from "@/components/jira-key-badge";


export function TaskQuickView({ task }: { task: TaskSummary }) {
  const plansTotal = task.plans_total ?? 0;
  const plansCompleted = task.plans_completed ?? 0;
  const plansFailed = task.plans_failed ?? 0;
  const cost = task.total_cost_usd ?? 0;
  const askBacks = task.ask_back_count ?? 0;
  const progressPct =
    plansTotal > 0 ? Math.round((plansCompleted / plansTotal) * 100) : 0;
  const stateInfo = stateMeta(task.state);

  return (
    <div className="flex h-full flex-col">
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="border-b border-border px-6 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-[16px] font-semibold tracking-tight text-fg-0">
              {task.title}
            </h2>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <span className={cn("badge", stateInfo.badgeClass)}>
                {stateInfo.label}
              </span>
              {task.jira_key && (
                <JiraKeyBadge jiraKey={task.jira_key} url={task.jira_url} />
              )}
              {task.has_pr && task.pr_number && task.pr_url && (
                <a
                  href={task.pr_url}
                  target="_blank"
                  rel="noreferrer"
                  className="badge badge-review font-mono hover:opacity-80"
                  title="Open pull request"
                >
                  <GitPullRequest className="h-2.5 w-2.5" />
                  #{task.pr_number}
                </a>
              )}
              <span className="badge badge-neutral font-mono uppercase">
                {task.created_from}
              </span>
              <span className="font-mono text-[10.5px] text-fg-3">
                · updated {formatRelativeDate(task.updated_at)}
              </span>
            </div>
          </div>
          <Link
            to={`/tasks/${task.id}`}
            className="inline-flex shrink-0 items-center gap-1 rounded-md bg-accent px-2.5 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover"
            title="Open the full task page (Enter)"
          >
            Open full
            <ArrowUpRight className="h-3 w-3" strokeWidth={2} />
          </Link>
        </div>
      </header>

      {/* ── Stat strip ─────────────────────────────────────────── */}
      <section className="grid grid-cols-2 gap-0 border-b border-border sm:grid-cols-4">
        <Stat
          label="Phases"
          value={
            task.phases_total > 0
              ? `${task.phases_done}/${task.phases_total}`
              : "—"
          }
        />
        <Stat
          label="Plans"
          value={plansTotal > 0 ? `${plansCompleted}/${plansTotal}` : "—"}
          tone={plansFailed > 0 ? "warn" : "default"}
          sub={plansFailed > 0 ? `${plansFailed} failed` : undefined}
        />
        <Stat
          label="Ask-backs"
          value={askBacks.toString()}
          tone={
            task.clarifications_pending > 0 ? "warn" : "default"
          }
          sub={
            task.clarifications_pending > 0
              ? `${task.clarifications_pending} open`
              : undefined
          }
        />
        <Stat
          label="Cost"
          value={`$${cost.toFixed(cost < 1 ? 3 : 2)}`}
          tone={cost >= 0.5 ? "warn" : "default"}
        />
      </section>

      {/* ── Progress bar ───────────────────────────────────────── */}
      {plansTotal > 0 && (
        <section className="border-b border-border px-6 py-3">
          <div className="mb-1.5 flex items-baseline justify-between">
            <span className="section-title">Plan progress</span>
            <span className="font-mono text-[11px] tabular-nums text-fg-1">
              {progressPct}%
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-bg-2">
            <div
              className={cn(
                "h-full transition-[width] duration-500",
                plansFailed > 0 ? "bg-warn" : "bg-accent",
              )}
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </section>
      )}

      {/* ── Lineage row ────────────────────────────────────────── */}
      <section className="border-b border-border px-6 py-4">
        <div className="section-title mb-2">Lineage</div>
        <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
          {task.created_from === "chat" && (
            <>
              <LineageStep
                icon={<MessageSquare className="h-3 w-3" strokeWidth={1.75} />}
                label="Chat"
                tone="info"
              />
              <Connector />
            </>
          )}
          <LineageStep
            icon={<Bot className="h-3 w-3" strokeWidth={1.75} />}
            label="Task"
            tone="default"
            current
          />
          {task.jira_key && (
            <>
              <Connector />
              <LineageStep
                icon={<ExternalLink className="h-3 w-3" strokeWidth={1.75} />}
                label={task.jira_key}
                tone="info"
                href={task.jira_url ?? undefined}
              />
            </>
          )}
          {task.has_pr && task.pr_number && task.pr_url && (
            <>
              <Connector />
              <LineageStep
                icon={<GitPullRequest className="h-3 w-3" strokeWidth={1.75} />}
                label={`PR #${task.pr_number}`}
                tone="review"
                href={task.pr_url}
              />
            </>
          )}
          {!task.has_pr && task.has_worktree && (
            <>
              <Connector />
              <LineageStep
                icon={<Hammer className="h-3 w-3" strokeWidth={1.75} />}
                label="Worktree"
                tone="default"
              />
            </>
          )}
        </div>
      </section>

      {/* ── Footer hint ────────────────────────────────────────── */}
      <footer className="mt-auto border-t border-border bg-bg-0 px-6 py-3 text-[11px] text-fg-3">
        <div className="flex items-center justify-between">
          <span>
            <Sparkles className="mr-1 inline h-3 w-3" strokeWidth={1.75} />
            Triage here · act on the full page
          </span>
          <span>
            <kbd className="kbd">↵</kbd>{" "}
            <span className="ml-1">to open full</span>
          </span>
        </div>
      </footer>
    </div>
  );
}


// ── Subcomponents ────────────────────────────────────────────────


function Stat({
  label,
  value,
  sub,
  tone = "default",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "default" | "warn" | "ok";
}) {
  const valueClass = {
    default: "text-fg-0",
    warn: "text-warn",
    ok: "text-ok",
  }[tone];
  return (
    <div className="border-r border-border px-4 py-3 last:border-r-0">
      <div className="section-title">{label}</div>
      <div
        className={cn(
          "mt-1 font-mono text-[18px] font-semibold tabular-nums",
          valueClass,
        )}
      >
        {value}
      </div>
      {sub && <div className="text-[10.5px] text-fg-3">{sub}</div>}
    </div>
  );
}


function LineageStep({
  icon,
  label,
  tone,
  current,
  href,
}: {
  icon: React.ReactNode;
  label: string;
  tone: "info" | "review" | "default";
  current?: boolean;
  href?: string;
}) {
  const toneClass =
    tone === "info"
      ? "badge-info"
      : tone === "review"
        ? "badge-review"
        : current
          ? "badge-neutral ring-1 ring-accent/40"
          : "badge-neutral";
  const inner = (
    <span
      className={cn(
        "badge font-mono",
        toneClass,
        current && "font-semibold",
      )}
    >
      {icon}
      {label}
    </span>
  );
  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer" className="hover:opacity-80">
        {inner}
      </a>
    );
  }
  return inner;
}


function Connector() {
  return (
    <span aria-hidden className="text-fg-4">
      →
    </span>
  );
}


// ── State metadata — same source of truth as the bucket rail ──────


function stateMeta(
  state: string,
): { label: string; badgeClass: string } {
  // Map task states to the global tone vocabulary.
  if (state === "blocked") return { label: "Blocked", badgeClass: "badge-warn" };
  if (state === "failed") return { label: "Failed", badgeClass: "badge-err" };
  if (state === "cancelled") return { label: "Cancelled", badgeClass: "badge-err" };
  if (state === "pr_merged") return { label: "Shipped", badgeClass: "badge-ok" };
  if (state === "success") return { label: "In review", badgeClass: "badge-review" };
  if (state === "executing") return { label: "Executing", badgeClass: "badge-info" };
  if (state === "verifying") return { label: "Verifying", badgeClass: "badge-info" };
  if (state === "drafting_plan") return { label: "Planning", badgeClass: "badge-review" };
  if (state === "plan_ready") return { label: "Plan ready", badgeClass: "badge-review" };
  if (state === "discussing") return { label: "Discussing", badgeClass: "badge-review" };
  if (state === "discuss_done") return { label: "Discussion done", badgeClass: "badge-review" };
  return { label: state, badgeClass: "badge-neutral" };
}
