import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  ClipboardList,
  Plus,
  Sparkles,
  Loader2,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Search,
  X,
  ExternalLink,
  GitPullRequest,
  Circle,
  History,
} from "lucide-react";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";
import {
  ApiError,
  autoArchiveTasks,
  cancelTask,
  createTask,
  listTasks,
  getTaskStats,
  taskFromJira,
  type TaskSummary,
} from "@/lib/api";
import { MasterDetail, type Bucket } from "@/components/master-detail";
import { TaskQuickView } from "@/components/task-quickview";

const BUCKET_TO_STATE: Record<string, string | undefined> = {
  working: "open",
  // "needs-you" is handled with a dual query (open+blocked merged),
  // so no single-state mapping here.
  review: "success",
  shipped: "pr_merged",
  // Not shown in main rail — reachable via the history link.
  closed: "closed",
  archived: undefined,
};

export function TasksPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const bucket = searchParams.get("bucket") ?? "working";
  const [composerOpen, setComposerOpen] = useState(false);
  const [fromJiraOpen, setFromJiraOpen] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");

  useEffect(() => {
    const id = window.setTimeout(
      () => setDebouncedSearch(searchInput.trim()),
      300,
    );
    return () => window.clearTimeout(id);
  }, [searchInput]);

  // Always keep working tasks loaded so we can count clarifications_pending
  // for the "Needs You" badge without an extra round-trip when that bucket
  // is active. React Query deduplicates this against the active "working"
  // bucket query when they share the same key.
  const workingTasksQ = useQuery({
    queryKey: ["tasks", "working", ""],
    queryFn: () => listTasks("open", { archived_only: false }),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const tasks = useQuery({
    queryKey: ["tasks", bucket, debouncedSearch],
    queryFn: async () => {
      if (bucket === "needs-you") {
        // Merge: blocked tasks + open tasks that have pending clarifications.
        const [openTasks, blockedTasks] = await Promise.all([
          listTasks("open", { archived_only: false, q: debouncedSearch || undefined }),
          listTasks("blocked", { archived_only: false, q: debouncedSearch || undefined }),
        ]);
        const withClarifications = openTasks.filter(
          (t) => t.clarifications_pending > 0,
        );
        const blockedIds = new Set(blockedTasks.map((t) => t.id));
        return [
          ...blockedTasks,
          ...withClarifications.filter((t) => !blockedIds.has(t.id)),
        ];
      }
      return listTasks(BUCKET_TO_STATE[bucket], {
        archived_only: false,
        q: debouncedSearch || undefined,
      });
    },
    refetchInterval: debouncedSearch ? false : 15_000,
    placeholderData: (prev) => prev,
  });

  const stats = useQuery({
    queryKey: ["task-stats"],
    queryFn: () => getTaskStats(7),
    refetchInterval: 30_000,
  });

  const s = stats.data;
  const prMerged = s?.pr_merged ?? 0;

  // Accurate "needs input" count: blocked state + tasks with open clarifications.
  const clarificationsCount = useMemo(
    () =>
      (workingTasksQ.data ?? []).filter((t) => t.clarifications_pending > 0)
        .length,
    [workingTasksQ.data],
  );
  const needsInputCount = (s?.blocked ?? 0) + clarificationsCount;

  // Pattern that identifies smoke / layer-execution / learning test tasks.
  const TEST_PATTERN = /\b(smoke|L\d+\s+|L\d+-|learning smoke|Brief [AB] -)/i;

  const runCleanup = async () => {
    try {
      const all = await listTasks("open", { archived_only: false });
      const testTasks = all.filter((t) => TEST_PATTERN.test(t.title));
      if (testTasks.length === 0) {
        toast.info("No test tasks found.");
        return;
      }
      const ok = window.confirm(
        `Cancel and archive ${testTasks.length} test task(s)?\n\n` +
          testTasks
            .slice(0, 8)
            .map((t) => `• ${t.title}`)
            .join("\n") +
          (testTasks.length > 8 ? `\n…and ${testTasks.length - 8} more` : ""),
      );
      if (!ok) return;
      await Promise.allSettled(testTasks.map((t) => cancelTask(t.id)));
      await autoArchiveTasks({ older_than_days: 0, dry_run: false });
      toast.success(`Removed ${testTasks.length} test task${testTasks.length !== 1 ? "s" : ""}`);
      void qc.invalidateQueries({ queryKey: ["tasks"] });
      void qc.invalidateQueries({ queryKey: ["task-stats"] });
    } catch (err) {
      toast.error(`Cleanup failed: ${describeError(err)}`);
    }
  };

  const buckets: Bucket[] = [
    {
      id: "working",
      label: "Working",
      icon: <Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: s?.active,
      tone: "default",
      hint: "Tasks actively in progress",
    },
    {
      id: "needs-you",
      label: "Needs You",
      icon: <AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: needsInputCount || undefined,
      tone: "warn",
      hint: "Open questions + blocked tasks",
    },
    {
      id: "review",
      label: "In Review",
      icon: <GitPullRequest className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: s?.success,
      tone: "review",
      hint: "PR open — awaiting your review",
    },
    {
      id: "shipped",
      label: "Shipped",
      icon: <CheckCircle2 className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: prMerged,
      tone: "ok",
      hint: "PR merged and done",
    },
  ];

  const taskList = tasks.data ?? [];
  const highlightTerms = useMemo(
    () => debouncedSearch.split(/\s+/).filter(Boolean),
    [debouncedSearch],
  );

  return (
    <div className="mx-auto flex h-full max-w-[1400px] flex-col px-8 py-6">
      <header className="mb-4 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-fg-0">
            Tasks
          </h1>
          <p className="mt-1 max-w-xl text-[13.5px] text-fg-2">
            Tell Bifrost what to build. It plans, codes, and opens a PR for
            your review.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setFromJiraOpen(true)}
            className="inline-flex items-center gap-2 rounded-md border border-border bg-bg-1 px-3 py-2 text-[13px] font-medium text-fg-1 hover:bg-bg-2"
            title="Adopt an existing Jira ticket as a Bifrost task"
          >
            <ExternalLink className="h-3.5 w-3.5" strokeWidth={1.75} />
            From Jira
          </button>
          <button
            onClick={() => setComposerOpen((v) => !v)}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-[13px] font-medium text-accent-fg hover:bg-accent-hover"
          >
            <Plus className="h-4 w-4" strokeWidth={2} />
            New task
          </button>
        </div>
      </header>

      {composerOpen && (
        <NewTaskComposer
          onClose={() => setComposerOpen(false)}
          onCreated={(taskId) => {
            setComposerOpen(false);
            void qc.invalidateQueries({ queryKey: ["tasks"] });
            navigate(`/tasks/${taskId}`);
          }}
        />
      )}

      {fromJiraOpen && (
        <FromJiraModal
          onClose={() => setFromJiraOpen(false)}
          onCreated={(taskId) => {
            setFromJiraOpen(false);
            void qc.invalidateQueries({ queryKey: ["tasks"] });
            navigate(`/tasks/${taskId}`);
          }}
        />
      )}

      {/* Summary line — single row instead of 4 stat tiles */}
      {s && (
        <p className="mb-3 text-[12.5px] text-fg-2">
          {s.active > 0 ? (
            <>
              <span className="font-medium text-fg-0">{s.active}</span>{" "}
              running
            </>
          ) : (
            "Nothing running"
          )}
          {needsInputCount > 0 && (
            <>
              {" · "}
              <span className="font-medium text-warn">{needsInputCount}</span>{" "}
              <span className="text-warn">
                need{needsInputCount === 1 ? "s" : ""} your input
              </span>
            </>
          )}
          {prMerged > 0 && (
            <>
              {" · "}
              <span className="font-medium text-ok">{prMerged}</span> shipped
              this week
            </>
          )}
        </p>
      )}

      <MasterDetail<TaskSummary>
        className="min-h-0 flex-1"
        buckets={buckets}
        defaultBucketId="working"
        items={taskList}
        itemKey={(t) => t.id}
        loading={tasks.isLoading}
        onOpenFull={(t) => navigate(`/tasks/${t.id}`)}
        filterBar={
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3"
              strokeWidth={1.75}
            />
            <input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setSearchInput("");
              }}
              placeholder="Search tasks or Jira key…"
              className="w-full rounded-md border border-border bg-bg-1 py-1.5 pl-8 pr-9 text-[12px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
            />
            {searchInput && (
              <button
                onClick={() => setSearchInput("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-fg-3 hover:bg-bg-2 hover:text-fg-0"
                title="Clear"
              >
                <X className="h-3 w-3" strokeWidth={2} />
              </button>
            )}
          </div>
        }
        renderItem={(t) => (
          <TaskListItem t={t} highlightTerms={highlightTerms} />
        )}
        renderDetail={(t) => (t ? <TaskQuickView task={t} /> : null)}
        emptyList={
          <TaskListEmpty
            bucket={bucket}
            hasSearch={!!debouncedSearch}
            onNew={() => setComposerOpen(true)}
            onClearSearch={() => setSearchInput("")}
          />
        }
        emptyDetail={
          <div className="flex flex-col items-center gap-2 text-center">
            <p className="text-[12.5px] text-fg-2">
              Select a task to see details
            </p>
            <p className="text-[11px] text-fg-3">
              Press <kbd className="kbd">Enter</kbd> to open the full page
            </p>
          </div>
        }
      />

      {/* Footer row: history + cleanup */}
      {bucket !== "closed" && bucket !== "archived" && (
        <div className="mt-2 flex items-center justify-between">
          <button
            onClick={runCleanup}
            className="inline-flex items-center gap-1.5 text-[11px] text-fg-3 hover:text-warn"
            title="Cancel and archive tasks with 'smoke', 'L# ', or test patterns in their name"
          >
            <XCircle className="h-3 w-3" strokeWidth={1.75} />
            Clean up test tasks
          </button>
          <button
            onClick={() => navigate("/tasks?bucket=closed")}
            className="inline-flex items-center gap-1.5 text-[11px] text-fg-3 hover:text-fg-1"
          >
            <History className="h-3 w-3" strokeWidth={1.75} />
            View closed &amp; archived tasks
          </button>
        </div>
      )}
    </div>
  );
}


