/**
 * ChatTaskLineageCard — inline SDLC status card rendered next to the
 * chat message that spawned a task.
 *
 * Each card shows:
 *
 *   ✦ Task title (link to /tasks/<id>)
 *   PROJ-4521  ·  in review  ·  PR #123
 *   [Open Jira]  [Open PR]  [Continue task]
 *
 * The state badge is color-coded the same way the LinkedTasksBar
 * chips are. The card auto-refreshes via the parent's react-query
 * hook so a Jira move (state→Done after PR merge) reflects without
 * a manual reload.
 */

import { Link } from "react-router-dom";
import { GitPullRequest, Sparkles } from "lucide-react";
import type { ConvTaskLink } from "@/lib/api";
import { JiraKeyBadge } from "@/components/jira-key-badge";
import { cn } from "@/lib/utils";


/** Map task state → readable label + color class. */
const STATE_LABEL: Record<string, string> = {
  draft_intent: "draft",
  discussing: "discussing",
  discuss_done: "context ready",
  drafting_plan: "drafting plan",
  plan_ready: "plan ready",
  executing: "executing",
  verifying: "verifying",
  success: "in review",
  pr_merged: "merged",
  blocked: "blocked",
  cancelled: "cancelled",
};

const STATE_CLASS: Record<string, string> = {
  draft_intent: "badge badge-neutral",
  discussing: "badge badge-info",
  discuss_done: "badge badge-info",
  drafting_plan: "badge badge-info",
  plan_ready: "badge badge-review",
  executing: "badge badge-warn",
  verifying: "badge badge-warn",
  success: "badge badge-ok",
  pr_merged: "badge badge-ok",
  blocked: "badge badge-err",
  cancelled: "badge badge-neutral",
};


export function ChatTaskLineageCard({ link }: { link: ConvTaskLink }) {
  const stateLabel = STATE_LABEL[link.state] ?? link.state;
  const stateClass = STATE_CLASS[link.state] ?? STATE_CLASS.draft_intent;

  return (
    <div className="my-2 flex items-start gap-3 rounded-lg border border-accent/30 bg-accent/5 p-2.5">
      <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent text-accent-fg">
        <Sparkles className="h-3 w-3" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-[12px]">
          <Link
            to={`/tasks/${link.task_id}`}
            className="truncate font-medium text-foreground hover:underline"
          >
            {link.title}
          </Link>
          <span
            className={cn(
              "font-mono text-[10px] uppercase tracking-wider",
              stateClass,
            )}
          >
            {stateLabel}
          </span>
          {link.jira_key && (
            <JiraKeyBadge
              jiraKey={link.jira_key}
              url={link.jira_url}
              className="text-[10px]"
            />
          )}
          {link.pr_url && link.pr_number && (
            <a
              href={link.pr_url}
              target="_blank"
              rel="noreferrer"
              className="badge badge-review font-mono hover:opacity-80"
              title="Open PR on GitHub"
            >
              <GitPullRequest className="h-2.5 w-2.5" />
              #{link.pr_number}
            </a>
          )}
          {link.link_type && link.link_type !== "spawned_task" && (
            <span className="font-mono text-[10px] text-muted-foreground">
              ({link.link_type.replace(/_/g, " ")})
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
