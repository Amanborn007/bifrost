import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  Copy,
  ExternalLink,
  Filter,
  Layers,
  Plus,
  RefreshCcw,
  Search,
  Shield,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";
import {
  deregisterRepo,
  getKnowledgeOverview,
  syncRepoOwnership,
  triggerExtractionRich,
  type KnowledgeRepo,
} from "@/lib/api";
import { RegisterRepoDialog } from "@/components/register-repo-dialog";
import { FreshnessIndicator } from "@/components/freshness-indicator";
import { RepoActionGroup } from "@/components/repo-action-group";
import { EntityBrowser } from "@/components/entity-browser";

/**
 * Knowledge — unified repo control panel + entity browser.
 *
 * Left rail: repo list with per-row ownership/actions. Right pane: the
 * entity browser filtered to the selected repo (or all repos when none
 * is selected). Replaces the old separate /knowledge and /entities pages.
 *
 * URL contract:
 *   /knowledge                       — all repos, no selection
 *   /knowledge?repo=<id>             — select that repo + filter entities
 *   /knowledge?domain=<id>           — filter both rails by domain
 *   /knowledge?search=<term>         — seed entity search
 */
export function KnowledgePage() {
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const selectedRepoId = searchParams.get("repo");
  const domainFilter = searchParams.get("domain") ?? "all";
  const entitySearchSeed = searchParams.get("search") ?? "";

  const [repoSearch, setRepoSearch] = useState("");
  const [registerOpen, setRegisterOpen] = useState(false);
  // Cap initial render to keep the rail responsive with a large repo catalog.
  // Bumped via "show more" button; a search typing reset is handled
  // in the useMemo filter below.
  const [visibleCap, setVisibleCap] = useState(100);

  const overview = useQuery({
    queryKey: ["knowledge-overview"],
    queryFn: getKnowledgeOverview,
    refetchInterval: 20_000,
  });

  const data = overview.data;
  const repos = useMemo(() => data?.repos ?? [], [data]);
  const domainOptions = useMemo(
    () => Object.keys(data?.by_domain ?? {}).sort(),
    [data],
  );

  // Reset the pagination cap whenever filters change — otherwise a
  // user searching for "delivery" might see 100 visible with
  // "show more (50)" even though only 5 match.
  useEffect(() => {
    setVisibleCap(100);
  }, [repoSearch, domainFilter]);

  const filteredRepos = useMemo(() => {
    return repos.filter((r) => {
      if (
        domainFilter !== "all" &&
        (r.domain_name ?? "(no domain)") !== domainFilter
      ) {
        return false;
      }
      if (!repoSearch.trim()) return true;
      const q = repoSearch.trim().toLowerCase();
      return (
        r.name.toLowerCase().includes(q) ||
        (r.squad ?? "").toLowerCase().includes(q) ||
        (r.jira_project_key ?? "").toLowerCase().includes(q) ||
        (r.domain_name ?? "").toLowerCase().includes(q)
      );
    });
  }, [repos, domainFilter, repoSearch]);

  const selectedRepo = useMemo(
    () => (selectedRepoId ? repos.find((r) => r.id === selectedRepoId) : null),
    [repos, selectedRepoId],
  );

  // Keep the URL as the single source of truth for repo selection so
  // deep-links like /knowledge?repo=<id> work and the back button does
  // the right thing.
  const selectRepo = (id: string | null) => {
    const params = new URLSearchParams(searchParams);
    if (id) params.set("repo", id);
    else params.delete("repo");
    // Preserve the domain filter so switching repos within a domain doesn't
    // drop the user's active filter; only clear the entity search seed.
    const domain = params.get("domain");
    params.delete("search");
    if (domain) params.set("domain", domain);
    setSearchParams(params, { replace: true });
  };

  const setDomain = (d: string) => {
    const params = new URLSearchParams(searchParams);
    if (d === "all") params.delete("domain");
    else params.set("domain", d);
    setSearchParams(params, { replace: true });
  };

  const onTrigger = async (
    repo: KnowledgeRepo,
    opts: { force_full?: boolean; invalidate_phases?: string[] },
    kind: "resume" | "reextract" | "phases",
  ) => {
    const confirmMsg =
      kind === "reextract"
        ? `Wipe checkpoint and re-run ALL phases for ${repo.name}?`
        : kind === "phases"
          ? `Re-run phases ${opts.invalidate_phases?.join(", ")} (+ downstream) for ${repo.name}?`
          : null;
    if (confirmMsg && !confirm(confirmMsg)) return;
    try {
      const res = await triggerExtractionRich(repo.id, opts);
      const label =
        kind === "resume"
          ? "Resumed"
          : kind === "reextract"
            ? "Full re-extract queued for"
            : `Re-running ${opts.invalidate_phases?.join(",")} on`;
      toast.success(`${label} ${repo.name} · task ${res.task_id.slice(0, 8)}`);
      qc.invalidateQueries({ queryKey: ["knowledge-overview"] });
      qc.invalidateQueries({ queryKey: ["repo-checkpoint", repo.id] });
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    }
  };

  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col">
      {/* Header + stat cards, compact */}
      <header className="border-b border-border px-6 py-3">
        <div className="flex items-end justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-[18px] font-semibold tracking-tight text-fg-0">
              Knowledge
            </h1>
            <p className="mt-0.5 text-[12.5px] text-fg-2">
              Repos, freshness, and entities — all in one place.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <StatChip
              label="Total"
              value={data?.total ?? 0}
              loading={overview.isLoading}
            />
            <StatChip
              label="Fresh"
              value={data?.fresh ?? 0}
              tone="ok"
              loading={overview.isLoading}
            />
            <StatChip
              label="Stale"
              value={data?.stale ?? 0}
              tone={data?.stale ? "warn" : "default"}
              loading={overview.isLoading}
            />
            <StatChip
              label="Not indexed"
              value={data?.not_indexed ?? 0}
              tone={data?.not_indexed ? "warn" : "default"}
              loading={overview.isLoading}
            />
            <button
              onClick={() => overview.refetch()}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-2.5 py-1.5 text-[12px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
            >
              <RefreshCcw
                className={cn(
                  "h-3.5 w-3.5",
                  overview.isFetching && "animate-spin",
                )}
                strokeWidth={1.75}
              />
              Refresh
            </button>
            <button
              onClick={() => setRegisterOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover"
            >
              <Plus className="h-3.5 w-3.5" strokeWidth={2} />
              Register repo
            </button>
          </div>
        </div>
      </header>

      <RegisterRepoDialog
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        onRegistered={() => {
          overview.refetch();
          qc.invalidateQueries({ queryKey: ["knowledge-overview"] });
        }}
      />

      {/* Master-detail */}
      <div className="grid min-h-0 flex-1 grid-cols-[340px_1fr] overflow-hidden">
        {/* LEFT RAIL: repo list */}
        <aside className="flex min-h-0 flex-col border-r border-border bg-bg-1">
          {/* Rail search + domain filter */}
          <div className="flex shrink-0 flex-col gap-1.5 border-b border-border px-3 py-2">
            <div className="relative">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3"
                strokeWidth={1.75}
              />
              <input
                value={repoSearch}
                onChange={(e) => setRepoSearch(e.target.value)}
                placeholder="Search repos, squad, Jira…"
                className="h-8 w-full rounded-md border border-border bg-bg-0 pl-8 pr-3 text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
              />
            </div>
            <div className="flex items-center gap-1.5">
              <Filter className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
              <select
                value={domainFilter}
                onChange={(e) => setDomain(e.target.value)}
                className="h-7 flex-1 rounded-md border border-border bg-bg-0 px-2 text-[12px] text-fg-0 focus:border-border-focus focus:outline-none"
              >
                <option value="all">All domains</option>
                {domainOptions.map((d) => (
                  <option key={d} value={d}>
                    {d} ({data?.by_domain[d]})
                  </option>
                ))}
              </select>
              <span className="font-mono text-[10.5px] text-fg-3">
                {filteredRepos.length}/{repos.length}
              </span>
            </div>
          </div>

          {/* Rail list */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            <button
              onClick={() => selectRepo(null)}
              className={cn(
                "flex w-full items-center gap-2 border-b border-border px-3 py-2 text-left text-[12.5px] transition-colors",
                !selectedRepoId
                  ? "bg-accent-soft text-accent"
                  : "text-fg-1 hover:bg-bg-2",
              )}
            >
              <Layers className="h-3.5 w-3.5" strokeWidth={1.75} />
              <span className="font-medium">All repos</span>
              <span className="ml-auto font-mono text-[10.5px] text-fg-3">
                {data?.total ?? 0}
              </span>
            </button>
            <ul className="divide-y divide-border">
              {filteredRepos.slice(0, visibleCap).map((r) => {
                const selected = r.id === selectedRepoId;
                const isRunning =
                  r.latest_task_status === "running" ||
                  r.latest_task_status === "received";
                const entityTotal =
                  r.component_count + r.flow_count + r.dependency_count;
                return (
                  <li key={r.id}>
                    <button
                      onClick={() => selectRepo(r.id)}
                      className={cn(
                        "flex w-full flex-col gap-1 px-3 py-2 text-left transition-colors",
                        selected ? "bg-accent-soft" : "hover:bg-bg-2",
                      )}
                    >
                      <div className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            "truncate text-[12.5px] font-medium",
                            selected ? "text-accent" : "text-fg-0",
                          )}
                        >
                          {r.name}
                        </span>
                        {r.compliance_tags.length > 0 && (
                          <Shield
                            className="h-3 w-3 shrink-0 text-warn"
                            strokeWidth={1.75}
                          />
                        )}
                        <span className="ml-auto font-mono text-[10.5px] text-fg-3">
                          {entityTotal.toLocaleString()}
                        </span>
                      </div>
                      <div className="flex items-center gap-1.5 text-[10.5px]">
                        <FreshnessIndicator state={r.freshness_state} />
                        {r.squad && (
                          <span className="font-mono text-fg-2">{r.squad}</span>
                        )}
                        {r.jira_project_key && (
                          <span className="font-mono text-fg-3">
                            · {r.jira_project_key}
                          </span>
                        )}
                        {isRunning && (
                          <span className="ml-auto inline-flex items-center gap-1 text-accent">
                            <Zap className="h-2.5 w-2.5" strokeWidth={2} />
                            {r.latest_task_status}
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                );
              })}
              {filteredRepos.length > visibleCap && (
                <li className="border-t border-border px-3 py-2 text-center">
                  <button
                    onClick={() => setVisibleCap((c) => c + 200)}
                    className="text-[11.5px] font-medium text-accent hover:underline"
                  >
                    Show {Math.min(200, filteredRepos.length - visibleCap)} more
                    <span className="ml-1 font-mono text-fg-3">
                      ({filteredRepos.length - visibleCap} hidden)
                    </span>
                  </button>
                </li>
              )}
              {filteredRepos.length === 0 && !overview.isLoading && (
                <li className="px-3 py-10 text-center text-[12px] text-fg-3">
                  No repos match.{" "}
                  <button
                    onClick={() => setRegisterOpen(true)}
                    className="text-accent hover:underline"
                  >
                    Register one
                  </button>
                  ?
                </li>
              )}
            </ul>
          </div>
        </aside>

        {/* RIGHT PANE: repo detail + entity browser */}
        <section className="flex min-h-0 flex-col overflow-hidden">
          {selectedRepo ? (
            <RepoDetailHeader
              r={selectedRepo}
              onTrigger={(opts, kind) => onTrigger(selectedRepo, opts, kind)}
              onOwnershipSync={() =>
                syncOwnershipFor(selectedRepo.id, selectedRepo.name, () => {
                  overview.refetch();
                })
              }
              onDeregister={() =>
                deregisterWithConfirm(selectedRepo, () => overview.refetch())
              }
              onClose={() => selectRepo(null)}
            />
          ) : (
            <div className="shrink-0 border-b border-border px-6 py-3">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <div className="text-[13.5px] font-semibold text-fg-0">
                    All repos
                  </div>
                  <div className="text-[11.5px] text-fg-2">
                    Browsing entities across {repos.length} repo
                    {repos.length === 1 ? "" : "s"}. Pick a repo on the left
                    for per-repo actions.
                  </div>
                </div>
              </div>
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
            <EntityBrowser
              key={selectedRepoId ?? "all"}
              repoId={selectedRepoId}
              domainId={null /* rail already filters repos by domain */}
              initialSearch={entitySearchSeed}
            />
          </div>
        </section>
      </div>
    </div>
  );
}

/* ─── Pinned detail header when a repo is selected ────────── */

function RepoDetailHeader({
  r,
  onTrigger,
  onOwnershipSync,
  onDeregister,
  onClose,
}: {
  r: KnowledgeRepo;
  onTrigger: (
    opts: { force_full?: boolean; invalidate_phases?: string[] },
    kind: "resume" | "reextract" | "phases",
  ) => void;
  onOwnershipSync: () => void;
  onDeregister: () => void;
  onClose: () => void;
}) {
  const copyUrn = async () => {
    await navigator.clipboard.writeText(`urn:bifrost:repo:${r.name}`);
    toast.success("URN copied");
  };
  return (
    <div className="shrink-0 border-b border-border bg-bg-1 px-6 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h2 className="truncate text-[15px] font-semibold text-fg-0">
              {r.name}
            </h2>
            <a
              href={r.url}
              target="_blank"
              rel="noreferrer"
              className="text-fg-3 hover:text-accent"
              title={r.url}
            >
              <ExternalLink className="h-3 w-3" strokeWidth={1.75} />
            </a>
            {r.compliance_tags.length > 0 && (
              <span
                title={`Compliance tags: ${r.compliance_tags.join(", ")}`}
                className="text-warn"
              >
                <Shield className="h-3 w-3" strokeWidth={1.75} />
              </span>
            )}
            <span className="ml-2 rounded border border-border bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
              urn:bifrost:repo:{r.name}
            </span>
            <button
              onClick={copyUrn}
              className="rounded p-1 text-fg-3 hover:bg-bg-2 hover:text-fg-0"
              title="Copy URN"
            aria-label="Copy URN"
            >
              <Copy className="h-3 w-3" strokeWidth={1.75} />
            </button>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11.5px]">
            <span className="inline-flex items-center gap-1">
              <FreshnessIndicator state={r.freshness_state} />
              {r.last_indexed_at && (
                <span className="text-fg-3">
                  · indexed {formatRelativeDate(r.last_indexed_at)}
                </span>
              )}
              {r.last_commit_indexed && (
                <span className="font-mono text-fg-3">
                  @ {r.last_commit_indexed.slice(0, 7)}
                </span>
              )}
            </span>
            {r.squad && (
              <span className="font-mono text-fg-2">squad={r.squad}</span>
            )}
            {r.jira_project_key && (
              <span className="font-mono text-fg-2">
                jira={r.jira_project_key}
              </span>
            )}
            {r.confluence_space_key &&
              r.confluence_space_key !== r.jira_project_key && (
                <span className="font-mono text-fg-2">
                  conf={r.confluence_space_key}
                </span>
              )}
            <span className="font-mono text-fg-3">
              {r.component_count}c · {r.flow_count}f · {r.dependency_count}d
            </span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <RepoActionGroup
            repoId={r.id}
            handlers={{
              onResume: () => onTrigger({}, "resume"),
              onReextract: () => onTrigger({ force_full: true }, "reextract"),
              onInvalidate: (phases) =>
                onTrigger({ invalidate_phases: phases }, "phases"),
            }}
          />
          <div className="mx-1 h-5 w-px bg-border" />
          <button
            onClick={onOwnershipSync}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-0 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:border-accent hover:text-accent"
            title="Re-scan ownership manifests + discover Jira project"
          >
            <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
            Ownership
          </button>
          <button
            onClick={onDeregister}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-0 px-2 py-1 text-[11.5px] font-medium text-fg-2 hover:border-err/60 hover:text-err"
            title="Deregister this repo"
            aria-label="Deregister this repo"
          >
            <Trash2 className="h-3 w-3" strokeWidth={1.75} />
          </button>
          <button
            onClick={onClose}
            className="rounded p-1 text-fg-3 hover:bg-bg-2 hover:text-fg-0"
            title="Close (show all repos)"
            aria-label="Close (show all repos)"
          >
            <X className="h-4 w-4" strokeWidth={1.75} />
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─── StatChip ─────────────────────────────────────────── */

function StatChip({
  label,
  value,
  tone,
  loading,
}: {
  label: string;
  value: string | number;
  tone?: "ok" | "warn" | "default";
  loading?: boolean;
}) {
  const toneClass =
    tone === "ok"
      ? "text-ok"
      : tone === "warn"
        ? "text-warn"
        : "text-fg-0";
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-bg-1 px-2.5 py-1">
      <span className="text-[10.5px] font-medium uppercase tracking-wide text-fg-3">
        {label}
      </span>
      <span
        className={cn(
          "font-mono text-[14px] font-semibold",
          toneClass,
          loading && "animate-pulse",
        )}
      >
        {value}
      </span>
    </div>
  );
}

/* ─── Helpers ───────────────────────────────────── */

async function syncOwnershipFor(
  id: string,
  name: string,
  onDone: () => void,
) {
  try {
    const res = await syncRepoOwnership(id);
    if (res.error) {
      toast.error(`Ownership sync failed: ${res.error}`);
    } else {
      toast.success(
        `Ownership refreshed — squad=${res.squad}, ${res.downstream_services_count} downstreams`,
      );
    }
    onDone();
  } catch (err) {
    toast.error(
      `Failed: ${describeError(err)} (${name})`,
    );
  }
}

async function deregisterWithConfirm(r: KnowledgeRepo, onDone: () => void) {
  if (
    !confirm(
      `Deregister ${r.name}? Entities and extraction history are preserved; only the registration is removed.`,
    )
  ) {
    return;
  }
  try {
    await deregisterRepo(r.id);
    toast.success(`${r.name} deregistered`);
    onDone();
  } catch (err) {
    toast.error(`Failed: ${describeError(err)}`);
  }
}

