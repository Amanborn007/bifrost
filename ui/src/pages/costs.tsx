import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { getCostSummary } from "@/lib/api";
import type { CostByRow, CostSummary } from "@/lib/api";
import { cn } from "@/lib/utils";
import { DollarSign, Zap, Layers, GitBranch, Database } from "lucide-react";

type Window = "7d" | "30d" | "all";

/**
 * Spend page — real $ from llm_cost_log. No more "entity count as proxy."
 */
export function CostsPage() {
  const [win, setWin] = useState<Window>("30d");
  const costs = useQuery<CostSummary>({
    queryKey: ["cost-summary", win],
    queryFn: () => getCostSummary(win),
    refetchInterval: 60_000,
  });
  const s = costs.data;

  return (
    <div className="mx-auto max-w-[1200px] space-y-8 px-8 py-8">
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-fg-0">
            Spend
          </h1>
          <p className="mt-1 text-[13.5px] text-fg-2">
            Real LLM cost from the <code className="rounded bg-bg-2 px-1 py-0.5 font-mono text-[11.5px]">llm_cost_log</code> table.
          </p>
        </div>
        <WindowToggle value={win} onChange={setWin} />
      </header>

      {/* Top stats */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <BigStat
          icon={DollarSign}
          label="Total spend"
          value={s ? `$${s.total_cost_usd.toFixed(s.total_cost_usd < 10 ? 3 : 2)}` : "—"}
          sub={windowLabel(win)}
          tone="accent"
          loading={costs.isLoading}
        />
        <BigStat
          icon={Zap}
          label="LLM calls"
          value={s ? s.total_calls.toLocaleString() : "—"}
          sub={windowLabel(win)}
          loading={costs.isLoading}
        />
        <BigStat
          icon={Layers}
          label="Input tokens"
          value={s ? fmtTokens(s.total_input_tokens) : "—"}
          sub={windowLabel(win)}
          loading={costs.isLoading}
        />
        <BigStat
          icon={GitBranch}
          label="Output tokens"
          value={s ? fmtTokens(s.total_output_tokens) : "—"}
          sub={windowLabel(win)}
          loading={costs.isLoading}
        />
      </section>

      {/* Cache savings — avoided $ from LLM response-cache hits */}
      {s && (s.cache_savings?.hits ?? 0) > 0 && (
        <section className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-ok/40 bg-ok-soft/30 px-5 py-3">
          <div className="inline-flex items-center gap-2 text-ok">
            <Database className="h-4 w-4" strokeWidth={1.75} />
            <span className="text-[13px] font-semibold">LLM cache savings</span>
          </div>
          <div>
            <span className="tabular text-[18px] font-semibold text-fg-0">
              ${s.cache_savings.saved_usd.toFixed(2)}
            </span>
            <span className="ml-1.5 text-[12px] text-fg-3">avoided</span>
          </div>
          <div className="text-[12px] text-fg-2">
            {s.cache_savings.hits.toLocaleString()} cache hit
            {s.cache_savings.hits === 1 ? "" : "s"} · {windowLabel(win)}
          </div>
          {s.total_cost_usd + s.cache_savings.saved_usd > 0 && (
            <div className="ml-auto text-[12px] text-fg-3">
              {(
                (100 * s.cache_savings.saved_usd) /
                (s.total_cost_usd + s.cache_savings.saved_usd)
              ).toFixed(0)}
              % of would-be spend served from cache
            </div>
          )}
        </section>
      )}

      {/* 7-day sparkline */}
      <section className="rounded-lg border border-border bg-bg-1 p-5">
        <header className="mb-3 flex items-end justify-between">
          <div>
            <h2 className="text-[14.5px] font-semibold text-fg-0">
              Daily spend · last 7 days
            </h2>
            <p className="mt-0.5 text-[12px] text-fg-3">
              Not affected by window selector — always last 7 days.
            </p>
          </div>
          {s && (
            <div className="text-right">
              <div className="tabular text-[18px] font-semibold text-fg-0">
                ${s.last_7_days.reduce((a, p) => a + p.cost_usd, 0).toFixed(2)}
              </div>
              <div className="text-[11px] text-fg-3">7-day total</div>
            </div>
          )}
        </header>
        {costs.isLoading ? (
          <Skeleton className="h-32 rounded-md" />
        ) : (
          <DailyBars data={s?.last_7_days ?? []} />
        )}
      </section>

      {/* Three breakdowns side by side */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Breakdown
          title="By model"
          rows={s?.by_model ?? []}
          loading={costs.isLoading}
          total={s?.total_cost_usd ?? 0}
        />
        <Breakdown
          title="By phase"
          rows={s?.by_phase ?? []}
          loading={costs.isLoading}
          total={s?.total_cost_usd ?? 0}
        />
        <Breakdown
          title="By repository"
          rows={s?.by_repo ?? []}
          loading={costs.isLoading}
          total={s?.total_cost_usd ?? 0}
        />
      </section>

      {s && s.total_calls === 0 && (
        <aside className="rounded-lg border border-dashed border-border bg-bg-1 p-6 text-center">
          <p className="text-[14px] font-medium text-fg-1">
            No LLM cost recorded in this window
          </p>
          <p className="mt-1 text-[12.5px] text-fg-3">
            Run an extraction or ask a question to populate the ledger.
          </p>
        </aside>
      )}
    </div>
  );
}

/* ── Window toggle ─────────────────────────────────── */

function WindowToggle({
  value,
  onChange,
}: {
  value: Window;
  onChange: (v: Window) => void;
}) {
  const options: { v: Window; label: string }[] = [
    { v: "7d", label: "7 days" },
    { v: "30d", label: "30 days" },
    { v: "all", label: "All time" },
  ];
  return (
    <div className="inline-flex rounded-md border border-border bg-bg-1 p-0.5">
      {options.map((o) => (
        <button
          key={o.v}
          onClick={() => onChange(o.v)}
          className={cn(
            "rounded-[5px] px-3 py-1 text-[12.5px] font-medium transition-colors",
            value === o.v
              ? "bg-bg-3 text-fg-0"
              : "text-fg-2 hover:text-fg-0",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ── Big stat card ─────────────────────────────────── */

function BigStat({
  icon: Icon,
  label,
  value,
  sub,
  tone = "default",
  loading,
}: {
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  label: string;
  value: string;
  sub: string;
  tone?: "default" | "accent";
  loading?: boolean;
}) {
  const color = tone === "accent" ? "text-accent" : "text-fg-0";
  return (
    <div className="rounded-lg border border-border bg-bg-1 px-5 py-4">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-medium text-fg-2">{label}</div>
        <Icon className="h-3.5 w-3.5 text-fg-3" strokeWidth={1.75} />
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-20" />
      ) : (
        <div className={cn("mt-1 tabular text-[24px] font-semibold leading-none", color)}>
          {value}
        </div>
      )}
      <div className="mt-1.5 text-[11.5px] text-fg-3">{sub}</div>
    </div>
  );
}

/* ── Daily bars ────────────────────────────────────── */

function DailyBars({ data }: { data: { date: string; cost_usd: number }[] }) {
  const max = Math.max(0.001, ...data.map((d) => d.cost_usd));
  return (
    <div className="flex h-28 items-end gap-1.5">
      {data.map((p) => {
        const h = Math.max(2, Math.round((p.cost_usd / max) * 110));
        const hasCost = p.cost_usd > 0;
        return (
          <div key={p.date} className="flex flex-1 flex-col items-center gap-1.5">
            <div className="flex w-full flex-1 items-end">
              <div
                className={cn(
                  "w-full rounded-sm transition-all",
                  hasCost ? "bg-accent" : "bg-bg-3",
                )}
                style={{ height: `${h}px` }}
                title={`${p.date}: $${p.cost_usd.toFixed(4)}`}
              />
            </div>
            <div className="flex flex-col items-center leading-tight">
              <div className="tabular text-[10px] font-medium text-fg-1">
                {p.cost_usd > 0 ? `$${p.cost_usd.toFixed(p.cost_usd < 1 ? 3 : 2)}` : "—"}
              </div>
              <div className="text-[9px] text-fg-3">{shortDay(p.date)}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Breakdown list ────────────────────────────────── */

function Breakdown({
  title,
  rows,
  loading,
  total,
}: {
  title: string;
  rows: CostByRow[];
  loading: boolean;
  total: number;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-1">
      <header className="border-b border-border px-4 py-2.5">
        <h3 className="text-[13.5px] font-semibold text-fg-0">{title}</h3>
      </header>
      {loading ? (
        <div className="space-y-2 p-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-10 rounded-md" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <p className="px-4 py-5 text-center text-[12px] text-fg-3">No data</p>
      ) : (
        <ol className="divide-y divide-border">
          {rows.slice(0, 6).map((r) => {
            const pct = total > 0 ? (r.cost_usd / total) * 100 : 0;
            return (
              <li key={r.key} className="px-4 py-2.5">
                <div className="mb-1.5 flex items-baseline justify-between gap-3">
                  <span className="truncate text-[13px] font-medium text-fg-0">
                    {r.label}
                  </span>
                  <span className="shrink-0 tabular text-[13px] font-semibold text-fg-0">
                    ${r.cost_usd.toFixed(r.cost_usd < 1 ? 4 : 2)}
                  </span>
                </div>
                <div className="mb-1 h-1 rounded-full bg-bg-2">
                  <div
                    className="h-full rounded-full bg-accent"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="flex items-center justify-between text-[10.5px] text-fg-3">
                  <span>
                    {r.calls.toLocaleString()}{" "}
                    {r.calls === 1 ? "call" : "calls"}
                  </span>
                  <span>{pct.toFixed(1)}%</span>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

/* ── Helpers ───────────────────────────────────────── */

function fmtTokens(n: number): string {
  if (n < 1000) return n.toString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(n / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
}

function windowLabel(w: Window): string {
  return { "7d": "last 7 days", "30d": "last 30 days", all: "all time" }[w];
}

function shortDay(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { weekday: "short" });
}