/* ── Task list row ─────────────────────────────────────────── */

function TaskListItem({
  t,
  highlightTerms,
}: {
  t: TaskSummary;
  highlightTerms: string[];
}) {
  const phrase = taskStatusPhrase(t);
  const isNeedsInput =
    t.state === "blocked" || t.clarifications_pending > 0;
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <TaskStatusDot state={t.state} needsInput={isNeedsInput} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-medium text-fg-0">
          {_highlight(t.title, highlightTerms)}
        </p>
        <div className="mt-0.5 flex items-center gap-2 text-[11.5px] text-fg-3">
          <span className={cn(isNeedsInput && "text-warn")}>{phrase}</span>
          {t.jira_key && (
            <span className="font-mono text-[10.5px] text-info">
              {t.jira_key}
            </span>
          )}
          {t.has_pr && t.pr_number && (
            <span className="font-mono text-[10.5px] text-review">
              PR#{t.pr_number}
            </span>
          )}
        </div>
      </div>
      <span className="shrink-0 self-start pt-0.5 font-mono text-[10px] text-fg-3">
        {formatRelativeDate(t.updated_at)}
      </span>
    </div>
  );
}

function TaskStatusDot({
  state,
  needsInput,
}: {
  state: string;
  needsInput: boolean;
}) {
  if (needsInput) {
    return (
      <AlertTriangle
        className="mt-1 h-3.5 w-3.5 shrink-0 text-warn"
        strokeWidth={2}
      />
    );
  }
  if (state === "pr_merged") {
    return (
      <CheckCircle2
        className="mt-1 h-3.5 w-3.5 shrink-0 text-ok"
        strokeWidth={2}
      />
    );
  }
  if (state === "success") {
    return (
      <GitPullRequest
        className="mt-1 h-3.5 w-3.5 shrink-0 text-review"
        strokeWidth={1.75}
      />
    );
  }
  if (state === "cancelled") {
    return (
      <XCircle
        className="mt-1 h-3.5 w-3.5 shrink-0 text-fg-3"
        strokeWidth={1.75}
      />
    );
  }
  if (
    state === "executing" ||
    state === "verifying" ||
    state === "discussing" ||
    state === "drafting_plan"
  ) {
    return (
      <Loader2
        className="mt-1 h-3.5 w-3.5 shrink-0 animate-spin text-accent"
        strokeWidth={1.75}
      />
    );
  }
  return (
    <Circle
      className="mt-1 h-3.5 w-3.5 shrink-0 text-fg-3"
      strokeWidth={1.75}
    />
  );
}

