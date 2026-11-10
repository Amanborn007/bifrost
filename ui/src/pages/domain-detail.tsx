import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import {
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  GitBranch,
  Layers,
  Loader2,
  MessageSquareText,
  Sparkles,
  Workflow,
  X,
} from "lucide-react";
import {
  getDomainDetail,
  discoverDomainRepos,
  triggerExtractionRich,
  type DomainRepoEntry,
  type TeamRepoDiscoveryResult,
} from "@/lib/api";
import { FreshnessIndicator } from "@/components/freshness-indicator";
import { RepoActionGroup } from "@/components/repo-action-group";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";

/**
 * DomainDetail — overview of one domain (a team or product area).
 *
 * Aggregates repo freshness, entity counts, and per-repo latest-task status.
 * Built for power users watching a domain-wide rollout.
 */
export function DomainDetailPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<TeamRepoDiscoveryResult | null>(null);
  const detail = useQuery({
    queryKey: ["domain-detail", id],
    queryFn: () => getDomainDetail(id!),
    enabled: !!id,
    refetchInterval: 15_000, // keeps extraction status fresh
  });

  const onDiscover = async (opts: {
    dry_run: boolean;
    trigger_extraction: boolean;
  }) => {
    if (!id) return;
    try {
      setSyncing(true);
      const result = await discoverDomainRepos(id, opts);
      setSyncResult(result);
      if (!opts.dry_run) {
        qc.invalidateQueries({ queryKey: ["domain-detail", id] });
        qc.invalidateQueries({ queryKey: ["repos"] });
        qc.invalidateQueries({ queryKey: ["tasks"] });
        const delta = result.added.length + result.linked.length;
        toast.success(
          delta > 0
            ? `Added ${delta} repo${delta === 1 ? "" : "s"} to this domain`
            : "No new repos found",
        );
      }
    } catch (err) {
      toast.error(
        `Repo discovery failed: ${describeError(err)}`,
      );
    } finally {
      setSyncing(false);
    }
  };

  const onTrigger = async (
    repoId: string,
    repoName: string,
    opts: { force_full?: boolean; invalidate_phases?: string[] },
    kind: "resume" | "reextract" | "phases",
  ) => {
    const confirmMsg =
      kind === "reextract"
        ? `Wipe checkpoint and re-run ALL phases for ${repoName}? This is expensive.`
        : kind === "phases"
          ? `Re-run phases ${opts.invalidate_phases?.join(", ")} (+ downstream) for ${repoName}?`
          : null;
    if (confirmMsg && !confirm(confirmMsg)) return;
    try {
      await triggerExtractionRich(repoId, opts);
      const what =
        kind === "resume"
          ? "Resumed"
          : kind === "reextract"
            ? "Full re-extract queued for"
            : `Re-running ${opts.invalidate_phases?.join(",")} on`;
      toast.success(`${what} ${repoName}`);
      qc.invalidateQueries({ queryKey: ["domain-detail", id] });
      qc.invalidateQueries({ queryKey: ["repo-checkpoint", repoId] });
    } catch (err) {
      toast.error(
        `Failed: ${describeError(err)}`,
      );
    }
  };

  if (detail.isLoading) {
    return (
      <div className="mx-auto max-w-6xl px-8 py-10 text-sm text-fg-2">
        Loading domain…
      </div>
    );
  }
  if (detail.isError || !detail.data) {
    return (
      <div className="mx-auto max-w-6xl px-8 py-10">
        <Link to="/" className="text-[13px] text-accent hover:underline">
          ← Back
        </Link>
        <div className="mt-4 rounded-lg border border-err-soft bg-err-soft px-4 py-3 text-[13px] text-err">
          Couldn't load domain:{" "}
          {detail.error instanceof Error ? detail.error.message : "unknown"}
        </div>
      </div>
    );
  }

  const d = detail.data;
  const totals = {
    components: 0,
    flows: 0,
    dependencies: 0,
    relationships: 0,
  };
  for (const r of d.repos) {
    totals.components += r.entity_counts.component ?? 0;
    totals.flows += r.entity_counts.flow ?? 0;
    totals.dependencies += r.entity_counts.dependency ?? 0;
    totals.relationships += r.entity_counts.relationship ?? 0;
  }

  return (
    <div className="mx-auto max-w-6xl px-8 py-8">
      {/* Breadcrumb */}
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-[12.5px] text-fg-2 hover:text-accent"
      >
        <ArrowLeft className="h-3.5 w-3.5" strokeWidth={1.75} />
        Overview
      </Link>

      {/* Header */}
      <header className="mt-3 flex items-start justify-between gap-6">
        <div className="min-w-0">
          <h1 className="truncate text-[22px] font-semibold tracking-tight text-fg-0">
            {d.name}
          </h1>
          {d.description && (
            <p className="mt-1 max-w-2xl text-[13.5px] leading-relaxed text-fg-2">
              {d.description}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => onDiscover({ dry_run: true, trigger_extraction: false })}
            disabled={syncing}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-3 py-1.5 text-[12.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0 disabled:opacity-50"
            title={`Preview the repos whose manifest names team "${d.name}"`}
          >
            {syncing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={1.75} />
            ) : (
              <Workflow className="h-3.5 w-3.5" strokeWidth={1.75} />
            )}
            Preview repo discovery
          </button>
          <button
            onClick={() => onDiscover({ dry_run: false, trigger_extraction: false })}
            disabled={syncing}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-50"
            title={`Register the repos whose manifest names team "${d.name}"`}
          >
            {syncing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={1.75} />
            ) : (
              <Workflow className="h-3.5 w-3.5" strokeWidth={2} />
            )}
            Discover repos
          </button>
        </div>
      </header>

      {/* Sync result */}
      {syncResult && (
        <DiscoveryResultPanel
          result={syncResult}
          onDismiss={() => setSyncResult(null)}
        />
      )}

      {/* Stats */}
      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Repos"
          value={d.repos.length}
          sub={
            d.fresh_count > 0
              ? `${d.fresh_count} fresh`
              : `${d.stale_count} stale`
          }
          icon={GitBranch}
        />
        <StatCard
          label="Entities"
          value={d.total_entities.toLocaleString()}
          sub={`${totals.components} components · ${totals.flows} flows`}
          icon={Layers}
        />
        <StatCard
          label="Dependencies"
          value={totals.dependencies.toLocaleString()}
          sub={`${totals.relationships} relationships`}
          icon={Sparkles}
        />
        <StatCard
          label="Freshness"
          value={
            d.repos.length > 0
              ? `${Math.round((d.fresh_count / d.repos.length) * 100)}%`
              : "—"
          }
          sub={`${d.fresh_count}/${d.repos.length} fresh`}
          icon={CheckCircle2}
        />
      </div>

      {/* Repo table */}
      <section className="mt-8 overflow-hidden rounded-lg border border-border bg-bg-1">
        <header className="flex items-center justify-between gap-4 border-b border-border px-4 py-3">
          <h2 className="text-[13.5px] font-semibold text-fg-0">
            Repositories{" "}
            <span className="font-normal text-fg-3">({d.repos.length})</span>
          </h2>
          <Link
            to="/query"
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-2 px-2.5 py-1 text-[11.5px] font-medium text-fg-1 hover:border-border-strong hover:text-accent"
          >
            <MessageSquareText className="h-3 w-3" strokeWidth={1.75} />
            Ask in chat
          </Link>
        </header>

        {d.repos.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-fg-2">
            No repositories in this domain yet.
          </div>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border bg-bg-2 text-left text-[11px] font-medium uppercase tracking-wide text-fg-3">
                <th className="px-4 py-2">Name</th>
                <th className="px-3 py-2">State</th>
                <th className="px-3 py-2 text-right">Entities</th>
                <th className="px-3 py-2 text-right">Components</th>
                <th className="px-3 py-2 text-right">Flows</th>
                <th className="px-3 py-2">Last extraction</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {d.repos.map((r) => (
                <RepoRow
                  key={r.id}
                  r={r}
                  onTrigger={(opts, kind) => onTrigger(r.id, r.name, opts, kind)}
                />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  icon: Icon,
}: {
  label: string;
  value: string | number;
  sub?: string;
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-1 px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-fg-3">
          {label}
        </span>
        <Icon className="h-3.5 w-3.5 text-fg-3" strokeWidth={1.75} />
      </div>
      <div className="mt-1 font-mono text-[20px] font-semibold text-fg-0">
        {value}
      </div>
      {sub && (
        <div className="mt-0.5 truncate text-[11.5px] text-fg-2">{sub}</div>
      )}
    </div>
  );
}

function RepoRow({
  r,
  onTrigger,
}: {
  r: DomainRepoEntry;
  onTrigger: (
    opts: { force_full?: boolean; invalidate_phases?: string[] },
    kind: "resume" | "reextract" | "phases",
  ) => void;
}) {
  const total = Object.values(r.entity_counts).reduce((a, b) => a + b, 0);
  const components = r.entity_counts.component ?? 0;
  const flows = r.entity_counts.flow ?? 0;

  return (
    <tr className="hover:bg-bg-2">
      <td className="min-w-[200px] px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Link
            to={`/knowledge?repo=${r.id}`}
            className="font-medium text-fg-0 hover:text-accent"
          >
            {r.name}
          </Link>
          <a
            href={r.url}
            target="_blank"
            rel="noreferrer"
            className="text-fg-3 hover:text-accent"
            title={r.url}
          >
            <ExternalLink className="h-3 w-3" strokeWidth={1.75} />
          </a>
        </div>
      </td>
      <td className="px-3 py-2.5">
        <FreshnessIndicator state={r.freshness_state} />
        {r.latest_task_status && (
          <div className="mt-0.5 font-mono text-[10.5px] text-fg-3">
            task: {r.latest_task_status}
          </div>
        )}
      </td>
      <td className="px-3 py-2.5 text-right font-mono text-fg-0">
        {total.toLocaleString()}
      </td>
      <td className="px-3 py-2.5 text-right font-mono text-fg-1">
        {components.toLocaleString()}
      </td>
      <td className="px-3 py-2.5 text-right font-mono text-fg-1">
        {flows.toLocaleString()}
      </td>
      <td className="px-3 py-2.5 text-[12px] text-fg-2">
        {r.last_indexed_at
          ? formatRelativeDate(r.last_indexed_at)
          : <span className="text-fg-3">never</span>}
      </td>
      <td className="relative px-3 py-2.5 text-right">
        <RepoActionGroup
          repoId={r.id}
          handlers={{
            onResume: () => onTrigger({}, "resume"),
            onReextract: () => onTrigger({ force_full: true }, "reextract"),
            onInvalidate: (phases) =>
              onTrigger({ invalidate_phases: phases }, "phases"),
          }}
        />
      </td>
    </tr>
  );
}

function DiscoveryResultPanel({
  result,
  onDismiss,
}: {
  result: TeamRepoDiscoveryResult;
  onDismiss: () => void;
}) {
  const sections = [
    { label: "Added", rows: result.added, tone: "ok" as const },
    { label: "Linked", rows: result.linked, tone: "info" as const },
    {
      label: "Already linked",
      rows: result.already_linked,
      tone: "muted" as const,
    },
    { label: "Errors", rows: result.errors, tone: "err" as const },
  ];

  return (
    <section className="mt-6 rounded-lg border border-border bg-bg-1">
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-[13.5px] font-semibold text-fg-0">
            Repo discovery{result.dry_run ? " (preview)" : ""} —{" "}
            <span className="font-mono text-accent">{result.team}</span>
          </h2>
          <p className="mt-0.5 text-[11.5px] text-fg-3">
            Found {result.candidates_found} repo
            {result.candidates_found === 1 ? "" : "s"} whose manifest names team{" "}
            <code className="text-fg-2">{result.team}</code>
            {result.providers.length > 0 && ` (${result.providers.join(", ")})`}
            {result.dry_run && " · no writes made"}
          </p>
        </div>
        <button
          onClick={onDismiss}
          className="rounded p-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          title="Dismiss"
            aria-label="Dismiss"
        >
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </button>
      </header>
      <div className="divide-y divide-border">
        {sections.map((s) =>
          s.rows.length === 0 ? null : (
            <div key={s.label} className="px-4 py-2.5">
              <div className="mb-1 flex items-center gap-2">
                <span
                  className={cn(
                    "text-[11.5px] font-medium",
                    s.tone === "ok" && "text-ok",
                    s.tone === "info" && "text-accent",
                    s.tone === "muted" && "text-fg-2",
                    s.tone === "err" && "text-err",
                  )}
                >
                  {s.label}
                </span>
                <span className="font-mono text-[11px] text-fg-3">
                  ({s.rows.length})
                </span>
              </div>
              <ul className="space-y-0.5">
                {s.rows.map((r) => (
                  <li
                    key={r.full_name}
                    className="flex items-center gap-2 font-mono text-[12px] text-fg-1"
                  >
                    <span>{r.full_name}</span>
                    {r.error && (
                      <span className="text-err">— {r.error}</span>
                    )}
                    {r.task_id && (
                      <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
                        extraction queued
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ),
        )}
      </div>
    </section>
  );
}

