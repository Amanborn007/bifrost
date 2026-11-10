/**
 * Five primitive panels for the redesigned collaboration page.
 *
 *   <PulsePanel />        — squad/domain/repo activity rollup
 *   <AsksPanel />         — auto-routed "who do I ask" board
 *   <ReviewQueuePanel />  — open PRs that need MY attention
 *   <StandupPanel />      — weekly status update draft
 *   <DomainHubPanel />    — onboarding-friendly per-squad summary
 *
 * Co-located in one file because each panel is small (<200 LOC) and
 * they share the same data-fetching idiom + the universal "Discuss"
 * button. Split into separate files when one outgrows ~400 LOC.
 */

import { useMemo, useState } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  Activity, ExternalLink, Loader2, MessageCircle, RefreshCw,
  AlertTriangle, GitPullRequest, FileText, Users,
  Sparkles, Send, Undo2,
} from "lucide-react";
import {
  ApiError,
  askCancel,
  askEscalate,
  askMarkAnswered,
  askMarkSent,
  createAsk,
  getDomainHub,
  getPulse,
  getPulseSectionMarkdown,
  getReviewQueue,
  getStandupDraft,
  listAsks,
  listRepos,
  seedConversation,
  type AskRow,
  type DomainHubResp,
  type ReviewQueueResp,
  type StandupDraftResp,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { MasterDetail, type Bucket } from "@/components/master-detail";
import { cn, formatRelativeDate } from "@/lib/utils";
import type { ReviewItem } from "@/lib/api";


// ── Universal "Discuss" button ──────────────────────────────────────


export function DiscussButton({
  heading,
  seedContext,
  scopeRepoIds = [],
  followUpQuestion,
  size = "sm",
  variant = "ghost",
  label = "Discuss",
}: {
  heading: string;
  seedContext: string;
  scopeRepoIds?: string[];
  followUpQuestion?: string;
  size?: "xs" | "sm";
  variant?: "ghost" | "outline" | "default";
  label?: string;
}) {
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);
  return (
    <Button
      size={size}
      variant={variant}
      disabled={pending}
      onClick={async () => {
        setPending(true);
        try {
          const { id } = await seedConversation({
            title: heading,
            heading,
            seed_context: seedContext,
            scope_repo_ids: scopeRepoIds,
            follow_up_question: followUpQuestion ?? undefined,
          });
          navigate(`/query?conv=${id}`);
        } catch (err) {
          toast.error(
            `Failed to start chat: ${err instanceof ApiError ? err.status : String(err)}`,
          );
        } finally {
          setPending(false);
        }
      }}
    >
      {pending
        ? <Loader2 className="h-3 w-3 animate-spin" strokeWidth={1.75} />
        : <MessageCircle className="h-3 w-3" strokeWidth={1.75} />}
      {label}
    </Button>
  );
}


// ── P1 Pulse panel ──────────────────────────────────────────────────