function taskStatusPhrase(t: TaskSummary): string {
  if (t.clarifications_pending > 0) {
    const n = t.clarifications_pending;
    return `${n} question${n > 1 ? "s" : ""} waiting for you`;
  }
  switch (t.state) {
    case "draft_intent":
      return "Just created";
    case "discussing":
      return "Exploring requirements";
    case "discuss_done":
      return "Ready to plan";
    case "drafting_plan":
      return "Drafting a plan";
    case "plan_ready":
      return "Plan ready — waiting for approval";
    case "executing":
      return "Writing code";
    case "verifying":
      return "Checking the work";
    case "success":
      return "PR open — ready to review";
    case "pr_merged":
      return "Merged and shipped";
    case "blocked":
      return "Needs your input";
    case "cancelled":
      return "Cancelled";
    default:
      return t.state.replace(/_/g, " ");
  }
}


/* ── Empty states ───────────────────────────────────────────── */

function TaskListEmpty({
  bucket,
  hasSearch,
  onNew,
  onClearSearch,
}: {
  bucket: string;
  hasSearch: boolean;
  onNew: () => void;
  onClearSearch: () => void;
}) {
  if (hasSearch) {
    return (
      <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
        <Search className="mb-3 h-8 w-8 text-fg-3" strokeWidth={1.5} />
        <p className="text-[13px] font-medium text-fg-1">No matches</p>
        <p className="mt-1 max-w-xs text-[11.5px] text-fg-3">
          Try fewer keywords. Search hits title and Jira key.
        </p>
        <button
          onClick={onClearSearch}
          className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-2.5 py-1 text-[12px] font-medium text-fg-1 hover:bg-bg-2"
        >
          <X className="h-3 w-3" strokeWidth={2} />
          Clear search
        </button>
      </div>
    );
  }

  if (bucket === "working") {
    return (
      <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent/10">
          <Sparkles className="h-7 w-7 text-accent" strokeWidth={1.5} />
        </div>
        <p className="text-[15px] font-semibold text-fg-0">
          Start your first task
        </p>
        <p className="mt-2 max-w-[260px] text-[12.5px] leading-relaxed text-fg-2">
          Describe what you want built. Bifrost will plan the work and open a
          pull request for your review.
        </p>
        <button
          onClick={onNew}
          className="mt-4 inline-flex items-center gap-2 rounded-md bg-accent px-4 py-2 text-[13px] font-medium text-accent-fg hover:bg-accent-hover"
        >
          <Plus className="h-4 w-4" strokeWidth={2} />
          New task
        </button>
      </div>
    );
  }

  const copy: Record<string, { icon: React.ReactNode; msg: string }> = {
    "needs-you": {
      icon: <AlertTriangle className="mb-3 h-7 w-7 text-fg-3" strokeWidth={1.5} />,
      msg: "Nothing needs your input right now.",
    },
    review: {
      icon: <GitPullRequest className="mb-3 h-7 w-7 text-fg-3" strokeWidth={1.5} />,
      msg: "No open pull requests to review.",
    },
    shipped: {
      icon: <CheckCircle2 className="mb-3 h-7 w-7 text-fg-3" strokeWidth={1.5} />,
      msg: "Nothing shipped yet. Tasks move here when their PR merges.",
    },
  };

  const { icon, msg } = copy[bucket] ?? {
    icon: <ClipboardList className="mb-3 h-7 w-7 text-fg-3" strokeWidth={1.5} />,
    msg: "No tasks here.",
  };

  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      {icon}
      <p className="text-[13px] font-medium text-fg-1">{msg}</p>
    </div>
  );
}


