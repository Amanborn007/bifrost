/**
 * JiraAuditPanel — collapsible audit trail of every Jira write
 * Bifrost has performed for a task, with per-row retract buttons.
 *
 * The spawn modal banner promises retract is available; this panel
 * is the UI affordance that calls the endpoint. It lives on the task
 * detail page and renders rows from ``task_jira_audit``.
 *
 * UX:
 *   ┌ Jira audit (3) ──────────────────────────── ▾ ┐
 *   │ create   PROJ-1234   ✓   2025-01-01 10:00     │
 *   │ transition PROJ-1234 To Do→In Progress ✓ ...   │
 *   │ comment  PROJ-1234   ✓   2025-01-01 10:20 [⤺] │
 *   └────────────────────────────────────────────────┘
 *
 * Retract posts a [bifrost-retracted] comment to Atlassian + marks
 * the audit row. The route layer also unlinks tasks.jira_key on
 * retracted creates so the operator can re-spawn into the right
 * project.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronRight, Undo2 } from "lucide-react";
import {
  ApiError,
  listTaskJiraAudit,
  retractJiraWrite,
  type JiraAuditRow,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { JiraKeyBadge } from "@/components/jira-key-badge";
import { cn } from "@/lib/utils";


export function JiraAuditPanel({ taskId }: { taskId: string }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["task-jira-audit", taskId],
    queryFn: () => listTaskJiraAudit(taskId),
    staleTime: 10_000,
  });

  const rows: JiraAuditRow[] = data ?? [];
  if (!isLoading && rows.length === 0) {
    return null;                              // hide entirely when nothing happened
  }

  return (
    <details
      className="rounded-lg border border-border bg-bg-1"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-[12px] font-medium text-fg-1 hover:text-fg-0">
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 text-fg-2 transition-transform",
            open && "rotate-90",
          )}
          strokeWidth={1.75}
        />
        <span>Jira audit</span>
        <span className="font-mono text-[11px] text-fg-3">
          ({rows.length})
        </span>
      </summary>
      <div className="divide-y divide-border border-t border-border">
        {rows.map((row) => (
          <AuditRow
            key={row.id}
            taskId={taskId}
            row={row}
            onRetracted={() => {
              void qc.invalidateQueries({ queryKey: ["task-jira-audit", taskId] });
              void qc.invalidateQueries({ queryKey: ["task", taskId] });
              void qc.invalidateQueries({ queryKey: ["conv-tasks"] });
            }}
          />
        ))}
      </div>
    </details>
  );
}


function AuditRow({
  taskId,
  row,
  onRetracted,
}: {
  taskId: string;
  row: JiraAuditRow;
  onRetracted: () => void;
}) {
  const retracted = !!row.retracted_at;
  const failed = !row.ok;

  const retractMut = useMutation({
    mutationFn: ({ reason }: { reason: string }) =>
      retractJiraWrite(taskId, row.id, reason),
    onSuccess: () => {
      toast.success("Retract posted to Atlassian + audit row marked");
      onRetracted();
    },
    onError: (err) => {
      const msg =
        err instanceof ApiError
          ? `Retract failed: ${err.status}`
          : String(err);
      toast.error(msg);
    },
  });

  const onRetract = () => {
    const reason = window.prompt(
      "Reason for retracting this Jira write?\n\n" +
      "(Posted as a [bifrost-retracted] comment on the issue.)",
      "",
    );
    if (reason === null) return;            // cancelled
    if (!reason.trim()) {
      toast.error("Reason is required");
      return;
    }
    retractMut.mutate({ reason: reason.trim() });
  };

  // Per-action transition summary line for the row body.
  const summary = (() => {
    if (row.action === "create") return "created Jira issue";
    if (row.action === "transition") {
      if (row.jira_state_before && row.jira_state_after) {
        return `${row.jira_state_before} → ${row.jira_state_after}`;
      }
      return `transition (${row.before_state ?? "?"} → ${row.after_state ?? "?"})`;
    }
    if (row.action === "comment") return "added comment";
    if (row.action === "retract") return "retracted";
    return row.action;
  })();

  return (
    <div
      className={cn(
        "flex items-start gap-2 px-3 py-2 text-[11.5px]",
        retracted && "bg-bg-2/30 opacity-60",
        failed && !retracted && "bg-err-soft",
      )}
    >
      <div
        className={cn(
          "mt-0.5 h-5 w-12 shrink-0 justify-center font-mono uppercase tracking-wider",
          row.action === "create"
            ? "badge badge-ok"
            : row.action === "transition"
              ? "badge badge-info"
              : row.action === "comment"
                ? "badge badge-review"
                : "badge badge-neutral",
        )}
      >
        {row.action.slice(0, 5)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-1.5">
          <span
            className={cn(
              "font-medium",
              retracted ? "line-through text-fg-3" : "text-fg-0",
            )}
          >
            {summary}
          </span>
          {row.jira_key && <JiraKeyBadge jiraKey={row.jira_key} url={row.jira_url} />}
          {failed && (
            <span className="badge badge-err font-mono uppercase">
              failed
            </span>
          )}
          {retracted && (
            <span className="badge badge-neutral font-mono uppercase">
              retracted
            </span>
          )}
        </div>
        <div className="mt-0.5 text-[10.5px] text-fg-3">
          <span>by {row.actor}</span>
          <span> · </span>
          <span>{new Date(row.created_at).toLocaleString()}</span>
          {row.error_message && (
            <span className="ml-2 text-err">
              · {row.error_message.slice(0, 120)}
            </span>
          )}
        </div>
      </div>
      {!retracted && (
        <Button
          size="xs"
          variant="ghost"
          disabled={retractMut.isPending}
          onClick={onRetract}
          title="Post a retraction comment to Jira + mark this audit row"
          className="ml-2 shrink-0"
        >
          <Undo2 className="h-3 w-3" strokeWidth={1.75} />
          Retract
        </Button>
      )}
    </div>
  );
}
