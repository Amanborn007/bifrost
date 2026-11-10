import { useQuery } from "@tanstack/react-query";
import { memo, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";
import { FreshnessIndicator } from "@/components/freshness-indicator";
import { ExtractionProgressCard } from "@/components/extraction-progress-card";
import { RegisterRepoDialog } from "@/components/register-repo-dialog";
import { InboxPanel } from "@/components/inbox-panel";
import {
  listRepos,
  listTasks,
  listDomains,
  triggerExtraction,
  deregisterRepo,
} from "@/lib/api";
import type { RepoSummary, TaskSummary } from "@/lib/api";
import { toast } from "sonner";
import {
  RefreshCcw,
  GitBranch,
  ArrowUpRight,
  Zap,
  MessageSquare,
  Loader2,
  Plus,
  MoreHorizontal,
  Trash2,
  Database,
  Copy,
} from "lucide-react";
import { cn, describeError } from "@/lib/utils";

const POLL_INTERVAL = 30_000;

/**
 * Overview — clean product dashboard. Summary stats, active work, repos,
 * recent activity. No ceremonial headers, no cyberpunk.
 */
export function DashboardPage() {
  const [registerOpen, setRegisterOpen] = useState(false);

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: listRepos,
    refetchInterval: POLL_INTERVAL,
  });
  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => listTasks(undefined, { limit: 20 }),
    refetchInterval: POLL_INTERVAL,
  });
  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: listDomains,
    refetchInterval: POLL_INTERVAL,
  });

  const repoList = repos.data ?? [];
  const taskList = tasks.data ?? [];
  const domainList = domains.data ?? [];
  // Classify every registered repo into ONE bucket so counts sum to the total.
  const freshCount = repoList.filter((r) => r.freshness_state === "fresh").length;
  const staleCount = repoList.filter((r) =>
    ["stale", "very_stale", "slightly_stale"].includes(r.freshness_state),
  ).length;
  const notIndexedCount = repoList.length - freshCount - staleCount;
  const extractionTasks = taskList.filter((t) => t.type === "extraction");
  const queryTasks = taskList.filter((t) => t.type === "question");
  const runningExtractions = extractionTasks.filter((t) =>
    ["running", "paused"].includes(t.status),
  );
  const queuedExtractions = extractionTasks.filter((t) =>
    ["received", "pending"].includes(t.status),
  );

  return (
    <div className="mx-auto max-w-[1200px] space-y-8 px-8 py-8">
      {/* Title */}
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-fg-0">
            State of the bridge
          </h1>
          <p className="mt-1 text-[13.5px] text-fg-2">
            System overview and live activity.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              repos.refetch();
              tasks.refetch();
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-3 py-1.5 text-[12.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
            title="Refresh"
          >
            <RefreshCcw className="h-3.5 w-3.5" strokeWidth={1.75} />
            Refresh
          </button>
          <Link
            to="/knowledge"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-3 py-1.5 text-[12.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
          >
            <Database className="h-3.5 w-3.5" strokeWidth={1.75} />
            Manage repos
          </Link>
          <button
            onClick={() => setRegisterOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-fg hover:bg-accent-hover"
          >
            <Plus className="h-3.5 w-3.5" strokeWidth={2} />
            Register repository
          </button>
        </div>
      </header>

      {/* API error banner — surfaces silent fetch failures */}
      {(repos.isError || tasks.isError || domains.isError) && (
        <div className="rounded-lg border border-err-soft bg-err-soft px-4 py-3 text-[12.5px]">
          <div className="font-semibold text-err">
            Couldn't reach the Bifrost API
          </div>
          <ul className="mt-1 space-y-0.5 font-mono text-[11.5px] text-fg-1">
            {repos.isError && (
              <li>
                /repos → {(repos.error as Error)?.message ?? "error"}
              </li>
            )}
            {tasks.isError && (
              <li>
                /tasks → {(tasks.error as Error)?.message ?? "error"}
              </li>
            )}
            {domains.isError && (
              <li>
                /domains → {(domains.error as Error)?.message ?? "error"}
              </li>
            )}
          </ul>
          <p className="mt-1 text-[11.5px] text-fg-2">
            Check that the API server is running (default port 8000) and that
            the Vite proxy is forwarding /api.
          </p>
        </div>
      )}

      <RegisterRepoDialog
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        onRegistered={() => {
          repos.refetch();
          tasks.refetch();
        }}
      />

      {/* Inbox hero — single source of truth for "what's mine to do
          right now" across the whole product. Aggregates escalated
          asks, blocked tasks, asks routed to your squad, PRs awaiting
          review, and open work in flight. */}
      <InboxPanel />

      {/* Stat row — buckets sum to total */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Repositories"
          value={repoList.length}
          sub="registered"
          loading={repos.isLoading}
        />
        <Stat
          label="Fresh"
          value={freshCount}
          sub={_pct(freshCount, repoList.length)}
          tone="ok"
          loading={repos.isLoading}
        />
        <Stat
          label="Stale"
          value={staleCount}
          sub={_pct(staleCount, repoList.length)}
          tone={staleCount > 0 ? "warn" : "default"}
          loading={repos.isLoading}
        />
        <Stat
          label="Not indexed"
          value={notIndexedCount}
          sub={_pct(notIndexedCount, repoList.length)}
          tone={notIndexedCount > 0 ? "warn" : "default"}
          loading={repos.isLoading}
        />
      </section>

      {/* Domains */}
      {domainList.length > 0 && (
        <section>
          <SectionHeader icon={Database} title="Domains" count={domainList.length} />
          <div className="mt-3 flex flex-wrap gap-2">
            {domainList.map((d) => {
              const repoCount = repoList.filter(
                (r) => r.domain_id === d.id,
              ).length;
              return (
                <Link
                  key={d.id}
                  to={`/domains/${d.id}`}
                  className="group inline-flex items-center gap-2 rounded-lg border border-border bg-bg-1 px-3 py-2 transition-colors hover:border-accent hover:bg-bg-2"
                >
                  <span className="text-[13px] font-semibold text-fg-0 group-hover:text-accent">
                    {d.name}
                  </span>
                  <span className="rounded-full bg-bg-3 px-2 py-0.5 font-mono text-[10.5px] text-fg-2">
                    {repoCount}
                  </span>
                  <ArrowUpRight
                    className="h-3 w-3 text-fg-3 group-hover:text-accent"
                    strokeWidth={1.75}
                  />
                </Link>
              );
            })}
          </div>
        </section>
      )}

      {/* Running extractions — full progress card each */}
      {runningExtractions.length > 0 && (
        <section>
          <SectionHeader
            icon={Zap}
            title="Running now"
            count={runningExtractions.length}
          />
          <div className="mt-3 space-y-3">
            {runningExtractions.map((t) => (
              <ExtractionProgressCard key={t.id} task={t} />
            ))}
          </div>
        </section>
      )}

      {/* Queued extractions — compact list */}
      {queuedExtractions.length > 0 && (
        <section>
          <SectionHeader
            icon={Loader2}
            title="Queued"
            count={queuedExtractions.length}
          />
          <div className="mt-3 overflow-hidden rounded-lg border border-border bg-bg-1">
            <ul className="divide-y divide-border">
              {queuedExtractions.map((t, i) => (
                <li
                  key={t.id}
                  className="flex items-center justify-between gap-3 px-4 py-2.5 text-[13px]"
                >
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="tabular w-6 text-right font-mono text-[11px] text-fg-3">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span className="truncate font-medium text-fg-0">
                      {t.query.replace(/^Extract knowledge from /, "")}
                    </span>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <span className="font-mono text-[10.5px] text-fg-3">
                      waiting {relativeDuration(t.created_at)}
                    </span>
                    <span className="rounded-full bg-bg-3 px-2 py-0.5 font-mono text-[10.5px] text-fg-2">
                      {t.status}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}

      {/* Repos */}
      <section>
        <SectionHeader
          icon={GitBranch}
          title="Repositories"
          rightLink={{ label: "Browse entities", to: "/knowledge" }}
        />
        {/* ``contain-layout`` isolates the reflow when the skeleton is
            replaced with real content, so the page below doesn't bounce. */}
        <div className="mt-3 [contain:layout]">
          {repos.isLoading ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {[1, 2].map((i) => (
                <Skeleton key={i} className="h-28 rounded-lg" />
              ))}
            </div>
          ) : repoList.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-bg-1 px-6 py-12 text-center">
              <GitBranch className="mb-2 h-6 w-6 text-fg-3" strokeWidth={1.75} />
              <p className="text-[14px] font-medium text-fg-1">
                No repositories registered
              </p>
              <p className="mt-1 max-w-sm text-[12.5px] text-fg-3">
                Register a repository to start extracting knowledge.
              </p>
              <button
                onClick={() => setRegisterOpen(true)}
                className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-fg hover:bg-accent-hover"
              >
                <Plus className="h-3.5 w-3.5" strokeWidth={2} />
                Register your first repo
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {repoList.map((r) => (
                <RepoCard key={r.id} repo={r} onRefetch={repos.refetch} />
              ))}
            </div>
          )}
        </div>
      </section>

      {/* Activity */}
      <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <ActivityList
          icon={Zap}
          title="Recent extractions"
          tasks={extractionTasks}
          loading={tasks.isLoading}
          emptyBody="No extractions yet."
          kind="extraction"
        />
        <ActivityList
          icon={MessageSquare}
          title="Recent queries"
          tasks={queryTasks}
          loading={tasks.isLoading}
          emptyBody="No queries yet."
          kind="question"
        />
      </section>
    </div>
  );
}

/* ── Stat ───────────────────────────────────────────── */

function Stat({
  label,
  value,
  sub,
  tone = "default",
  loading,
}: {
  label: string;
  value: number;
  sub: string;
  tone?: "default" | "ok" | "warn";
  loading?: boolean;
}) {
  const toneColor = {
    default: "text-fg-0",
    ok: "text-ok",
    warn: "text-warn",
  }[tone];
  return (
    <div className="rounded-lg border border-border bg-bg-1 px-5 py-4 transition-colors hover:border-border-strong">
      <div className="section-title">{label}</div>
      {loading ? (
        <Skeleton className="mt-2 h-8 w-16" />
      ) : (
        <div className={cn("mt-1 tabular text-[30px] font-semibold leading-none", toneColor)}>
          {value}
        </div>
      )}
      <div className="mt-1.5 text-[12px] text-fg-3">{sub}</div>
    </div>
  );
}

/* ── Section header ─────────────────────────────────── */

function SectionHeader({
  icon: Icon,
  title,
  count,
  rightLink,
}: {
  icon?: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  title: string;
  count?: number;
  rightLink?: { label: string; to: string };
}) {
  return (
    <header className="flex items-center justify-between">
      <div className="flex items-center gap-2">
        {Icon && <Icon className="h-4 w-4 text-fg-2" strokeWidth={1.75} />}
        <h2 className="text-[15px] font-semibold text-fg-0">{title}</h2>
        {count != null && (
          <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10.5px] font-semibold text-accent">
            {count}
          </span>
        )}
      </div>
      {rightLink && (
        <Link
          to={rightLink.to}
          className="group inline-flex items-center gap-1 text-[12.5px] font-medium text-fg-2 hover:text-fg-0"
        >
          {rightLink.label}
          <ArrowUpRight
            className="h-3 w-3 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5"
            strokeWidth={1.75}
          />
        </Link>
      )}
    </header>
  );
}

/* ── Repo card ──────────────────────────────────────── */

/**
 * Memoized: with 30+ repos rendered as a grid, every 30s react-query
 * poll was forcing every card (including their menu state, status
 * badges, action handlers) to repaint. Repo identity is stable
 * across polls, so memo's shallow compare avoids the cascade.
 */
const RepoCard = memo(function RepoCard({
  repo,
  onRefetch,
}: {
  repo: RepoSummary;
  onRefetch: () => void;
}) {
  const [extracting, setExtracting] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const navigate = useNavigate();

  const handleExtract = async (forceFull = false) => {
    try {
      setExtracting(true);
      await triggerExtraction(repo.id, forceFull);
      toast.success(
        forceFull
          ? `Full re-extraction started: ${repo.name}`
          : `Extraction started: ${repo.name}`,
      );
      onRefetch();
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    } finally {
      setExtracting(false);
    }
  };

  const handleDelete = async () => {
    if (
      !confirm(
        `Deregister "${repo.name}"? This removes it from Bifrost (entities and extractions stay in the DB).`,
      )
    )
      return;
    try {
      await deregisterRepo(repo.id);
      toast.success(`Deregistered ${repo.name}`);
      onRefetch();
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    }
  };

  const handleCopyUrn = () => {
    navigator.clipboard.writeText(repo.urn);
    toast.success("URN copied");
  };

  // Close menu on Esc / click-away
  useEffect(() => {
    if (!menuOpen) return;
    const close = () => setMenuOpen(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("click", close);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  return (
    <article className="rounded-lg border border-border bg-bg-1 transition-colors hover:border-border-strong">
      <div className="flex items-start justify-between gap-3 px-5 py-3.5">
        <div className="min-w-0 flex-1">
          <Link
            to={`/knowledge?repo=${repo.id}`}
            className="text-[14px] font-semibold text-fg-0 hover:text-accent"
          >
            {repo.name}
          </Link>
          <p className="mt-0.5 truncate font-mono text-[11px] text-fg-2">
            {repo.url || "— no url —"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <FreshnessIndicator state={repo.freshness_state} />
          <div className="relative">
            <button
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen((m) => !m);
              }}
              className="flex h-7 w-7 items-center justify-center rounded-md text-fg-2 hover:bg-bg-2 hover:text-fg-0"
              title="More"
              aria-label="Repo actions"
            >
              <MoreHorizontal className="h-3.5 w-3.5" strokeWidth={1.75} />
            </button>
            {menuOpen && (
              <div
                className="absolute right-0 top-8 z-20 w-[200px] rounded-md border border-border bg-bg-2 py-1 shadow-xl"
                onClick={(e) => e.stopPropagation()}
              >
                <MenuItem
                  icon={Database}
                  onClick={() => {
                    setMenuOpen(false);
                    // Was ``window.location.href``,
                    // which forces a full page reload (state loss,
                    // cache invalidation, full bundle re-parse).
                    // ``navigate`` keeps the SPA state intact.
                    navigate(`/knowledge?repo=${repo.id}`);
                  }}
                >
                  Browse entities
                </MenuItem>
                <MenuItem
                  icon={RefreshCcw}
                  onClick={() => {
                    setMenuOpen(false);
                    handleExtract(true);
                  }}
                >
                  Re-extract (full)
                </MenuItem>
                <MenuItem icon={Copy} onClick={handleCopyUrn}>
                  Copy URN
                </MenuItem>
                <div className="my-1 border-t border-border" />
                <MenuItem icon={Trash2} danger onClick={handleDelete}>
                  Deregister
                </MenuItem>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center justify-between border-t border-border px-5 py-3">
        <div className="text-[12px] text-fg-2">
          <span className="text-fg-3">Status</span>{" "}
          <span className="text-fg-1">{repo.index_status}</span>
          {repo.last_indexed_at && (
            <>
              {"  ·  "}
              <span className="text-fg-3">Last</span>{" "}
              <span className="text-fg-1">
                {new Date(repo.last_indexed_at).toLocaleDateString()}
              </span>
            </>
          )}
        </div>
        <button
          onClick={() => handleExtract(false)}
          disabled={extracting}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors",
            extracting
              ? "bg-bg-3 text-fg-3"
              : "bg-accent text-accent-fg hover:bg-accent-hover",
          )}
        >
          {extracting ? (
            <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
          ) : (
            <Zap className="h-3 w-3" strokeWidth={2} />
          )}
          {extracting ? "Starting…" : "Extract"}
        </button>
      </div>
    </article>
  );
});

function MenuItem({
  icon: Icon,
  children,
  onClick,
  danger,
}: {
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  children: React.ReactNode;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] transition-colors",
        danger
          ? "text-err hover:bg-err-soft"
          : "text-fg-1 hover:bg-bg-3 hover:text-fg-0",
      )}
    >
      <Icon className="h-3.5 w-3.5" strokeWidth={1.75} />
      {children}
    </button>
  );
}

/* ── Activity list ──────────────────────────────────── */

function ActivityList({
  icon,
  title,
  tasks,
  loading,
  emptyBody,
  kind,
}: {
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  title: string;
  tasks: TaskSummary[];
  loading: boolean;
  emptyBody: string;
  kind: "extraction" | "question";
}) {
  return (
    <div>
      <SectionHeader icon={icon} title={title} />
      <div className="mt-3">
        {loading ? (
          <Skeleton className="h-36 rounded-lg" />
        ) : tasks.length === 0 ? (
          <EmptyState title="Empty" body={emptyBody} />
        ) : (
          <ol className="overflow-hidden rounded-lg border border-border bg-bg-1">
            {tasks.slice(0, 6).map((t, idx) =>
              kind === "extraction" ? (
                <ExtractionRow key={t.id} task={t} first={idx === 0} />
              ) : (
                <QueryRow key={t.id} task={t} first={idx === 0} />
              ),
            )}
          </ol>
        )}
      </div>
    </div>
  );
}

const ExtractionRow = memo(function ExtractionRow({
  task,
  first,
}: {
  task: TaskSummary;
  first: boolean;
}) {
  // result shape from worker: {components, flows, dependencies, relationships, errors?, partial?}
  const r = (task.result ?? {}) as Record<string, number | boolean | undefined>;
  const components = (r.components as number) ?? 0;
  const flows = (r.flows as number) ?? 0;
  const deps = (r.dependencies as number) ?? 0;
  const rels = (r.relationships as number) ?? 0;
  const total = components + flows + deps + rels;
  const errors = (r.errors as number) ?? 0;
  return (
    <li
      className={cn(
        "flex flex-col gap-1.5 px-4 py-2.5",
        !first && "border-t border-border",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <TaskStatusDot status={task.status} />
          <span className="truncate text-[13px] font-medium text-fg-0">
            {task.query}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[11px] text-fg-3">
          <span>{humanStatus(task.status)}</span>
          <span aria-hidden className="text-fg-4">·</span>
          <time dateTime={task.created_at}>
            {formatDate(task.created_at)}
          </time>
        </div>
      </div>
      {(total > 0 || errors > 0 || task.error_message) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-4 text-[11px] text-fg-2">
          {components > 0 && (
            <span>
              <span className="tabular text-fg-1">{components}</span>{" "}
              <span className="text-fg-3">components</span>
            </span>
          )}
          {flows > 0 && (
            <span>
              <span className="tabular text-fg-1">{flows}</span>{" "}
              <span className="text-fg-3">flows</span>
            </span>
          )}
          {deps > 0 && (
            <span>
              <span className="tabular text-fg-1">{deps}</span>{" "}
              <span className="text-fg-3">deps</span>
            </span>
          )}
          {rels > 0 && (
            <span>
              <span className="tabular text-fg-1">{rels}</span>{" "}
              <span className="text-fg-3">rels</span>
            </span>
          )}
          {errors > 0 && (
            <span className="text-err">
              <span className="tabular font-medium">{errors}</span>{" "}
              <span>errors</span>
            </span>
          )}
          {task.error_message && (
            <span className="truncate text-err" title={task.error_message}>
              {task.error_message.slice(0, 80)}
            </span>
          )}
        </div>
      )}
    </li>
  );
});

const QueryRow = memo(function QueryRow({
  task,
  first,
}: {
  task: TaskSummary;
  first: boolean;
}) {
  return (
    <li className={cn("flex items-center justify-between gap-3 px-4 py-2.5", !first && "border-t border-border")}>
      <div className="flex min-w-0 items-center gap-2.5">
        <TaskStatusDot status={task.status} />
        <span className="truncate text-[13px] text-fg-0">{task.query}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2 text-[11px] text-fg-3">
        <span>{humanStatus(task.status)}</span>
        <span aria-hidden className="text-fg-4">·</span>
        <time dateTime={task.created_at}>{formatDate(task.created_at)}</time>
      </div>
    </li>
  );
});

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function TaskStatusDot({ status }: { status: string }) {
  const cls =
    status === "running"
      ? "bg-accent"
      : status === "delivered"
        ? "bg-ok"
        : status === "failed" || status === "cancelled"
          ? "bg-err"
          : status === "paused"
            ? "bg-info"
            : "bg-fg-3";
  return <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", cls)} />;
}

function humanStatus(s: string): string {
  return (
    {
      running: "Running",
      delivered: "Complete",
      failed: "Failed",
      cancelled: "Cancelled",
      paused: "Paused",
      received: "Queued",
      pending: "Pending",
    }[s] ?? s
  );
}

function _pct(part: number, total: number): string {
  if (total <= 0) return "—";
  return `${Math.round((part / total) * 100)}% of total`;
}

function relativeDuration(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const mRem = m % 60;
  return mRem === 0 ? `${h}h` : `${h}h ${mRem}m`;
}

function EmptyState({
  icon: Icon,
  title,
  body,
}: {
  icon?: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  title: string;
  body: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-bg-1 px-6 py-10 text-center">
      {Icon && <Icon className="mb-2 h-5 w-5 text-fg-3" strokeWidth={1.75} />}
      <p className="text-[13.5px] font-medium text-fg-1">{title}</p>
      <p className="mt-0.5 text-[12px] text-fg-3">{body}</p>
    </div>
  );
}