/* ── Composer ───────────────────────────────────────────────── */

function NewTaskComposer({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (taskId: string) => void;
}) {
  const [text, setText] = useState("");
  const [jiraKey, setJiraKey] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!text.trim() || submitting) return;
    setSubmitting(true);
    try {
      const trimmedKey = jiraKey.trim().toUpperCase();
      const title = text
        .split("\n")[0]
        .slice(0, 120)
        .replace(/^https?:\/\/\S+\s*/, "");
      const task = await createTask({
        title: title || "New task",
        intent_text: text,
        created_from: trimmedKey ? "jira" : "direct",
        jira_key: trimmedKey || null,
      });
      toast.success("Task created");
      onCreated(task.id);
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="mb-5 rounded-lg border border-border bg-bg-1 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-[13.5px] font-semibold text-fg-0">
          <Sparkles className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          New task
        </h2>
        <button
          onClick={onClose}
          className="rounded p-0.5 text-fg-3 hover:bg-bg-2 hover:text-fg-1"
          title="Close"
        >
          <X className="h-4 w-4" strokeWidth={1.75} />
        </button>
      </div>

      <textarea
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void submit();
        }}
        placeholder="Describe what you want done — e.g. Add retry logic to the webhook handler in the orders service"
        rows={3}
        className="w-full resize-none rounded-md border border-border bg-bg-0 px-3 py-2 text-[13.5px] text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
      />

      <div className="mt-2 flex items-center gap-2">
        <input
          value={jiraKey}
          onChange={(e) => setJiraKey(e.target.value.toUpperCase())}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
          }}
          placeholder="Jira key (optional)"
          className="w-40 rounded-md border border-border bg-bg-0 px-2.5 py-1.5 font-mono text-[12px] uppercase text-fg-0 placeholder:normal-case placeholder:text-fg-3 focus:border-accent focus:outline-none"
        />
        <p className="text-[10.5px] text-fg-3">⌘↵ to submit</p>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-border bg-bg-2 px-3 py-1.5 text-[12px] font-medium text-fg-1 hover:bg-bg-3"
          >
            Cancel
          </button>
          <button
            onClick={() => void submit()}
            disabled={!text.trim() || submitting}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-50"
          >
            {submitting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2} />
            ) : (
              <Plus className="h-3.5 w-3.5" strokeWidth={2} />
            )}
            {submitting ? "Creating…" : "Create task"}
          </button>
        </div>
      </div>
    </section>
  );
}