export function PulsePanel() {
  // Warm the repo cache (shared query key) — result not read yet, but kept
  // so the squad chooser has data once RepoSummary exposes ownership.
  useQuery({ queryKey: ["repos-min"], queryFn: listRepos });
  // Squad chooser — derive from repos that carry a squad. If none of
  // the loaded repos has ownership data we still allow free-text.
  const allSquads = useMemo<string[]>(() => {
    const set = new Set<string>();
    // RepoSummary doesn't currently expose squad, so there's nothing to
    // collect yet; the user types a squad name and the API does a
    // case-insensitive match. Kept as a seam for when repos carry squads.
    return Array.from(set).sort();
    // No deps: the body derives only from a (currently empty) local set;
    // repos.data is not yet read until RepoSummary exposes squad ownership.
  }, []);

  const [squad, setSquad] = useState<string>("");
  const [rangeDays, setRangeDays] = useState<number>(7);

  const pulse = useQuery({
    queryKey: ["pulse", squad, rangeDays],
    queryFn: () => getPulse({ squad, range_days: rangeDays }),
    enabled: !!squad,
    staleTime: 30_000,
  });

  return (
    <div className="space-y-4">
      <header className="flex items-center gap-3 rounded-md border border-border bg-bg-1 px-3 py-2.5">
        <Activity className="h-4 w-4 text-accent" strokeWidth={1.75} />
        <div className="flex flex-1 items-center gap-2 text-[13px]">
          <label>Squad</label>
          <input
            type="text"
            value={squad}
            onChange={(e) => setSquad(e.target.value.toUpperCase())}
            placeholder="team name"
            list={allSquads.length ? "squad-suggestions" : undefined}
            className="w-32 rounded border border-border bg-background px-2 py-1 font-mono text-sm uppercase focus:border-primary focus:outline-none"
          />
          {allSquads.length > 0 && (
            <datalist id="squad-suggestions">
              {allSquads.map((s) => <option key={s} value={s} />)}
            </datalist>
          )}
          <label>Range</label>
          <select
            value={rangeDays}
            onChange={(e) => setRangeDays(parseInt(e.target.value, 10))}
            className="rounded border border-border bg-background px-2 py-1 text-sm focus:border-primary focus:outline-none"
          >
            <option value={3}>3 days</option>
            <option value={7}>This week</option>
            <option value={14}>2 weeks</option>
            <option value={30}>This month</option>
            <option value={90}>Quarter</option>
          </select>
          <Button
            size="xs"
            variant="ghost"
            onClick={() => pulse.refetch()}
            disabled={pulse.isFetching}
          >
            <RefreshCw className={cn("h-3 w-3", pulse.isFetching && "animate-spin")} strokeWidth={1.75} />
            Refresh
          </Button>
        </div>
        {pulse.data && (
          <span className="font-mono text-[10.5px] text-fg-3">
            {pulse.data.scope.label}
          </span>
        )}
      </header>

      {pulse.isLoading && (
        <div className="rounded-md border border-border bg-bg-1 p-6 text-center text-sm text-fg-2">
          <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" />
          Aggregating activity…
        </div>
      )}

      {pulse.data && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <PulseSection
            title="Shipped"
            count={pulse.data.tasks_shipped_count}
            tone="ok"
            sectionKey="shipped"
            squad={squad}
            rangeDays={rangeDays}
          >
            {pulse.data.tasks_shipped.length === 0
              ? <Empty />
              : pulse.data.tasks_shipped.map((t) => (
                  <TaskRowLine key={t.id} t={t} />
                ))}
          </PulseSection>

          <PulseSection
            title="In flight"
            count={pulse.data.tasks_in_flight_count}
            tone="accent"
            sectionKey="in_flight"
            squad={squad}
            rangeDays={rangeDays}
          >
            {pulse.data.tasks_in_flight.length === 0
              ? <Empty />
              : pulse.data.tasks_in_flight.map((t) => (
                  <TaskRowLine key={t.id} t={t} />
                ))}
          </PulseSection>

          <PulseSection
            title="Blocked"
            count={pulse.data.tasks_blocked_count}
            tone="warn"
            sectionKey="blocked"
            squad={squad}
            rangeDays={rangeDays}
          >
            {pulse.data.tasks_blocked.length === 0
              ? <Empty />
              : pulse.data.tasks_blocked.map((t) => (
                  <TaskRowLine key={t.id} t={t} />
                ))}
          </PulseSection>

          <PulseSection
            title="Recent chats"
            count={pulse.data.chats_count}
            tone="default"
            sectionKey="chats"
            squad={squad}
            rangeDays={rangeDays}
          >
            {pulse.data.recent_chats.length === 0
              ? <Empty />
              : pulse.data.recent_chats.map((c) => (
                  <a
                    key={c.id}
                    href={`/query?conv=${c.id}`}
                    className="block truncate text-[12px] text-fg-1 hover:underline"
                    title={c.title}
                  >
                    {c.title} · <span className="font-mono text-[10.5px] text-fg-3">{c.cited_urn_count} cites</span>
                  </a>
                ))}
          </PulseSection>

          <PulseSection
            title="Design docs"
            count={pulse.data.gdocs_added_count}
            tone="default"
            sectionKey="gdocs"
            squad={squad}
            rangeDays={rangeDays}
          >
            {pulse.data.gdocs_added.length === 0
              ? <Empty />
              : pulse.data.gdocs_added.map((g, i) => (
                  <a key={i} href={g.url} target="_blank" rel="noreferrer"
                     className="block truncate text-[12px] text-fg-1 hover:underline">
                    <FileText className="mr-1 inline h-3 w-3" strokeWidth={1.75} />
                    {g.title}
                  </a>
                ))}
          </PulseSection>

          <PulseSection
            title="Jira closed"
            count={pulse.data.jira_closed_count}
            tone="default"
            sectionKey="jira_closed"
            squad={squad}
            rangeDays={rangeDays}
          >
            {pulse.data.jira_closed.length === 0
              ? <Empty />
              : pulse.data.jira_closed.map((j) => (
                  <a key={j.key} href={j.url} target="_blank" rel="noreferrer"
                     className="block truncate text-[12px] text-fg-1 hover:underline">
                    <span className="font-mono text-[11px]">{j.key}</span> {j.title}
                  </a>
                ))}
          </PulseSection>

          <div className="rounded-md border border-border bg-bg-1 p-3 lg:col-span-2">
            <div className="mb-2 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">
              <Users className="h-3 w-3" strokeWidth={1.75} />
              Top contributors
            </div>
            {pulse.data.top_contributors.length === 0 ? <Empty /> : (
              <div className="flex flex-wrap gap-1.5">
                {pulse.data.top_contributors.map((c) => (
                  <span
                    key={c.email}
                    className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-2 px-2 py-0.5 text-[11px]"
                    title={`${c.activity_count} activities`}
                  >
                    {c.email}
                    <span className="font-mono text-[10px] text-fg-3">{c.activity_count}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}


function PulseSection({
  title, count, tone, sectionKey, squad, rangeDays, children,
}: {
  title: string;
  count: number;
  tone: "ok" | "warn" | "accent" | "default";
  sectionKey: string;
  squad: string;
  rangeDays: number;
  children: React.ReactNode;
}) {
  const [discussing, setDiscussing] = useState(false);
  const navigate = useNavigate();

  const onDiscuss = async () => {
    setDiscussing(true);
    try {
      const md = await getPulseSectionMarkdown({ squad, range_days: rangeDays, section: sectionKey });
      const { id } = await seedConversation({
        title: `${title} · ${squad}`,
        heading: `${title} (${squad})`,
        seed_context: md.markdown,
      });
      navigate(`/query?conv=${id}`);
    } catch (err) {
      toast.error(`Failed to start chat: ${err instanceof ApiError ? err.status : String(err)}`);
    } finally {
      setDiscussing(false);
    }
  };

  return (
    <section className="rounded-md border border-border bg-bg-1 p-3">
      <header className="mb-2 flex items-center gap-2">
        <span className="text-[12px] font-semibold text-fg-0">{title}</span>
        <span
          className={cn(
            "badge rounded-full font-mono text-[10px]",
            tone === "ok" && "badge-ok",
            tone === "warn" && "badge-err",
            tone === "accent" && "badge-info",
            tone === "default" && "badge-neutral",
          )}
        >
          {count}
        </span>
        <Button size="xs" variant="ghost" disabled={discussing} onClick={onDiscuss} className="ml-auto">
          {discussing ? <Loader2 className="h-3 w-3 animate-spin" strokeWidth={1.75} /> : <MessageCircle className="h-3 w-3" strokeWidth={1.75} />}
          Discuss
        </Button>
      </header>
      <div className="space-y-1">{children}</div>
    </section>
  );
}

function TaskRowLine({ t }: { t: { id: string; title: string; state: string; jira_key: string | null; pr_url: string | null; pr_number: number | null; owner_email: string | null } }) {
  return (
    <div className="flex items-center gap-1.5 truncate text-[12px]">
      <a href={`/tasks/${t.id}`} className="truncate text-fg-1 hover:underline">{t.title}</a>
      {t.jira_key && (
        <span className="font-mono text-[10px] text-info">{t.jira_key}</span>
      )}
      {t.pr_number && (
        <span className="font-mono text-[10px] text-review">#{t.pr_number}</span>
      )}
    </div>
  );
}

function Empty() {
  return <div className="py-1 text-[11.5px] text-fg-3">(none)</div>;
}


// ── P2 Asks panel ───────────────────────────────────────────────────


export function AsksPanel() {
  const qc = useQueryClient();
  const [searchParams] = useSearchParams();
  const bucket = searchParams.get("bucket") ?? "routed";
  const [composerOpen, setComposerOpen] = useState(false);
  const asks = useQuery({
    queryKey: ["asks-open"],
    queryFn: () => listAsks({ status: "open", limit: 50 }),
    refetchInterval: 30_000,
  });

  const all = useMemo(() => asks.data ?? [], [asks.data]);
  const filtered = useMemo(
    () => all.filter((a) => a.status === bucket),
    [all, bucket],
  );
  const counts = useMemo(
    () => ({
      drafted: all.filter((a) => a.status === "drafted").length,
      routed: all.filter((a) => a.status === "routed").length,
      sent: all.filter((a) => a.status === "sent").length,
      escalated: all.filter((a) => a.status === "escalated").length,
    }),
    [all],
  );

  // The Board now uses the same master-detail surface the Tasks page
  // does — bucket rail · row list · right-panel quickview. Operator
  // picks a status, scans rows, lands on one, takes action without
  // leaving the page. Replaces the previous 4-column kanban whose
  // drill-down required hunting through stacked cards.
  const buckets: Bucket[] = [
    {
      id: "drafted",
      label: "Drafted",
      icon: <FileText className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: counts.drafted,
      tone: "default",
      hint: "Awaiting routing",
    },
    {
      id: "routed",
      label: "Routed",
      icon: <Users className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: counts.routed,
      tone: "info",
      hint: "Squad assigned",
    },
    {
      id: "sent",
      label: "Sent",
      icon: <Send className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: counts.sent,
      tone: "review",
      hint: "Posted to Slack",
    },
    {
      id: "escalated",
      label: "Escalated",
      icon: <AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: counts.escalated,
      tone: "err",
      hint: "Needs immediate attention",
    },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <header className="flex shrink-0 items-center justify-between gap-3 rounded-md border border-border bg-bg-1 px-3 py-2">
        <div className="text-[13px]">
          <div className="font-semibold text-fg-0">Asks</div>
          <div className="text-[11.5px] text-fg-3">Auto-routed questions for other squads.</div>
        </div>
        <Button size="sm" variant="default" onClick={() => setComposerOpen(true)}>
          <Sparkles className="h-3 w-3" strokeWidth={1.75} />
          New ask
        </Button>
      </header>

      {composerOpen && (
        <NewAskForm
          onClose={() => setComposerOpen(false)}
          onCreated={() => {
            setComposerOpen(false);
            qc.invalidateQueries({ queryKey: ["asks-open"] });
          }}
        />
      )}

      <MasterDetail<AskRow>
        className="min-h-0 flex-1"
        buckets={buckets}
        defaultBucketId="routed"
        items={filtered}
        itemKey={(a) => a.id}
        loading={asks.isLoading}
        renderItem={(a) => <AskRowLine ask={a} />}
        renderDetail={(a) => (a ? <AskCard ask={a} /> : null)}
        emptyList={
          <p className="text-center text-[12px] text-fg-3">
            Nothing here. Pick another bucket from the rail.
          </p>
        }
      />
    </div>
  );
}


function AskRowLine({ ask }: { ask: AskRow }) {
  return (
    <div className="px-3 py-2.5">
      <div className="truncate text-[12.5px] font-medium text-fg-0">
        {ask.question}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-1.5 font-mono text-[10.5px] text-fg-3">
        <span>{ask.asker_email.split("@")[0]}</span>
        {ask.target_squad && <span>· → {ask.target_squad}</span>}
        {ask.target_slack_channel && (
          <span>· {ask.target_slack_channel}</span>
        )}
        <span>· {formatRelativeDate(ask.created_at)}</span>
      </div>
    </div>
  );
}


function AskCard({ ask }: { ask: AskRow }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [showDraft, setShowDraft] = useState(false);

  const sentMut = useMutation({
    mutationFn: () => askMarkSent(ask.id, null),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["asks-open"] }),
  });
  const escalateMut = useMutation({
    mutationFn: () => askEscalate(ask.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["asks-open"] }),
  });
  const cancelMut = useMutation({
    mutationFn: () => askCancel(ask.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["asks-open"] }),
  });

  const onDiscuss = async () => {
    try {
      const heading = `Ask: ${ask.target_squad ?? "(unrouted)"}`;
      const seed = [
        `*Question:* ${ask.question}`,
        ask.target_squad ? `*Routed to:* ${ask.target_squad}` : "",
        ask.slack_draft_md ? `\n*Slack draft:*\n${ask.slack_draft_md}` : "",
      ].filter(Boolean).join("\n");
      const { id } = await seedConversation({
        title: heading,
        heading,
        seed_context: seed,
        scope_repo_ids: ask.scope_repo_ids,
      });
      // Mark answered with the spawned conv id so "where's the answer?"
      // resolves automatically once the engineer chats their way to one.
      await askMarkAnswered(ask.id, id);
      qc.invalidateQueries({ queryKey: ["asks-open"] });
      navigate(`/query?conv=${id}`);
    } catch (err) {
      toast.error(`Failed: ${err instanceof ApiError ? err.status : String(err)}`);
    }
  };

  const onCopyDraft = async () => {
    if (!ask.slack_draft_md) return;
    await navigator.clipboard.writeText(ask.slack_draft_md);
    toast.success("Slack draft copied");
  };

  return (
    <article className="rounded-md border border-border bg-bg-2 p-2 text-[12px]">
      <div className="font-medium text-fg-0">{ask.question}</div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-fg-3">
        <span>by {ask.asker_email.split("@")[0]}</span>
        {ask.target_squad && <span>· {ask.target_squad}</span>}
        {ask.target_slack_channel && <span>· {ask.target_slack_channel}</span>}
        {ask.target_jira_project && (
          <span className="font-mono">{ask.target_jira_project}</span>
        )}
      </div>
      {ask.slack_draft_md && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowDraft((v) => !v)}
            className="text-[10.5px] text-fg-2 underline"
          >
            {showDraft ? "Hide" : "Show"} Slack draft
          </button>
          {showDraft && (
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap rounded border border-border bg-bg-1 p-2 font-mono text-[11px] text-fg-1">
              {ask.slack_draft_md}
            </pre>
          )}
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-1">
        {ask.slack_draft_md && (
          <Button size="xs" variant="outline" onClick={onCopyDraft}>
            Copy draft
          </Button>
        )}
        {ask.status !== "sent" && (
          <Button size="xs" variant="outline" disabled={sentMut.isPending} onClick={() => sentMut.mutate()}>
            <Send className="h-3 w-3" strokeWidth={1.75} />
            Mark sent
          </Button>
        )}
        <Button size="xs" variant="ghost" onClick={onDiscuss}>
          <MessageCircle className="h-3 w-3" strokeWidth={1.75} />
          Discuss
        </Button>
        {ask.status !== "escalated" && (
          <Button size="xs" variant="ghost" disabled={escalateMut.isPending} onClick={() => escalateMut.mutate()}>
            <AlertTriangle className="h-3 w-3" strokeWidth={1.75} />
            Escalate
          </Button>
        )}
        <Button size="xs" variant="ghost" disabled={cancelMut.isPending} onClick={() => cancelMut.mutate()}>
          <Undo2 className="h-3 w-3" strokeWidth={1.75} />
          Cancel
        </Button>
      </div>
    </article>
  );
}


function NewAskForm({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const repos = useQuery({ queryKey: ["repos-min"], queryFn: listRepos });
  const [question, setQuestion] = useState("");
  const [scopeIds, setScopeIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const onSubmit = async () => {
    if (!question.trim()) {
      toast.error("Question is required");
      return;
    }
    setSubmitting(true);
    try {
      await createAsk({ question: question.trim(), scope_repo_ids: scopeIds });
      toast.success("Ask routed");
      onCreated();
    } catch (err) {
      toast.error(`Failed: ${err instanceof ApiError ? err.status : String(err)}`);
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <div className="rounded-md border border-border bg-bg-1 p-3">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">New ask</div>
      <textarea
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        rows={3}
        placeholder="What's your question? E.g. 'How does FulfillmentService route carrier webhooks?'"
        className="w-full rounded border border-border bg-background px-2 py-1.5 text-[13px] focus:border-primary focus:outline-none"
      />
      <div className="mt-2">
        <label className="text-[11px] font-medium uppercase tracking-wider text-fg-2">
          Scope (repos involved — for auto-routing)
        </label>
        <select
          multiple
          value={scopeIds}
          onChange={(e) => setScopeIds(Array.from(e.target.selectedOptions).map((o) => o.value))}
          className="mt-1 w-full rounded border border-border bg-background px-2 py-1 text-[12px] focus:border-primary focus:outline-none"
          size={5}
        >
          {(repos.data ?? []).slice(0, 200).map((r) => (
            <option key={r.id} value={r.id}>{r.name}</option>
          ))}
        </select>
        <div className="mt-1 text-[10.5px] text-fg-3">
          ⌘/Ctrl-click to multi-select. We'll pick the dominant squad's slack channel.
        </div>
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onClose} disabled={submitting}>Cancel</Button>
        <Button size="sm" variant="default" onClick={onSubmit} disabled={submitting || !question.trim()}>
          {submitting && <Loader2 className="h-3 w-3 animate-spin" strokeWidth={1.75} />}
          Route + draft
        </Button>
      </div>
    </div>
  );
}


// ── P3 Review Queue panel ───────────────────────────────────────────


export function ReviewQueuePanel() {
  const [searchParams] = useSearchParams();
  const bucket = searchParams.get("bucket") ?? "all";
  const queue = useQuery<ReviewQueueResp>({
    queryKey: ["review-queue"],
    queryFn: () => getReviewQueue({}),
    staleTime: 60_000,
  });
  const allItems = useMemo(() => queue.data?.items ?? [], [queue.data]);

  // Bucket by ``source`` so the operator can drill into "PRs from my
  // tasks" vs "teammate PRs" vs "stale" without losing the unified
  // view. Ordering: own work first (most actionable), then teammate
  // PRs, then everything else.
  const sources = useMemo(() => {
    const seen = new Map<string, number>();
    for (const it of allItems) {
      seen.set(it.source, (seen.get(it.source) ?? 0) + 1);
    }
    return seen;
  }, [allItems]);

  const filtered = useMemo(
    () => (bucket === "all" ? allItems : allItems.filter((it) => it.source === bucket)),
    [allItems, bucket],
  );

  const buckets: Bucket[] = [
    {
      id: "all",
      label: "All",
      icon: <GitPullRequest className="h-3.5 w-3.5" strokeWidth={1.75} />,
      count: allItems.length,
      tone: "review",
    },
    ...Array.from(sources.entries()).map(
      ([src, count]): Bucket => ({
        id: src,
        label: src.replace(/_/g, " "),
        icon: <GitPullRequest className="h-3.5 w-3.5" strokeWidth={1.75} />,
        count,
        tone: src.includes("my") ? "ok" : src.includes("stale") ? "warn" : "default",
        hint: `Source: ${src}`,
      }),
    ),
  ];

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <header className="flex shrink-0 items-center justify-between rounded-md border border-border bg-bg-1 px-3 py-2">
        <div className="text-[13px]">
          <div className="font-semibold text-fg-0">PRs needing your attention</div>
          <div className="text-[11.5px] text-fg-3">{queue.data?.note ?? "Loading…"}</div>
        </div>
        <Button size="xs" variant="ghost" onClick={() => queue.refetch()} disabled={queue.isFetching}>
          <RefreshCw className={cn("h-3 w-3", queue.isFetching && "animate-spin")} strokeWidth={1.75} />
          Refresh
        </Button>
      </header>

      <MasterDetail<ReviewItem>
        className="min-h-0 flex-1"
        buckets={buckets}
        defaultBucketId="all"
        items={filtered}
        itemKey={(it) => it.pr_url}
        loading={queue.isLoading}
        renderItem={(it) => <ReviewRowLine it={it} />}
        renderDetail={(it) => (it ? <ReviewQuickView it={it} /> : null)}
        emptyList={
          <p className="text-center text-[12px] text-fg-3">
            No PRs in this bucket — clean inbox.
          </p>
        }
      />
    </div>
  );
}


function ReviewRowLine({ it }: { it: ReviewItem }) {
  return (
    <div className="flex items-start gap-2.5 px-3 py-2.5">
      <GitPullRequest
        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-review"
        strokeWidth={1.75}
      />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12.5px] font-medium text-fg-0">
          {it.title || it.pr_url}
          {it.pr_number && (
            <span className="ml-1 font-mono text-[11px] text-fg-3">
              #{it.pr_number}
            </span>
          )}
        </div>
        <div className="mt-0.5 truncate font-mono text-[10.5px] text-fg-3">
          {it.repo_name && <span>{it.repo_name} · </span>}
          {it.author && <span>{it.author}</span>}
        </div>
      </div>
    </div>
  );
}


function ReviewQuickView({ it }: { it: ReviewItem }) {
  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-border px-6 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-[15px] font-semibold tracking-tight text-fg-0">
              {it.title || it.pr_url}
            </h2>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-fg-3">
              {it.pr_number && (
                <span className="badge badge-review">#{it.pr_number}</span>
              )}
              {it.state && <span className="badge badge-neutral">{it.state}</span>}
              {it.repo_name && <span>{it.repo_name}</span>}
              {it.author && <span>· {it.author}</span>}
              <span>· seen {formatRelativeDate(it.last_seen_at)}</span>
            </div>
          </div>
          <a
            href={it.pr_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex shrink-0 items-center gap-1 rounded-md bg-accent px-2.5 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover"
          >
            Open PR
            <ExternalLink className="h-3 w-3" strokeWidth={2} />
          </a>
        </div>
      </header>

      <section className="border-b border-border px-6 py-4">
        <div className="section-title mb-1.5">Why it's here</div>
        <p className="text-[12.5px] text-fg-1">{it.rationale}</p>
        <p className="mt-1 font-mono text-[10.5px] text-fg-3">
          source: {it.source}
        </p>
      </section>

      {it.task_id && (
        <section className="px-6 py-4">
          <div className="section-title mb-1.5">Linked task</div>
          <a
            href={`/tasks/${it.task_id}`}
            className="block rounded-md border border-border bg-bg-2 px-3 py-2 transition-colors hover:border-border-strong"
          >
            <div className="truncate text-[12.5px] font-medium text-fg-0">
              {it.task_title || "Task"}
            </div>
            {it.task_state && (
              <div className="mt-0.5 font-mono text-[10.5px] text-fg-3">
                state: {it.task_state}
              </div>
            )}
          </a>
        </section>
      )}
    </div>
  );
}


// ── P4 Standup panel ────────────────────────────────────────────────


export function StandupPanel() {
  const draft = useQuery<StandupDraftResp>({
    queryKey: ["standup-draft"],
    queryFn: () => getStandupDraft({}),
    staleTime: 60_000,
  });
  const onCopy = async () => {
    if (!draft.data?.markdown) return;
    await navigator.clipboard.writeText(draft.data.markdown);
    toast.success("Standup draft copied");
  };
  return (
    <div className="space-y-3">
      <header className="flex items-center justify-between rounded-md border border-border bg-bg-1 px-3 py-2">
        <div className="text-[13px]">
          <div className="font-semibold text-fg-0">Weekly standup draft</div>
          <div className="text-[11.5px] text-fg-3">
            Auto-collated from your tasks + open asks. Slack mrkdwn — paste anywhere.
          </div>
        </div>
        <div className="flex gap-1">
          <Button size="xs" variant="ghost" onClick={() => draft.refetch()} disabled={draft.isFetching}>
            <RefreshCw className={cn("h-3 w-3", draft.isFetching && "animate-spin")} strokeWidth={1.75} />
            Refresh
          </Button>
          <Button size="xs" variant="default" onClick={onCopy} disabled={!draft.data?.markdown}>
            Copy
          </Button>
        </div>
      </header>
      {draft.isLoading ? (
        <div className="rounded-md border border-border bg-bg-1 p-6 text-center text-sm text-fg-2">
          <Loader2 className="mx-auto h-5 w-5 animate-spin" />
        </div>
      ) : (
        <pre className="whitespace-pre-wrap rounded-md border border-border bg-bg-1 p-4 font-mono text-[12.5px] leading-relaxed text-fg-0">
          {draft.data?.markdown || "(empty — no recent activity)"}
        </pre>
      )}
    </div>
  );
}


// ── P5 Domain Hub panel ────────────────────────────────────────────


export function DomainHubPanel() {
  const [squad, setSquad] = useState("");
  const hub = useQuery<DomainHubResp>({
    queryKey: ["domain-hub", squad],
    queryFn: () => getDomainHub(squad, 30),
    enabled: !!squad,
    staleTime: 60_000,
  });
  return (
    <div className="space-y-3">
      <header className="flex items-center gap-3 rounded-md border border-border bg-bg-1 px-3 py-2">
        <span className="text-[13px] font-semibold text-fg-0">Domain knowledge hub</span>
        <input
          type="text"
          value={squad}
          onChange={(e) => setSquad(e.target.value.toUpperCase())}
          className="w-32 rounded border border-border bg-background px-2 py-1 font-mono text-sm uppercase focus:border-primary focus:outline-none"
        />
        {hub.data && <span className="font-mono text-[10.5px] text-fg-3">{hub.data.label}</span>}
      </header>

      {hub.isLoading && (
        <div className="rounded-md border border-border bg-bg-1 p-6 text-center"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></div>
      )}

      {hub.data && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <section className="rounded-md border border-border bg-bg-1 p-3">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">
              Where to ask
            </div>
            <div className="flex flex-wrap gap-1.5 text-[12px]">
              {hub.data.slack_channels.map((c) => (
                <span key={c} className="rounded-full bg-bg-2 px-2 py-0.5 font-mono text-[11px]">{c}</span>
              ))}
              {hub.data.jira_projects.map((p) => (
                <span key={p} className="rounded-full bg-info-soft px-2 py-0.5 font-mono text-[11px] text-info">{p}</span>
              ))}
              {hub.data.slack_channels.length + hub.data.jira_projects.length === 0 && <Empty />}
            </div>
          </section>

          <section className="rounded-md border border-border bg-bg-1 p-3">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">Top contributors</div>
            <div className="flex flex-wrap gap-1.5">
              {hub.data.top_contributors.length === 0 ? <Empty /> : hub.data.top_contributors.map((c) => (
                <span key={c.email} className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-2 px-2 py-0.5 text-[11px]">
                  {c.email}
                  <span className="font-mono text-[10px] text-fg-3">{c.activity_count}</span>
                </span>
              ))}
            </div>
          </section>

          <section className="rounded-md border border-border bg-bg-1 p-3 lg:col-span-2">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">Services ({hub.data.services.length})</div>
            <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2 xl:grid-cols-3">
              {hub.data.services.length === 0 ? <Empty /> : hub.data.services.map((s) => (
                <li key={s.repo_id} className="truncate rounded border border-border bg-bg-2 px-2 py-1 text-[12px]">
                  <a href={`/knowledge/repos/${s.repo_id}`} className="font-medium text-fg-0 hover:underline">{s.name}</a>
                  <div className="text-[10.5px] text-fg-3">
                    {s.component_count} components · {s.flow_count} flows
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="rounded-md border border-border bg-bg-1 p-3">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">Recent design docs</div>
            <ul className="space-y-1">
              {hub.data.recent_design_docs.length === 0 ? <Empty /> : hub.data.recent_design_docs.map((g, i) => (
                <li key={i} className="truncate text-[12px]">
                  <a href={g.url} target="_blank" rel="noreferrer" className="text-fg-1 hover:underline">
                    <FileText className="mr-1 inline h-3 w-3" strokeWidth={1.75} />
                    {g.title}
                  </a>
                </li>
              ))}
            </ul>
          </section>

          <section className="rounded-md border border-border bg-bg-1 p-3">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-fg-2">Active initiatives</div>
            <ul className="space-y-1">
              {hub.data.active_epics.length === 0 ? <Empty /> : hub.data.active_epics.map((e) => (
                <li key={e.key} className="truncate text-[12px]">
                  <a href={e.url} target="_blank" rel="noreferrer" className="text-fg-1 hover:underline">
                    <span className="mr-1 font-mono text-[11px]">{e.key}</span>
                    {e.title}
                  </a>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}
