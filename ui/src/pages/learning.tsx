import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Activity,
  TrendingDown,
  TrendingUp,
  Hourglass,
  AlertCircle,
  CheckCircle2,
  MinusCircle,
  Gauge,
  Zap,
  Database,
  Target,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Inbox,
  Scale,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  getAgentQuality,
  getBenchmarksDomains,
  getBenchmarksQuestions,
  getBenchmarksRuns,
  getBenchmarksSummary,
  getCacheStats,
  getLearningStats,
  listLearningDeltas,
  listLearningJudgements,
  listPendingUpgrades,
  type AgentQualitySnapshot,
  type BenchmarkBuild,
  type BenchmarkQuestionRow,
  type BenchmarkRunRow,
  type ConfidenceDelta,
  type PendingUpgrade,
  type AgentJudgement,
} from "@/lib/api";

/**
 * Admin • Learning — observational view of the continuous-learning loop.
 *
 * Design intent:
 *   - Operators need to see *whether the loop is actually learning* —
 *     that means lots of signals flowing in, few stuck in "pending"
 *     limbo, and a healthy mix of up/down adjustments over time.
 *   - Data-dense by default; no hidden tabs for first 80% of the job.
 *   - Tiny mono-font numbers for everything quantitative (tokens,
 *     confidences, counts) — matches the power-user aesthetic of the
 *     rest of Bifrost.
 */