/* ── From Jira modal ────────────────────────────────────────── */

function FromJiraModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (taskId: string) => void;
}) {
  const [key, setKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async () => {
    const trimmed = key.trim().toUpperCase();
    if (!trimmed) {
      setError("Jira key is required (e.g. PROJ-1234)");
      return;
    }
    if (!/^[A-Z][A-Z0-9]+-\d+$/.test(trimmed)) {
      setError(`"${trimmed}" doesn't look like a Jira key (PROJ-NNN)`);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const task = await taskFromJira({ jira_key: trimmed });
      toast.success(`Adopted ${trimmed} as task`);
      onCreated(task.id);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.status}: ${typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)}`
          : String(err);
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-md rounded-lg border border-border bg-bg-1 shadow-xl">
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-base font-semibold text-fg-0">
            Import from Jira
          </h3>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="rounded p-1 text-fg-2 hover:bg-bg-2"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="space-y-3 p-4">
          <p className="text-[12px] text-fg-2">
            Pull an existing Jira ticket into Bifrost. The ticket's summary
            becomes the task title; the description and acceptance criteria are
            imported automatically.
          </p>
          <div>
            <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-fg-2">
              Jira key
            </label>
            <input
              autoFocus
              type="text"
              value={key}
              onChange={(e) => setKey(e.target.value.toUpperCase())}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !submitting) void onSubmit();
              }}
              placeholder="PROJ-1234"
              className="w-full rounded border border-border bg-background px-2 py-1.5 font-mono text-sm uppercase focus:border-primary focus:outline-none"
            />
          </div>
          {error && (
            <div className="rounded border border-destructive/30 bg-destructive/10 p-2 text-[12px] text-destructive">
              {error}
            </div>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="rounded-md px-3 py-1.5 text-[13px] font-medium text-fg-1 hover:bg-bg-2 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void onSubmit()}
            disabled={submitting || !key.trim()}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[13px] font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-50"
          >
            {submitting && (
              <Loader2 className="h-3 w-3 animate-spin" strokeWidth={1.75} />
            )}
            Import ticket
          </button>
        </footer>
      </div>
    </div>
  );
}


/* ── Highlight helper ───────────────────────────────────────── */

function _highlight(text: string, terms: string[]): React.ReactNode {
  if (!terms || terms.length === 0) return text;
  const escaped = terms
    .filter((t) => t.length > 0)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (escaped.length === 0) return text;
  const splitRe = new RegExp(`(${escaped.join("|")})`, "ig");
  const matchRe = new RegExp(`^(${escaped.join("|")})$`, "i");
  const parts = text.split(splitRe);
  return parts.map((p, i) =>
    matchRe.test(p) ? (
      <mark key={i} className="rounded bg-accent-soft px-0.5 text-accent">
        {p}
      </mark>
    ) : (
      <span key={i}>{p}</span>
    ),
  );
}
