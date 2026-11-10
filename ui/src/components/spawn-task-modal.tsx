/**
 * SpawnTaskModal — the only mutation point for chat→task→Jira.
 *
 * Opens when the operator clicks "Make this a task" in the chat.
 * Calls /spawn-task/preview to populate suggested defaults (title,
 * acceptance criteria, project resolved from scope, issue type),
 * lets the operator edit, then on "Create + go" calls /spawn-task
 * with create_jira_now=true so the Jira ticket is born inline with
 * the task.
 *
 * Why a modal: the prior silent-spawn UX created tasks the moment
 * the operator clicked. Now that the same click also writes to Jira,
 * we need a confirm step — the spawn modal is the seam between
 * "this is what I'm thinking" and "here's the audit-loaded ticket
 * the team will see in their board".
 */

import { useEffect, useState } from "react";
import { Loader2, ShieldCheck, ShieldAlert, X } from "lucide-react";
import {
  ApiError,
  previewSpawnTask,
  spawnTaskFromChat,
  type SpawnPreview,
  type TaskSummary,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const ISSUE_TYPES = ["Task", "Story", "Bug"] as const;
type IssueType = (typeof ISSUE_TYPES)[number];

interface Props {
  conversationId: string;
  messageId?: string | null;
  onClose: () => void;
  onSpawned: (task: TaskSummary) => void;
}

export function SpawnTaskModal({
  conversationId,
  messageId,
  onClose,
  onSpawned,
}: Props) {
  const [preview, setPreview] = useState<SpawnPreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Form state
  const [title, setTitle] = useState("");
  const [acceptanceCriteria, setAcceptanceCriteria] = useState<string[]>([]);
  const [projectKey, setProjectKey] = useState<string>("");
  const [issueType, setIssueType] = useState<IssueType>("Task");
  const [createJiraNow, setCreateJiraNow] = useState(true);

  // Submit state
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Load preview on mount
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const p = await previewSpawnTask(conversationId, {
          message_id: messageId ?? null,
        });
        if (cancelled) return;
        setPreview(p);
        setTitle(p.suggested_title);
        setAcceptanceCriteria(p.suggested_acceptance_criteria);
        setProjectKey(p.resolved_project_key ?? "");
        const it = ISSUE_TYPES.includes(p.suggested_issue_type as IssueType)
          ? (p.suggested_issue_type as IssueType)
          : "Task";
        setIssueType(it);
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError ? `Preview failed: ${err.status}` : String(err);
        setLoadError(msg);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, messageId]);

  const updateAc = (i: number, value: string) => {
    setAcceptanceCriteria((prev) => prev.map((s, idx) => (idx === i ? value : s)));
  };
  const removeAc = (i: number) => {
    setAcceptanceCriteria((prev) => prev.filter((_, idx) => idx !== i));
  };
  const addAc = () => {
    setAcceptanceCriteria((prev) => [...prev, ""]);
  };

  const onSubmit = async () => {
    if (!title.trim()) {
      setSubmitError("Title is required.");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      const cleanAc = acceptanceCriteria
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      const task = await spawnTaskFromChat(conversationId, {
        message_id: messageId ?? null,
        title: title.trim(),
        acceptance_criteria: cleanAc,
        project_key: projectKey.trim() || null,
        issue_type: issueType,
        create_jira_now: createJiraNow && !!projectKey.trim(),
      });
      onSpawned(task);
      onClose();
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Spawn failed: ${err.status} ${typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)}`
          : String(err);
      setSubmitError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-xl rounded-lg border border-border bg-popover shadow-xl">
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-base font-semibold text-foreground">
            Spawn task from this conversation
          </h3>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:bg-muted"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto p-4">
          {loadError ? (
            <div className="rounded border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
              {loadError}
            </div>
          ) : !preview ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-primary" strokeWidth={1.75} />
              <span className="ml-2 text-sm text-muted-foreground">
                Suggesting defaults…
              </span>
            </div>
          ) : (
            <div className="space-y-4">
              {/* Title */}
              <Field label="Title" hint="suggested from chat — edit if needed">
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
                  placeholder="e.g. Add idempotency to the carrier webhook handler"
                />
              </Field>

              {/* Project */}
              <Field
                label="Jira project"
                hint={
                  preview.resolved_project_key
                    ? `auto-resolved from ${preview.resolved_project_source}: ${preview.resolved_project_reason}`
                    : preview.resolved_project_source === "scope_tied"
                      ? `tied across ${preview.candidate_projects.join(", ")} — pick one`
                      : "no scope match — pick from full list"
                }
              >
                <select
                  value={projectKey}
                  onChange={(e) => setProjectKey(e.target.value)}
                  className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
                >
                  <option value="">(no Jira ticket — local-only)</option>
                  {preview.candidate_projects.length > 0 && (
                    <optgroup label="Suggested (matched scope)">
                      {preview.candidate_projects.map((p) => (
                        <option key={`c-${p}`} value={p}>
                          {p}
                        </option>
                      ))}
                    </optgroup>
                  )}
                  <optgroup label="All projects">
                    {preview.all_projects.map((p) => (
                      <option key={`a-${p}`} value={p}>
                        {p}
                      </option>
                    ))}
                  </optgroup>
                </select>
              </Field>

              {/* Issue type */}
              <Field label="Issue type">
                <div className="flex gap-2">
                  {ISSUE_TYPES.map((t) => (
                    <label
                      key={t}
                      className={cn(
                        "flex-1 cursor-pointer rounded border px-3 py-1.5 text-center text-sm transition-colors",
                        issueType === t
                          ? "border-primary bg-primary/10 text-foreground"
                          : "border-border bg-background text-muted-foreground hover:bg-muted",
                      )}
                    >
                      <input
                        type="radio"
                        name="issue_type"
                        value={t}
                        checked={issueType === t}
                        onChange={() => setIssueType(t)}
                        className="sr-only"
                      />
                      {t}
                    </label>
                  ))}
                </div>
              </Field>

              {/* Acceptance criteria */}
              <Field
                label="Acceptance criteria"
                hint="suggested from chat — edit, add, or remove"
              >
                <div className="space-y-1.5">
                  {acceptanceCriteria.map((ac, i) => (
                    <div key={i} className="flex items-center gap-1.5">
                      <input
                        type="text"
                        value={ac}
                        onChange={(e) => updateAc(i, e.target.value)}
                        placeholder="One-line testable success condition"
                        className="flex-1 rounded border border-border bg-background px-2 py-1 text-[13px] focus:border-primary focus:outline-none"
                      />
                      <button
                        type="button"
                        aria-label="Remove"
                        onClick={() => removeAc(i)}
                        className="rounded p-1 text-muted-foreground hover:bg-muted"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={addAc}
                    className="text-xs text-muted-foreground hover:text-foreground"
                  >
                    + Add criterion
                  </button>
                </div>
              </Field>

              {/* Create-now toggle */}
              <Field label="Create Jira ticket now?">
                <label className="flex items-center gap-2 text-sm text-foreground">
                  <input
                    type="checkbox"
                    checked={createJiraNow && !!projectKey}
                    disabled={!projectKey}
                    onChange={(e) => setCreateJiraNow(e.target.checked)}
                  />
                  <span>
                    {projectKey
                      ? createJiraNow
                        ? `Yes — write ${projectKey}-NEW to Atlassian on submit`
                        : "No — keep local; sync to Jira later from task detail"
                      : "Pick a project first to enable"}
                  </span>
                </label>
              </Field>

              {/* Banner: explicit auth + retract assurance */}
              <div className="flex items-start gap-2 rounded bg-ok-soft p-2 text-xs text-ok">
                <ShieldCheck className="mt-[1px] h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
                <div>
                  This is the only step that writes to Jira. Every Jira
                  write is logged in the task's audit trail and can be
                  reversed via "Retract" on the task detail page.
                </div>
              </div>

              {submitError && (
                <div className="flex items-start gap-2 rounded border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                  <ShieldAlert className="mt-[1px] h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
                  <div className="whitespace-pre-wrap">{submitError}</div>
                </div>
              )}
            </div>
          )}
        </div>
        <footer className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="default"
            onClick={onSubmit}
            disabled={submitting || !preview || !title.trim()}
          >
            {submitting && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            {createJiraNow && projectKey ? "Create + go" : "Create local task"}
          </Button>
        </footer>
      </div>
    </div>
  );
}


function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <label className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          {label}
        </label>
        {hint && (
          <span className="ml-2 text-[10px] text-muted-foreground/70">
            {hint}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}