export function LearningPage() {
  const stats = useQuery({
    queryKey: ["learning-stats"],
    queryFn: getLearningStats,
    refetchInterval: 10_000,
  });
  const deltas = useQuery({
    queryKey: ["learning-deltas"],
    queryFn: () => listLearningDeltas({ limit: 100 }),
    refetchInterval: 10_000,
  });
  const pending = useQuery({
    queryKey: ["learning-pending"],
    queryFn: () => listPendingUpgrades(50),
    refetchInterval: 10_000,
  });
  const judgements = useQuery({
    queryKey: ["learning-judgements"],
    queryFn: () => listLearningJudgements({ limit: 50 }),
    refetchInterval: 15_000,
  });
  const quality = useQuery({
    queryKey: ["agent-quality"],
    queryFn: () => getAgentQuality({ window_days: 30, trend_window_days: 7 }),
    refetchInterval: 15_000,
  });

  const [filter, setFilter] = useState<"all" | "up" | "down" | "pending" | "noise">("all");
  const filtered = (deltas.data ?? []).filter((d) => {
    if (filter === "all") return true;
    if (filter === "up") return d.applied_by.startsWith("auto_upgrade");
    if (filter === "down") return d.applied_by.startsWith("auto_downgrade");
    if (filter === "pending") return d.applied_by.startsWith("pending_upgrade");
    if (filter === "noise") return d.applied_by.startsWith("noise");
    return true;
  });

  return (
    <div className="mx-auto max-w-[1200px] px-8 py-6">
      <header className="mb-3 flex items-baseline gap-3">
        <h1 className="text-[22px] font-semibold tracking-tight text-fg-0">
          Learning
        </h1>
        <p className="text-[12.5px] text-fg-3">
          Bifrost gets smarter every turn. Capture · Govern · Apply ·
          Consume · Measure.
        </p>
      </header>

      {/* Narrative — what this page is about, in plain English. */}
      <div className="mb-4 rounded-md border border-border bg-bg-1 px-4 py-3 text-[12.5px] text-fg-1">
        Every chat, every task, every benchmark produces structured
        signals. A governance layer turns those signals into graph
        updates. The next query reads from that graph instead of
        re-deriving everything from scratch.
      </div>

      {/* Collapsible — the 5-layer flow diagram + per-layer explainer.
          The defaults: closed for return visitors, open for first-time
          operators (sessionStorage flag). */}
      <HowLearningWorks />

      {/* Stat cards — Layer 1: Capture (signals captured) + Layer 2:
          Govern (decisions made). Operators want both numbers in the
          same eye-line so they can spot capture-without-application
          bottlenecks immediately. */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Actionable deltas"
          value={stats.data?.deltas.actionable}
          hint={
            `${stats.data?.deltas.total ?? "…"} logged · ` +
            `${stats.data?.deltas.flat ?? "…"} audit-only · ` +
            `${stats.data?.deltas.last_7_days ?? "…"} in 7d`
          }
          icon={<Activity className="h-3 w-3" strokeWidth={1.75} />}
        />
        <StatCard
          label="Auto-upgrades"
          value={stats.data?.deltas.auto_upgrades}
          hint={`${stats.data?.deltas.pending_upgrades ?? "…"} pending`}
          icon={<TrendingUp className="h-3 w-3" strokeWidth={1.75} />}
          tone="ok"
        />
        <StatCard
          label="Auto-downgrades"
          value={stats.data?.deltas.auto_downgrades}
          hint="applied immediately"
          icon={<TrendingDown className="h-3 w-3" strokeWidth={1.75} />}
          tone="warn"
        />
        <StatCard
          label="Targets touched"
          value={stats.data?.deltas.targets_touched}
          hint={`${stats.data?.deltas.tasks_involved ?? "…"} tasks`}
          icon={<AlertCircle className="h-3 w-3" strokeWidth={1.75} />}
        />
      </section>

      {/* Governance explainer */}
      <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-bg-1 px-3 py-2 text-[11.5px] text-fg-2">
        <MinusCircle className="h-3 w-3 shrink-0 text-fg-3" strokeWidth={1.75} />
        <span>
          Upgrades need{" "}
          <span className="font-mono font-semibold text-fg-0">
            {stats.data?.config.upgrade_confirmations_needed ?? "…"}
          </span>{" "}
          distinct-task confirmations. Noise floor{" "}
          <span className="font-mono font-semibold text-fg-0">
            ±{stats.data?.config.noise_floor ?? "…"}
          </span>{" "}
          keeps tiny deltas out of the graph.
        </span>
      </div>

      {/* ━━━━━━ Layer 3: Apply ━━━━━━ */}
      <LayerBanner
        layer="3"
        title="Apply — knowledge stored"
        explainer={
          "Governed deltas flow into the graph (entities + entity_relationships) " +
          "and into the permanent agent_results store. The 'knowledge cache' below " +
          "isn't a TTL cache — rows are kept forever; the 24h is just the activity " +
          "window. Hit rate climbing over weeks = the loop is accumulating value."
        }
      />
      <KnowledgeCacheCard />

      {/* ━━━━━━ Layer 5: Measure ━━━━━━ */}
      <LayerBanner
        layer="5"
        title="Measure — did it work?"
        explainer={
          "Three benchmark run kinds prove the loop. A (cold) is the baseline cost; " +
          "B (warm) should be cheaper at the same quality; C (paraphrased) proves " +
          "the semantic cache holds across rewordings. Regressions on any axis " +
          "(recall / precision / specificity / coverage / tier integrity / evidence " +
          "consistency / format) flag a broken assumption in the loop."
        }
      />
      <BenchmarkScoreboardCard />

      {/* ━━━━━━ Layer 2: Govern (the agent-quality view) ━━━━━━ */}
      <LayerBanner
        layer="2"
        title="Govern — agent quality"
        explainer={
          "Verify-distribution = how often each agent's claims hold up. Plan outcomes " +
          "+ readiness buckets show where tasks stall. Assumption tier distribution " +
          "tells you whether the system is grounding answers in code (T1) or low-tier " +
          "evidence (T4/T5). Quick mode + agent metrics expose cost + latency."
        }
      />
      {quality.data && <AgentQualityDashboard q={quality.data} />}

      {/* ━━━━━━ Layer 2: Govern (pending + recent deltas) ━━━━━━ */}
      <LayerBanner
        layer="2"
        title="Govern — what the rules decided"
        explainer={
          "Pending upgrades are signals that need a 2nd cross-task or cross-conversation " +
          "confirmation before they're applied (Rule 2). Recent deltas is a live tail of " +
          "every signal — filter by applied state. Auto-downgrades fire on any single " +
          "contradicting signal (Rule 1). |Δ| < 0.05 stays as 'noise' for audit only."
        }
      />
      <section className="mt-3 rounded-lg border border-border bg-bg-1">
        <header className="flex items-center justify-between border-b border-border px-4 py-2">
          <h2 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
            <Hourglass className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
            Pending upgrades
            <span className="font-mono text-[10.5px] text-fg-3">
              ({pending.data?.length ?? "…"})
            </span>
          </h2>
          <span className="text-[10.5px] text-fg-3">
            Need a 2nd distinct-task signal to promote
          </span>
        </header>
        {pending.data?.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-fg-3">
            No pending upgrades — nothing is stuck.
          </div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="bg-bg-2 text-[10.5px] uppercase tracking-wide text-fg-3">
              <tr>
                <th className="px-3 py-1.5 text-left">Target</th>
                <th className="px-3 py-1.5 text-right">Current</th>
                <th className="px-3 py-1.5 text-right">Proposed</th>
                <th className="px-3 py-1.5 text-center">Confirmations</th>
                <th className="px-3 py-1.5 text-right">First seen</th>
              </tr>
            </thead>
            <tbody>
              {(pending.data ?? []).map((p) => (
                <PendingRow key={p.target_id} p={p} />
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Deltas */}
      <section className="mt-5 rounded-lg border border-border bg-bg-1">
        <header className="flex items-center justify-between border-b border-border px-4 py-2">
          <h2 className="text-[12.5px] font-semibold text-fg-1">
            Recent deltas
            <span className="ml-1.5 font-mono text-[10.5px] text-fg-3">
              ({filtered.length}/{deltas.data?.length ?? 0})
            </span>
          </h2>
          <div className="flex gap-1">
            {(["all", "up", "down", "pending", "noise"] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={cn(
                  "rounded-full px-2 py-0.5 text-[10.5px] font-medium",
                  filter === f
                    ? "bg-accent text-bg-0"
                    : "bg-bg-2 text-fg-2 hover:text-fg-0",
                )}
              >
                {f}
              </button>
            ))}
          </div>
        </header>
        {filtered.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-fg-3">
            No deltas match this filter.
          </div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="bg-bg-2 text-[10.5px] uppercase tracking-wide text-fg-3">
              <tr>
                <th className="px-3 py-1.5 text-left">When</th>
                <th className="px-3 py-1.5 text-left">Target</th>
                <th className="px-3 py-1.5 text-right">Confidence</th>
                <th className="px-3 py-1.5 text-left">Reason</th>
                <th className="px-3 py-1.5 text-left">Applied by</th>
                <th className="px-3 py-1.5 text-left">Task</th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 80).map((d) => (
                <DeltaRow key={d.id} d={d} />
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Judgements */}
      <section className="mt-5 rounded-lg border border-border bg-bg-1">
        <header className="flex items-center justify-between border-b border-border px-4 py-2">
          <h2 className="text-[12.5px] font-semibold text-fg-1">
            Agent judgements
            <span className="ml-1.5 font-mono text-[10.5px] text-fg-3">
              ({judgements.data?.length ?? 0})
            </span>
          </h2>
          <span className="text-[10.5px] text-fg-3">
            Claims to be graded after task termination
          </span>
        </header>
        {(judgements.data ?? []).length === 0 ? (
          <div className="py-6 text-center text-[12px] text-fg-3">
            Layer 7+ will populate this as agents make claims. Today it's a
            storage surface.
          </div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="bg-bg-2 text-[10.5px] uppercase tracking-wide text-fg-3">
              <tr>
                <th className="px-3 py-1.5 text-left">Agent</th>
                <th className="px-3 py-1.5 text-left">Claim</th>
                <th className="px-3 py-1.5 text-right">Citations</th>
                <th className="px-3 py-1.5 text-left">Outcome</th>
                <th className="px-3 py-1.5 text-left">When</th>
              </tr>
            </thead>
            <tbody>
              {(judgements.data ?? []).slice(0, 50).map((j) => (
                <JudgementRow key={j.id} j={j} />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}


/* ── Sub-components ──────────────────────────────────────────── */


function StatCard({
  label,
  value,
  hint,
  icon,
  tone = "fg",
}: {
  label: string;
  value: number | undefined;
  hint?: string;
  icon?: React.ReactNode;
  tone?: "fg" | "ok" | "warn" | "err" | "accent";
}) {
  const toneClass = {
    fg: "text-fg-0",
    ok: "text-ok",
    warn: "text-warn",
    err: "text-err",
    accent: "text-accent",
  }[tone];
  return (
    <div className="rounded-lg border border-border bg-bg-1 px-3 py-2.5">
      <div className="flex items-center gap-1 text-[10.5px] uppercase tracking-wide text-fg-3">
        {icon}
        {label}
      </div>
      <div className={cn("mt-1 font-mono text-[22px] font-semibold tabular-nums", toneClass)}>
        {value === undefined ? "…" : value.toLocaleString()}
      </div>
      {hint && (
        <div className="mt-0.5 text-[10.5px] text-fg-3">{hint}</div>
      )}
    </div>
  );
}


function PendingRow({ p }: { p: PendingUpgrade }) {
  // Use the effective confirmation count (max of task- and conversation-
  // origin signals), not distinct_tasks alone — chat-origin pendings have
  // distinct_tasks=0 and would otherwise always read 0/N.
  const gotPct = Math.min(100, (p.confirmations / p.confirmations_needed) * 100);
  return (
    <tr className="border-t border-border/60 align-top">
      <td className="px-3 py-1.5">
        <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
          {p.target_kind}
        </span>
        <span className="ml-1.5 font-mono text-[11px] text-fg-2">
          {p.target_id.slice(0, 8)}…
        </span>
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-fg-2">
        {p.current_confidence?.toFixed(2) ?? "—"}
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-fg-0">
        {p.max_proposed_confidence?.toFixed(2) ?? "—"}
      </td>
      <td className="px-3 py-1.5">
        <div className="flex items-center justify-center gap-1.5">
          <span className="font-mono text-[11px] text-fg-2">
            {p.confirmations}/{p.confirmations_needed}
          </span>
          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-bg-2">
            <div
              className="h-full bg-accent"
              style={{ width: `${gotPct}%` }}
            />
          </div>
        </div>
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-[10.5px] text-fg-3">
        {p.first_seen ? relTime(p.first_seen) : "—"}
      </td>
    </tr>
  );
}


function DeltaRow({ d }: { d: ConfidenceDelta }) {
  const dir =
    d.applied_by.startsWith("auto_upgrade") || d.applied_by.startsWith("pending_upgrade")
      ? "up"
      : d.applied_by.startsWith("auto_downgrade")
        ? "down"
        : d.applied_by.startsWith("noise")
          ? "noop"
          : "other";
  return (
    <tr className="border-t border-border/60 align-top hover:bg-bg-2/40">
      <td className="px-3 py-1.5 font-mono text-[10.5px] text-fg-3">
        {relTime(d.applied_at)}
      </td>
      <td className="px-3 py-1.5">
        <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
          {d.target_kind}
        </span>
        <span className="ml-1.5 font-mono text-[10.5px] text-fg-2">
          {d.target_id.slice(0, 8)}…
        </span>
      </td>
      <td className="px-3 py-1.5 text-right">
        <span className="font-mono text-[11px] text-fg-3">
          {d.old_confidence?.toFixed(2) ?? "—"}
        </span>
        <span
          className={cn(
            "mx-1 inline-block font-mono",
            dir === "up" && "text-ok",
            dir === "down" && "text-warn",
            dir === "noop" && "text-fg-3",
          )}
        >
          {dir === "up" ? "↑" : dir === "down" ? "↓" : "·"}
        </span>
        <span
          className={cn(
            "font-mono text-[11px]",
            dir === "up" ? "font-semibold text-ok" : "text-fg-0",
          )}
        >
          {d.new_confidence.toFixed(2)}
        </span>
      </td>
      <td className="px-3 py-1.5 font-mono text-[10.5px] text-fg-2">
        {d.reason}
      </td>
      <td className="px-3 py-1.5">
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 font-mono text-[10.5px]",
            dir === "up" && d.applied_by.startsWith("auto")
              ? "bg-ok-soft text-ok"
              : dir === "down"
                ? "bg-warn-soft text-warn"
                : dir === "up"
                  ? "bg-bg-2 text-fg-3"
                  : "bg-bg-2 text-fg-3",
          )}
        >
          {d.applied_by}
        </span>
      </td>
      <td className="px-3 py-1.5 font-mono text-[10.5px] text-fg-3">
        {d.task_id ? d.task_id.slice(0, 8) + "…" : "—"}
      </td>
    </tr>
  );
}


function JudgementRow({ j }: { j: AgentJudgement }) {
  const Icon =
    j.outcome === "confirmed"
      ? CheckCircle2
      : j.outcome === "contradicted"
        ? AlertCircle
        : Hourglass;
  const tone =
    j.outcome === "confirmed"
      ? "text-ok"
      : j.outcome === "contradicted"
        ? "text-err"
        : "text-fg-3";
  return (
    <tr className="border-t border-border/60 align-top">
      <td className="px-3 py-1.5">
        <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
          {j.agent_kind}
        </span>
      </td>
      <td className="px-3 py-1.5 text-fg-0">
        {j.claim.length > 180 ? j.claim.slice(0, 180) + "…" : j.claim}
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-[10.5px] text-fg-2">
        {j.cited_urns.length}
      </td>
      <td className="px-3 py-1.5">
        <span className={cn("inline-flex items-center gap-1", tone)}>
          <Icon className="h-3 w-3" strokeWidth={1.75} />
          <span className="font-mono text-[10.5px]">{j.outcome}</span>
        </span>
      </td>
      <td className="px-3 py-1.5 font-mono text-[10.5px] text-fg-3">
        {relTime(j.created_at)}
      </td>
    </tr>
  );
}


function relTime(iso: string): string {
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}


/* ── Agent quality dashboard ─────────────────────────────────── */


function AgentQualityDashboard({ q }: { q: AgentQualitySnapshot }) {
  // All-zero payload (no verify/plan/quick activity, no per-agent rows) would
  // otherwise render six blank cards that read as "broken". Show one honest
  // empty-state instead until the learning loop has signal.
  const isEmpty =
    q.agent_metrics.length === 0 &&
    q.verify_distribution.total === 0 &&
    q.plan_outcomes.total === 0 &&
    q.quick_mode.invocations === 0 &&
    q.assumption_tiers.total_active === 0;
  return (
    <section className="mt-5">
      <header className="mb-3 flex items-baseline gap-2">
        <h2 className="text-[13.5px] font-semibold text-fg-0">Agent quality</h2>
        <p className="text-[11px] text-fg-3">
          last <span className="font-mono text-fg-1">{q.window_days}d</span> ·
          recent window{" "}
          <span className="font-mono text-fg-1">{q.trend_window_days}d</span>
        </p>
      </header>
      {isEmpty ? (
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-6 text-center">
          <p className="text-[13px] font-medium text-fg-1">
            No agent-quality signal yet
          </p>
          <p className="mx-auto mt-1 max-w-md text-[11.5px] text-fg-3">
            These metrics populate once tasks run through verify / plan / quick
            flows in the last {q.window_days} days. Run a task to completion (or
            widen the window) and this fills in — verify distribution, plan
            outcomes, assumption tiers, and per-agent cost/latency.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <VerifyDistroCard q={q} />
          <PlanOutcomesCard q={q} />
          <AssumptionTiersCard q={q} />
          <PlanReadinessCard q={q} />
          <QuickModeCard q={q} />
          <AgentMetricsCard q={q} />
        </div>
      )}
    </section>
  );
}


function VerifyDistroCard({ q }: { q: AgentQualitySnapshot }) {
  const vd = q.verify_distribution;
  const order = ["pass", "fail", "skipped", "pending"];
  const byStatus = [...vd.by_status].sort(
    (a, b) => order.indexOf(a.status) - order.indexOf(b.status),
  );
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-3">
      <header className="flex items-center justify-between pb-2">
        <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <CheckCircle2 className="h-3 w-3 text-ok" strokeWidth={1.75} />
          Verify items
          <span className="font-mono text-[10.5px] text-fg-3">
            {vd.total.toLocaleString()}
          </span>
        </h3>
        <span className="font-mono text-[10.5px] text-fg-3">
          recent: {vd.total_recent}
        </span>
      </header>
      <StackedBar
        segments={byStatus.map((s) => ({
          label: s.status,
          n: s.n,
          tone: _statusTone(s.status),
        }))}
      />
      <div className="mt-2 grid grid-cols-4 gap-1.5 text-[10.5px]">
        {byStatus.map((s) => (
          <div
            key={s.status}
            className="flex flex-col rounded bg-bg-0 px-2 py-1"
          >
            <span
              className={cn(
                "font-mono text-[10px] uppercase tracking-wide",
                _statusToneText(s.status),
              )}
            >
              {s.status}
            </span>
            <span className="font-mono text-[13px] font-semibold text-fg-0">
              {(s.pct * 100).toFixed(0)}%
            </span>
            <span className="font-mono text-[10px] text-fg-3">
              {s.n}
              {s.recent > 0 && (
                <>
                  {" · "}
                  <span className="text-fg-2">
                    {(s.recent_pct * 100).toFixed(0)}%
                  </span>{" "}
                  recent
                </>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


function PlanOutcomesCard({ q }: { q: AgentQualitySnapshot }) {
  const po = q.plan_outcomes;
  const order = [
    "completed", "approved", "pending", "executing", "failed", "rejected",
  ];
  const byStatus = [...po.by_status].sort(
    (a, b) => order.indexOf(a.status) - order.indexOf(b.status),
  );
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-3">
      <header className="flex items-center justify-between pb-2">
        <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <Activity className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
          Plan outcomes
          <span className="font-mono text-[10.5px] text-fg-3">
            {po.total}
          </span>
        </h3>
      </header>
      <StackedBar
        segments={byStatus.map((s) => ({
          label: s.status,
          n: s.n,
          tone: _planTone(s.status),
        }))}
      />
      <div className="mt-2 flex flex-wrap gap-1.5 text-[10.5px]">
        {byStatus.map((s) => (
          <span
            key={s.status}
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-mono",
              _planToneBg(s.status),
            )}
          >
            <span className="font-semibold">{s.n}</span>
            {s.status}
            <span className="text-fg-3">{(s.pct * 100).toFixed(0)}%</span>
          </span>
        ))}
      </div>
    </div>
  );
}


function AssumptionTiersCard({ q }: { q: AgentQualitySnapshot }) {
  const at = q.assumption_tiers;
  const tierColors: Record<number, string> = {
    1: "bg-ok",
    2: "bg-ok/70",
    3: "bg-info",
    4: "bg-warn",
    5: "bg-err",
  };
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-3">
      <header className="flex items-center justify-between pb-2">
        <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <Gauge className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
          Assumption tiers
          <span className="font-mono text-[10.5px] text-fg-3">
            {at.total_active} active
          </span>
        </h3>
      </header>
      <StackedBar
        segments={at.by_tier.map((t) => ({
          label: `T${t.tier}`,
          n: t.n,
          tone: tierColors[t.tier] ?? "bg-fg-3",
        }))}
      />
      <div className="mt-2 grid grid-cols-5 gap-1.5 text-[10.5px]">
        {[1, 2, 3, 4, 5].map((tier) => {
          const row = at.by_tier.find((t) => t.tier === tier);
          const n = row?.n ?? 0;
          const pct = row?.pct ?? 0;
          return (
            <div key={tier} className="flex flex-col rounded bg-bg-0 px-2 py-1">
              <span className="font-mono text-[10px] uppercase tracking-wide text-fg-3">
                T{tier}
              </span>
              <span className="font-mono text-[13px] font-semibold text-fg-0">
                {(pct * 100).toFixed(0)}%
              </span>
              <span className="font-mono text-[10px] text-fg-3">{n}</span>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-[10.5px] text-fg-3">
        Shift toward T5 = discussion agent guessing more; shift toward T1 =
        clarifications turning guesses into user-stated facts.
      </p>
    </div>
  );
}


function PlanReadinessCard({ q }: { q: AgentQualitySnapshot }) {
  const pr = q.plan_readiness;
  const ready = pr.ready;
  const notReady = pr.not_ready;
  const optimism =
    pr.optimism_score === null ? null : pr.optimism_score * 100;
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-3">
      <header className="pb-2">
        <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <TrendingUp className="h-3 w-3 text-ok" strokeWidth={1.75} />
          Plan readiness vs reality
        </h3>
      </header>
      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <ReadinessBucket
          label="ready_to_execute"
          bucket={ready}
          tone="ok"
        />
        <ReadinessBucket
          label="not ready"
          bucket={notReady}
          tone="fg"
        />
      </div>
      <div className="mt-2 rounded bg-bg-0 px-2 py-1 text-[11px] text-fg-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-fg-3">
          optimism score
        </span>
        <span className="ml-2 font-mono text-[14px] font-semibold text-fg-0 tabular-nums">
          {optimism === null ? "—" : `${optimism.toFixed(0)}%`}
        </span>
        <span className="ml-1 text-[10.5px] text-fg-3">
          of ready phases that actually landed every plan
        </span>
      </div>
    </div>
  );
}


function ReadinessBucket({
  label,
  bucket,
  tone,
}: {
  label: string;
  bucket: { n_phases: number; all_completed: number; any_failed: number };
  tone: "ok" | "fg";
}) {
  const toneClass = tone === "ok" ? "text-ok" : "text-fg-2";
  return (
    <div className="rounded bg-bg-0 px-2 py-1.5">
      <div className={cn("font-mono text-[10px] uppercase tracking-wide", toneClass)}>
        {label}
      </div>
      <div className="mt-0.5 flex items-baseline gap-2">
        <span className="font-mono text-[14px] font-semibold text-fg-0 tabular-nums">
          {bucket.n_phases}
        </span>
        <span className="font-mono text-[10.5px] text-fg-3">phases</span>
      </div>
      <div className="mt-0.5 flex gap-2 text-[10px]">
        <span className="font-mono text-ok">
          ✓ {bucket.all_completed} all-pass
        </span>
        <span className="font-mono text-err">
          ✗ {bucket.any_failed} any-fail
        </span>
      </div>
    </div>
  );
}


function QuickModeCard({ q }: { q: AgentQualitySnapshot }) {
  const qm = q.quick_mode;
  return (
    <div className="rounded-lg border border-border bg-bg-1 p-3">
      <header className="pb-2">
        <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <Zap className="h-3 w-3 text-ok" strokeWidth={1.75} />
          Quick-mode tasks
        </h3>
      </header>
      {qm.invocations === 0 ? (
        <p className="text-[11px] text-fg-3">
          No /quick invocations in this window.
        </p>
      ) : (
        <>
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[18px] font-semibold text-fg-0 tabular-nums">
              {qm.invocations}
            </span>
            <span className="text-[11px] text-fg-3">invocations</span>
            <span className="ml-auto font-mono text-[13px] font-semibold text-ok tabular-nums">
              {(qm.success_rate * 100).toFixed(0)}%
            </span>
            <span className="text-[10.5px] text-fg-3">success</span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1 text-[10.5px]">
            <span className="inline-flex items-center gap-1 rounded-full bg-ok-soft px-1.5 py-0.5 font-mono text-ok">
              <span className="font-semibold">{qm.successes}</span> success
            </span>
            <span className="inline-flex items-center gap-1 rounded-full bg-err-soft px-1.5 py-0.5 font-mono text-err">
              <span className="font-semibold">{qm.blocked}</span> blocked
            </span>
            {qm.cancelled > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-fg-2">
                <span className="font-semibold">{qm.cancelled}</span> cancelled
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}


function AgentMetricsCard({ q }: { q: AgentQualitySnapshot }) {
  const rows = q.agent_metrics.filter((m) => m.n > 0);
  return (
    <div className="rounded-lg border border-border bg-bg-1 lg:col-span-2">
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <Gauge className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
          Per-agent cost + latency
        </h3>
      </header>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-[11px] text-fg-3">
          No agent activity in this window.
        </p>
      ) : (
        <table className="w-full text-[12px]">
          <thead className="bg-bg-2 text-[10.5px] uppercase tracking-wide text-fg-3">
            <tr>
              <th className="px-3 py-1.5 text-left">Agent</th>
              <th className="px-3 py-1.5 text-right">Calls</th>
              <th className="px-3 py-1.5 text-right">Cost</th>
              <th className="px-3 py-1.5 text-right">Tokens</th>
              <th className="px-3 py-1.5 text-right">Avg duration</th>
              <th className="px-3 py-1.5 text-right">Avg confidence</th>
              <th className="px-3 py-1.5 text-right">Budget hits</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.agent_kind}
                className="border-t border-border/60 align-top hover:bg-bg-2/40"
              >
                <td className="px-3 py-1.5">
                  <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
                    {r.agent_kind}
                  </span>
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-fg-0">
                  {r.n}
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-fg-1">
                  ${r.cost_usd.toFixed(3)}
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-fg-2">
                  {r.tokens.toLocaleString()}
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-fg-2">
                  {(r.avg_duration_ms / 1000).toFixed(1)}s
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-fg-2">
                  {r.avg_confidence.toFixed(2)}
                </td>
                <td
                  className={cn(
                    "px-3 py-1.5 text-right font-mono",
                    r.budget_hits > 0 ? "text-err font-semibold" : "text-fg-3",
                  )}
                >
                  {r.budget_hits}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


function StackedBar({
  segments,
}: {
  segments: Array<{ label: string; n: number; tone: string }>;
}) {
  const total = segments.reduce((a, s) => a + s.n, 0);
  if (total === 0) {
    return (
      <div className="h-2 rounded-full bg-bg-0" />
    );
  }
  return (
    <div className="flex h-2 overflow-hidden rounded-full bg-bg-0">
      {segments.map((s, i) => {
        const pct = (s.n / total) * 100;
        if (pct <= 0) return null;
        return (
          <div
            key={s.label + i}
            className={cn(s.tone, "h-full")}
            style={{ width: `${pct}%` }}
            title={`${s.label}: ${s.n} (${pct.toFixed(1)}%)`}
          />
        );
      })}
    </div>
  );
}


function _statusTone(status: string): string {
  return {
    pass: "bg-ok",
    fail: "bg-err",
    skipped: "bg-bg-3",
    pending: "bg-fg-3/50",
  }[status] ?? "bg-fg-3";
}
function _statusToneText(status: string): string {
  return {
    pass: "text-ok",
    fail: "text-err",
    skipped: "text-fg-2",
    pending: "text-fg-3",
  }[status] ?? "text-fg-3";
}
function _planTone(status: string): string {
  return {
    completed: "bg-ok",
    approved: "bg-ok/70",
    pending: "bg-bg-3",
    executing: "bg-info animate-pulse",
    failed: "bg-err",
    rejected: "bg-warn",
  }[status] ?? "bg-fg-3";
}
function _planToneBg(status: string): string {
  return {
    completed: "bg-ok-soft text-ok",
    approved: "bg-ok-soft/60 text-ok",
    pending: "bg-bg-2 text-fg-2",
    executing: "bg-info-soft text-info",
    failed: "bg-err-soft text-err",
    rejected: "bg-warn-soft text-warn",
  }[status] ?? "bg-bg-2 text-fg-2";
}


/* ── Knowledge base card ──────────────────────────────────────────────
 *
 * Bifrost's agent_results table is a PERMANENT knowledge base — rows
 * live forever, queries reuse them across conversations.  This card
 * shows two views side-by-side:
 *
 *   1. Lifetime — the headline "what does Bifrost know?" totals.
 *      Distinct targets, distinct conversations, total agent runs,
 *      money saved across all runs.
 *
 *   2. Recent activity (last 24h) — is the loop active right now?
 *      Hit rate, fresh runs, $ saved this window.
 *
 * Per-kind breakdown + top reused targets cover the same 24h window
 * so operators can see where the reuse is coming from.
 */
function KnowledgeCacheCard() {
  const cache = useQuery({
    queryKey: ["cache-stats", 24],
    queryFn: () => getCacheStats(24),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });

  if (cache.isError) {
    return (
      <section className="mt-5 rounded-lg border border-border bg-bg-1 p-4">
        <div className="flex items-center gap-2 text-[12px] text-warn">
          <Database className="h-3.5 w-3.5" strokeWidth={1.75} />
          Could not load knowledge base stats.
        </div>
      </section>
    );
  }

  const c = cache.data;
  const lifetime = c?.lifetime;
  const totals = c?.totals;
  const byKind = c?.by_kind ?? {};
  const top = c?.top_targets ?? [];
  const kinds: Array<keyof typeof byKind> = [
    "repo", "jira", "confluence", "github", "slack", "external",
  ];

  const ltHitRate = ((lifetime?.hit_rate ?? 0) * 100).toFixed(1);
  const ltSaved = lifetime?.runs_saved_cost_usd ?? 0;
  const ltTotal = lifetime?.runs_total_cost_usd ?? 0;
  const ltGross = ltTotal + ltSaved;
  const ltSavedPct = ltGross > 0 ? ((ltSaved / ltGross) * 100).toFixed(1) : "0.0";

  const winHitRate = ((totals?.hit_rate ?? 0) * 100).toFixed(1);
  const winSaved = totals?.saved_cost_usd ?? 0;
  const winTotal = totals?.total_cost_usd ?? 0;

  return (
    <section className="mt-5 rounded-lg border border-border bg-bg-1">
      <header className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Database className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          <h3 className="text-[13px] font-semibold tracking-tight text-fg-0">
            Knowledge base
          </h3>
          <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fg-2">
            permanent
          </span>
        </div>
        <span className="font-mono text-[11px] text-fg-3">
          {lifetime?.runs ?? 0} runs lifetime · ${ltSaved.toFixed(2)} saved
          ({ltSavedPct}% of gross)
        </span>
      </header>

      {/* LIFETIME row — the "what Bifrost knows" headline */}
      <div className="border-b border-border px-4 py-3">
        <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-wide text-fg-3">
          Lifetime totals
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <CacheKpi
            label="Distinct targets"
            value={String(lifetime?.distinct_targets ?? 0)}
            hint={`${lifetime?.distinct_conversations ?? 0} conversations`}
            tone="fg"
          />
          <CacheKpi
            label="Lifetime hit rate"
            value={`${ltHitRate}%`}
            hint={`${lifetime?.hits ?? 0} hits / ${(lifetime?.hits ?? 0) + (lifetime?.misses ?? 0)} total`}
            tone={(lifetime?.hit_rate ?? 0) >= 0.20 ? "ok" : "fg"}
          />
          <CacheKpi
            label="Total spent"
            value={`$${ltTotal.toFixed(2)}`}
            hint={`across ${lifetime?.runs ?? 0} runs`}
            tone="fg"
          />
          <CacheKpi
            label="Total saved"
            value={`$${ltSaved.toFixed(2)}`}
            hint={`${ltSavedPct}% of gross`}
            tone={ltSaved > 0 ? "ok" : "fg"}
          />
        </div>
      </div>

      {/* WINDOW row — last 24h activity */}
      <div className="px-4 py-3">
        <div className="mb-2 flex items-center justify-between">
          <div className="text-[10.5px] font-semibold uppercase tracking-wide text-fg-3">
            Recent activity ({c?.window_hours ?? 24}h)
          </div>
          <span className="font-mono text-[10.5px] text-fg-3">
            {totals?.runs ?? 0} runs · ${winTotal.toFixed(2)} spent
          </span>
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <CacheKpi
            label="Hit rate"
            value={`${winHitRate}%`}
            hint={`${totals?.hits ?? 0} hits / ${(totals?.hits ?? 0) + (totals?.misses ?? 0)} total`}
            tone={(totals?.hit_rate ?? 0) >= 0.30 ? "ok" : "fg"}
          />
          <CacheKpi
            label="Saved"
            value={`$${winSaved.toFixed(2)}`}
            hint="this window"
            tone={winSaved > 0 ? "ok" : "fg"}
          />
          <CacheKpi
            label="Misses"
            value={String(totals?.misses ?? 0)}
            hint="fresh agent runs"
          />
          <CacheKpi
            label="Hits"
            value={String(totals?.hits ?? 0)}
            hint="cross-run reuse"
            tone={(totals?.hits ?? 0) > 0 ? "ok" : "fg"}
          />
        </div>
      </div>

      {/* Per-kind breakdown */}
      <div className="border-t border-border px-4 py-3">
        <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-wide text-fg-3">
          By source kind
        </div>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
          {kinds.map((k) => {
            const stat = byKind[k as string];
            if (!stat) {
              return (
                <div
                  key={k}
                  className="rounded border border-border/60 bg-bg-0/40 px-3 py-2 text-[11px] text-fg-3"
                >
                  <div className="font-mono text-[10px] uppercase tracking-wider text-fg-3">
                    {k}
                  </div>
                  <div className="mt-1 text-fg-3">no data</div>
                </div>
              );
            }
            const hr = (stat.hit_rate * 100).toFixed(0);
            return (
              <div
                key={k}
                className="rounded border border-border bg-bg-0 px-3 py-2 text-[12px]"
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-fg-2">
                    {k}
                  </span>
                  <span
                    className={cn(
                      "font-mono text-[10.5px] font-semibold",
                      stat.hits > 0 ? "text-ok" : "text-fg-3",
                    )}
                  >
                    {hr}%
                  </span>
                </div>
                <div className="mt-1 flex items-baseline gap-2 font-mono text-[11px] text-fg-1">
                  <span className="font-semibold text-fg-0">{stat.hits}</span>
                  <span className="text-fg-3">/ {stat.hits + stat.misses}</span>
                  <span className="ml-auto text-fg-3">${stat.spent_usd.toFixed(2)}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Top hot targets — where reuse paid off */}
      {top.length > 0 && (
        <div className="border-t border-border px-4 py-3">
          <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-wide text-fg-3">
            Top reused targets (24h)
          </div>
          <ul className="grid grid-cols-1 gap-1 md:grid-cols-2">
            {top.map((t) => (
              <li
                key={`${t.agent_kind}:${t.target_id}`}
                className="flex items-center justify-between rounded bg-bg-0 px-3 py-1.5 text-[11.5px]"
              >
                <span className="flex items-center gap-2 truncate font-mono text-fg-1">
                  <span className="rounded bg-bg-2 px-1.5 py-0.5 text-[10px] text-fg-2">
                    {t.agent_kind}
                  </span>
                  <span className="truncate">{t.target_id}</span>
                </span>
                <span className="font-mono text-[11px] text-ok">
                  {t.hits} hits
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function CacheKpi({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "ok" | "fg";
}) {
  return (
    <div className="rounded border border-border bg-bg-0 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-fg-3">
        {label}
      </div>
      <div
        className={cn(
          "mt-0.5 font-mono text-[18px] font-semibold tabular-nums",
          tone === "ok" ? "text-ok" : "text-fg-0",
        )}
      >
        {value}
      </div>
      {hint && (
        <div className="mt-0.5 truncate text-[10.5px] text-fg-3">{hint}</div>
      )}
    </div>
  );
}

/**
 * Benchmark scoreboard — Tier 3 proving ground.
 *
 * Renders one button per benchmark domain:
 *   - regular domains: clean-slate proving ground (Run A/B/C across
 *     hand-curated questions)
 *   - sequenced domains (``sequenced: true`` in the domain's YAML):
 *     regression fixtures, one multi-turn conversation reviewed
 *     externally; each question has an expected_score baseline so
 *     per-question lift is visible
 *
 * Empty state instructs the operator to invoke the harness CLI.
 */
function BenchmarkScoreboardCard() {
  // Domain switcher is driven off the live DB inventory rather than
  // a hardcoded list — the scoreboard surfaces every benchmark
  // domain in the system without code changes.
  const domains = useQuery({
    queryKey: ["benchmarks-domains"],
    queryFn: () => getBenchmarksDomains(),
    staleTime: 60_000,
  });
  const domainList = domains.data ?? [];
  // "" = auto-pick. We default to the domain with the MOST runs so the
  // scoreboard lands on populated data instead of an empty domain (the old
  // fixed default domain can have 0 runs, so the screen looked broken on
  // first paint).
  const [activeDomain, setActiveDomain] = useState<string>("");
  const effectiveDomain =
    // explicit user pick (if still present in the live list) wins…
    (activeDomain && domainList.find((d) => d.domain === activeDomain)?.domain)
    // …otherwise the most-populated domain…
    || [...domainList].sort((a, b) => b.n_runs - a.n_runs)[0]?.domain
    // …final fallback.
    || activeDomain;
  const summary = useQuery({
    queryKey: ["benchmarks-summary", effectiveDomain],
    queryFn: () => getBenchmarksSummary(effectiveDomain),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const questions = useQuery({
    queryKey: ["benchmarks-questions", effectiveDomain],
    queryFn: () => getBenchmarksQuestions(effectiveDomain),
    staleTime: 5 * 60_000,
  });
  const isRegressionFixture =
    domainList.find((d) => d.domain === effectiveDomain)?.sequenced === true;
  // For the regression fixture, we also need per-run rows so we can
  // join expected_score against actual score per question.
  const runs = useQuery({
    queryKey: ["benchmarks-runs", effectiveDomain],
    queryFn: () => getBenchmarksRuns({ domain: effectiveDomain, limit: 200 }),
    enabled: isRegressionFixture,
    staleTime: 30_000,
  });

  const builds = summary.data?.builds ?? [];
  const nQuestions = summary.data?.n_questions ?? 0;
  const totalRuns = builds.reduce(
    (acc, b) =>
      acc + Object.values(b.by_run_kind).reduce((a, k) => a + k.n, 0),
    0,
  );

  return (
    <section className="mt-5 rounded-lg border border-border bg-bg-1">
      <header className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Target className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          <h3 className="text-[13px] font-semibold tracking-tight text-fg-0">
            Benchmark scoreboard
          </h3>
          <div className="flex items-center gap-0.5 ml-1 flex-wrap">
            {domainList.length === 0 ? (
              <span className="font-mono text-[10px] uppercase tracking-wider text-fg-3">
                no domains loaded
              </span>
            ) : (
              domainList.map((d, i) => {
                const isFirst = i === 0;
                const isLast = i === domainList.length - 1;
                const isActive = effectiveDomain === d.domain;
                // Compact label: full name for regular domains, abbreviated
                // for sequenced regression fixtures.
                const label = d.sequenced ? "regression" : d.domain;
                return (
                  <button
                    key={d.domain}
                    type="button"
                    onClick={() => setActiveDomain(d.domain)}
                    className={cn(
                      "px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider",
                      isFirst && "rounded-l",
                      isLast && "rounded-r",
                      isActive
                        ? "bg-accent text-bg-0"
                        : "bg-bg-2 text-fg-2 hover:bg-bg-3",
                    )}
                    title={`${d.domain} — ${d.n_questions} questions · ${d.n_runs} runs`}
                  >
                    {label}
                  </button>
                );
              })
            )}
          </div>
        </div>
        <span className="font-mono text-[11px] text-fg-3">
          {nQuestions} questions · {builds.length} build
          {builds.length === 1 ? "" : "s"} · {totalRuns} runs
        </span>
      </header>

      {builds.length === 0 ? (
        <div className="px-4 py-6 text-center text-[12px] text-fg-3">
          <div className="mb-1 text-fg-1">No benchmark runs yet.</div>
          <div className="font-mono text-[11px] text-fg-3">
            Run{" "}
            <code className="rounded bg-bg-2 px-1.5 py-0.5">
              uv run python -m bifrost.benchmarks --domain {effectiveDomain} --label after-fixes
            </code>{" "}
            to populate.
          </div>
        </div>
      ) : (
        <div className="divide-y divide-border">
          {builds.map((b) => (
            <BenchmarkBuildRow key={b.build_label ?? "_unlabeled"} build={b} />
          ))}
        </div>
      )}

      {/* Per-question expected vs actual table — only meaningful for
          the regression fixture, where each question carries the
          reviewer's expected_score baseline. */}
      {isRegressionFixture && questions.data && (questions.data ?? []).length > 0 && (
        <RegressionPerQuestionTable
          questions={questions.data}
          runs={runs.data ?? []}
        />
      )}

      {/* Question list (collapsed beneath the score table) */}
      {questions.data && questions.data.length > 0 && (
        <details className="border-t border-border">
          <summary className="cursor-pointer px-4 py-2 text-[11px] font-medium text-fg-2 hover:bg-bg-2">
            {questions.data.length} questions in scope
            {isRegressionFixture && " · with reviewer-graded baseline"}
          </summary>
          <ul className="border-t border-border bg-bg-0">
            {questions.data.map((q) => (
              <li
                key={q.id}
                className="flex items-start gap-3 border-b border-border px-4 py-2 last:border-0"
              >
                <span className="mt-0.5 rounded bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-2">
                  {q.question_key}
                </span>
                <div className="flex-1 text-[12px] text-fg-1">
                  {q.question_text}
                  <div className="mt-0.5 font-mono text-[10.5px] text-fg-3">
                    expects {q.n_entities} entities · {q.n_facts} facts · T
                    {q.tier_floor}+
                    {q.expects_diagram && " · diagram"}
                    {q.paraphrase && " · paraphrase available"}
                    {q.expected_format && ` · ${q.expected_format}`}
                    {q.expected_score !== null && q.expected_score !== undefined &&
                      ` · baseline ${(q.expected_score * 100).toFixed(0)}%`}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/**
 * Per-question expected vs actual score table. Renders alongside the
 * regression-fixture domain so the operator can see, for each turn,
 * whether the system's score after a fix moved above the reviewer's
 * graded baseline.
 *
 * The "delta" column is the headline — green when actual > expected
 * (the fix worked), warn when actual < expected (regression).
 */
function RegressionPerQuestionTable({
  questions,
  runs,
}: {
  questions: BenchmarkQuestionRow[];
  runs: BenchmarkRunRow[];
}) {
  // Pick the most recent run per (question_key, run_kind="A"). Run A
  // is what the reviewer originally rated against (no warm cache),
  // so apples-to-apples vs the baseline.
  const latestByQuestion = new Map<string, BenchmarkRunRow>();
  for (const r of runs) {
    if (r.run_kind !== "A") continue;
    const cur = latestByQuestion.get(r.question_key);
    if (!cur) {
      latestByQuestion.set(r.question_key, r);
      continue;
    }
    const curT = cur.started_at ? new Date(cur.started_at).getTime() : 0;
    const newT = r.started_at ? new Date(r.started_at).getTime() : 0;
    if (newT > curT) latestByQuestion.set(r.question_key, r);
  }

  return (
    <div className="border-t border-border">
      <div className="bg-bg-2 px-4 py-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-fg-3">
        Per-turn lift vs reviewer baseline (Run A)
      </div>
      <table className="w-full text-[11.5px]">
        <thead className="bg-bg-0 text-[10px] uppercase tracking-wide text-fg-3">
          <tr>
            <th className="px-3 py-1.5 text-left">Turn</th>
            <th className="px-2 py-1.5 text-right">Baseline</th>
            <th className="px-2 py-1.5 text-right">Actual</th>
            <th className="px-2 py-1.5 text-right">Δ</th>
            <th className="px-2 py-1.5 text-right">EvCons</th>
            <th className="px-2 py-1.5 text-right">FmtAdh</th>
            <th className="px-2 py-1.5 text-left">Conf</th>
          </tr>
        </thead>
        <tbody>
          {questions.map((q) => {
            const run = latestByQuestion.get(q.question_key);
            const baseline = q.expected_score ?? null;
            const actual = run?.score_total ?? null;
            const delta = (baseline !== null && actual !== null)
              ? actual - baseline
              : null;
            return (
              <tr key={q.id} className="border-t border-border">
                <td className="px-3 py-1.5 font-mono text-fg-1">
                  {q.question_key}
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-fg-2">
                  {baseline !== null ? (baseline * 100).toFixed(0) + "%" : "—"}
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-fg-0">
                  {actual !== null ? (actual * 100).toFixed(0) + "%" : "—"}
                </td>
                <td
                  className={cn(
                    "px-2 py-1.5 text-right font-mono tabular-nums",
                    delta === null && "text-fg-3",
                    delta !== null && delta > 0.02 && "text-ok",
                    delta !== null && delta < -0.02 && "text-warn",
                    delta !== null && Math.abs(delta) <= 0.02 && "text-fg-2",
                  )}
                >
                  {delta === null
                    ? "—"
                    : (delta > 0 ? "+" : "") + (delta * 100).toFixed(0) + "%"}
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-fg-2">
                  {run?.score_evidence_consistency !== undefined
                    ? run.score_evidence_consistency.toFixed(2)
                    : "—"}
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-fg-2">
                  {run?.score_format_adherence !== undefined
                    ? run.score_format_adherence.toFixed(2)
                    : "—"}
                </td>
                <td className="px-2 py-1.5 text-left font-mono text-fg-2">
                  {run?.answer_confidence ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function BenchmarkBuildRow({ build }: { build: BenchmarkBuild }) {
  const kinds: Array<"A" | "B" | "C"> = ["A", "B", "C"];
  return (
    <div className="px-4 py-3">
      <div className="mb-2 flex items-baseline justify-between">
        <div className="text-[12px] font-semibold text-fg-0">
          {build.build_label ?? <span className="italic text-fg-3">(unlabeled)</span>}
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        {kinds.map((kind) => {
          const k = build.by_run_kind[kind];
          if (!k) {
            return (
              <div
                key={kind}
                className="rounded border border-border bg-bg-0 px-3 py-2 opacity-50"
              >
                <div className="text-[10px] font-semibold uppercase tracking-wide text-fg-3">
                  Run {kind}
                </div>
                <div className="mt-1 font-mono text-[14px] text-fg-3">—</div>
              </div>
            );
          }
          const scorePct = (k.avg_score * 100).toFixed(1);
          const cacheRatePct = (k.cache_hit_rate * 100).toFixed(0);
          return (
            <div
              key={kind}
              className="rounded border border-border bg-bg-0 px-3 py-2"
            >
              <div className="flex items-center justify-between">
                <div className="text-[10px] font-semibold uppercase tracking-wide text-fg-3">
                  Run {kind}
                  <span className="ml-1 text-[10px] normal-case text-fg-3">
                    ({kind === "A" ? "cold" : kind === "B" ? "warm" : "paraphrased"})
                  </span>
                </div>
                <div className="font-mono text-[10.5px] text-fg-3">
                  {k.n} run{k.n === 1 ? "" : "s"}
                </div>
              </div>
              <div className="mt-1.5 flex items-baseline gap-2">
                <span
                  className={cn(
                    "font-mono text-[20px] font-semibold tabular-nums",
                    k.avg_score >= 0.7
                      ? "text-ok"
                      : k.avg_score >= 0.5
                      ? "text-fg-0"
                      : "text-warn",
                  )}
                >
                  {scorePct}%
                </span>
                <span className="font-mono text-[10.5px] text-fg-3">avg score</span>
              </div>
              <div className="mt-1 grid grid-cols-3 gap-1 text-[10.5px] text-fg-3">
                <span title="recall">R {k.avg_recall.toFixed(2)}</span>
                <span title="precision">P {k.avg_precision.toFixed(2)}</span>
                <span title="specificity">S {k.avg_specificity.toFixed(2)}</span>
              </div>
              <div className="mt-1 flex items-center justify-between text-[10.5px] text-fg-3">
                <span className="font-mono">${k.avg_cost_usd.toFixed(3)}/q</span>
                <span className="font-mono">{cacheRatePct}% hit</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


// ── Narrative + 4-layer grouping ─────────────


/** Collapsible "How learning works" inline guide. The 5-layer flow
 * diagram + per-layer explainer. Closed by default for return
 * visitors (sessionStorage flag); opens automatically on the first
 * visit so first-timers don't have to hunt the explanation.
 *
 * The diagram is ASCII-art rendered in a monospace box because it
 * matches Bifrost's power-user aesthetic. */
function HowLearningWorks() {
  const STORAGE_KEY = "bifrost_learning_guide_dismissed";
  const dismissed = (() => {
    try {
      return typeof window !== "undefined"
        && sessionStorage.getItem(STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  })();
  const [open, setOpen] = useState<boolean>(!dismissed);

  const onToggle = () => {
    const next = !open;
    setOpen(next);
    try {
      if (typeof window !== "undefined") {
        if (next) sessionStorage.removeItem(STORAGE_KEY);
        else sessionStorage.setItem(STORAGE_KEY, "true");
      }
    } catch {
      /* swallow */
    }
  };

  return (
    <div className="mb-4 overflow-hidden rounded-md border border-border bg-bg-1">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-[12.5px] font-medium text-fg-0 hover:bg-bg-2"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          <BookOpen className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          How learning works
          <span className="text-[11px] font-normal text-fg-3">
            (5 layers — read this if any number on this page surprises you)
          </span>
        </span>
        {open
          ? <ChevronDown className="h-4 w-4 text-fg-3" strokeWidth={1.75} />
          : <ChevronRight className="h-4 w-4 text-fg-3" strokeWidth={1.75} />}
      </button>
      {open && (
        <div className="border-t border-border bg-bg-0 p-4 text-[12.5px] text-fg-1">
          {/* Per-layer mini-cards in a grid */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
            <LayerMiniCard
              n="1"
              icon={<Inbox className="h-3.5 w-3.5" strokeWidth={1.75} />}
              title="Capture"
              body={
                "Chat audit, task transitions, benchmark scoring, " +
                "and UI votes write to confidence_deltas. Every signal " +
                "lands here exactly once."
              }
              color="review"
            />
            <LayerMiniCard
              n="2"
              icon={<Scale className="h-3.5 w-3.5" strokeWidth={1.75} />}
              title="Govern"
              body={
                "Three rules: downgrades apply immediately. " +
                "Upgrades need ≥2 distinct-task or distinct-conv " +
                "confirmations. |Δ| < 0.05 is noise (audit only)."
              }
              color="warn"
            />
            <LayerMiniCard
              n="3"
              icon={<Database className="h-3.5 w-3.5" strokeWidth={1.75} />}
              title="Apply"
              body={
                "Governed deltas update entities.confidence + " +
                "entity_relationships.confidence. Permanent storage; " +
                "no TTL eviction."
              }
              color="info"
            />
            <LayerMiniCard
              n="4"
              icon={<Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />}
              title="Consume"
              body={
                "Per-source agents get a 'Known facts' prompt block " +
                "before they research. Semantic cache returns prior " +
                "agent outputs on cosine ≥ 0.78."
              }
              color="ok"
            />
            <LayerMiniCard
              n="5"
              icon={<Target className="h-3.5 w-3.5" strokeWidth={1.75} />}
              title="Measure"
              body={
                "Three benchmark run kinds (cold / warm / paraphrased) " +
                "× 7 axes prove cost reduction without quality loss. " +
                "Regressions flag broken assumptions."
              }
              color="err"
            />
          </div>

          {/* ASCII flow box */}
          <pre className="mt-4 overflow-auto rounded border border-border bg-bg-2 p-3 font-mono text-[10.5px] leading-relaxed text-fg-1">
{`chat / task / benchmark / UI vote
            │
            ▼
  ┌─────────────────────┐
  │  CAPTURE  →  confidence_deltas (audit log)
  └─────────────────────┘
            │
            ▼
  ┌─────────────────────┐
  │  GOVERN   →  rules in actions/learning.py
  │   • downgrades apply immediately
  │   • upgrades need ≥2 distinct tasks / convs
  │   • |Δ| < 0.05 = noise floor
  └─────────────────────┘
            │
            ▼
  ┌─────────────────────┐
  │  APPLY    →  entity_relationships.confidence
  │              entities.confidence
  │              correction_cascade walks the DAG
  └─────────────────────┘
            │
            ▼
  ┌─────────────────────┐
  │  CONSUME  →  graph_facts injects "Known facts"
  │              into per-source agent prompts
  │              agent_cache returns prior outputs on
  │              semantic match (cosine ≥ 0.78)
  └─────────────────────┘
            │
            ▼
  ┌─────────────────────┐
  │  MEASURE  →  benchmark_runs · 7-axis scoring
  │              run kinds A (cold) / B (warm) / C (paraphrased)
  └─────────────────────┘`}
          </pre>
        </div>
      )}
    </div>
  );
}


function LayerMiniCard({
  n, icon, title, body, color,
}: {
  n: string;
  icon: React.ReactNode;
  title: string;
  body: string;
  color: "review" | "warn" | "info" | "ok" | "err";
}) {
  // 5-layer architecture diagram. Maps to the same 5 semantic tones
  // the rest of the app uses — review/warn/info/ok/err are the
  // canonical visualization spectrum.
  const ringClass: Record<string, string> = {
    review: "ring-review/30 bg-review-soft",
    warn: "ring-warn/30 bg-warn-soft",
    info: "ring-info/30 bg-info-soft",
    ok: "ring-ok/30 bg-ok-soft",
    err: "ring-err/30 bg-err-soft",
  };
  const accentClass: Record<string, string> = {
    review: "text-review",
    warn: "text-warn",
    info: "text-info",
    ok: "text-ok",
    err: "text-err",
  };
  return (
    <div className={cn("rounded-md p-2.5 ring-1", ringClass[color])}>
      <div className={cn("flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-wider", accentClass[color])}>
        <span>L{n}</span>
        {icon}
        <span className="font-sans font-semibold">{title}</span>
      </div>
      <div className="mt-1.5 text-[11.5px] leading-snug text-fg-1">{body}</div>
    </div>
  );
}


/** Section banner that explains what the cards/tables BELOW show.
 * Operators have to scroll past 4–5 sections; without these, every
 * section looks the same and they don't know which one to inspect
 * for what symptom. */
function LayerBanner({
  layer, title, explainer,
}: {
  layer: "1" | "2" | "3" | "4" | "5";
  title: string;
  explainer: string;
}) {
  return (
    <div className="mt-6 mb-2 flex items-start gap-2 border-l-2 border-accent/40 pl-3">
      <span className="mt-0.5 inline-flex h-5 min-w-[1.5rem] items-center justify-center rounded bg-accent/10 px-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-accent ring-1 ring-accent/30">
        L{layer}
      </span>
      <div>
        <div className="text-[13px] font-semibold text-fg-0">{title}</div>
        <div className="mt-0.5 max-w-3xl text-[11.5px] leading-relaxed text-fg-2">
          {explainer}
        </div>
      </div>
    </div>
  );
}
