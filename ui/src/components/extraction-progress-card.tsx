import { memo, useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { Pause, Play, Square, Loader2 } from "lucide-react";
import { subscribeToTaskStream, type SSEEvent } from "@/lib/sse";
import {
  pauseExtractionTask as pauseTask,
  resumeExtractionTask as resumeTask,
  cancelExtractionTask as cancelTask,
  type TaskSummary,
} from "@/lib/api";
import { toast } from "sonner";

/**
 * Live extraction progress card. Clean product-first layout — no graph paper,
 * no ASCII, no WONK serif. Phase timeline + progress bar + metric grid.
 */

const PHASES = [
  { key: "A", label: "Triage" },
  { key: "D", label: "Components" },
  { key: "E", label: "Flows" },
  { key: "F", label: "Dependencies" },
  { key: "G", label: "Relations" },
  { key: "H", label: "Index" },
  { key: "I", label: "Confidence" },
] as const;

interface CostSnapshot {
  total_cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  calls: number;
  per_phase_cost: Record<string, number>;
  per_phase_calls: Record<string, number>;
  last_phase: string;
  last_model: string;
  last_cost: number;
}

function parseFileProgress(phase: string): { done: number; total: number } | null {
  const m = phase.match(/(\d+)\s*\/\s*(\d+)/);
  return m ? { done: parseInt(m[1], 10), total: parseInt(m[2], 10) } : null;
}
function parseErrorCount(phase: string): number {
  const m = phase.match(/\((\d+)\s*errors?\)/);
  return m ? parseInt(m[1], 10) : 0;
}
function parseCurrentPhase(phase: string): string | null {
  const m = phase.match(/Phase\s+([A-I])/i);
  return m ? m[1].toUpperCase() : null;
}
function fmtElapsed(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m < 1) return `${sec}s`;
  if (m < 60) return `${m}m ${sec}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
function fmtCount(n: number): string {
  if (n < 1000) return n.toString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(n / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
}

/**
 * Memoized so the dashboard's 30s react-query poll doesn't repaint
 * every running-extraction card. The card owns its own SSE state +
 * a 1s tick — that's where re-renders should originate, not from a
 * parent prop change that didn't actually alter ``task``.
 */
export const ExtractionProgressCard = memo(ExtractionProgressCardImpl);

function ExtractionProgressCardImpl({ task }: { task: TaskSummary }) {
  const [progress, setProgress] = useState(0);
  const [phase, setPhase] = useState("Connecting…");
  const [taskStatus, setTaskStatus] = useState(task.status);
  const [cost, setCost] = useState<CostSnapshot | null>(null);
  const [completedPhases, setCompletedPhases] = useState<Set<string>>(new Set());
  const [errorCount, setErrorCount] = useState(0);
  // Both initialised in the mount effect (not during render, which must
  // stay pure). The 1s ``now`` tick drives the elapsed-time readout.
  const [startTime, setStartTime] = useState(0);
  const [now, setNow] = useState(0);

  useEffect(() => {
    // Seed from the wall clock (an external system) on mount, then tick.
    const t0 = Date.now();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStartTime(t0);
    setNow(t0);
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const unsub = subscribeToTaskStream(
      task.id,
      (e: SSEEvent) => {
        if (e.event_type === "extraction_progress") {
          const pct = e.data.progress as number | undefined;
          const ph = e.data.phase as string | undefined;
          if (pct != null) setProgress(pct);
          if (ph) {
            setPhase(ph);
            const errs = parseErrorCount(ph);
            if (errs) setErrorCount(errs);
            if (ph === "Paused") setTaskStatus("paused");
            else if (pct != null && pct > 0) setTaskStatus("running");
          }
        }
        if (e.event_type === "phase_complete") {
          const ph = e.data.phase as string | undefined;
          if (ph) setCompletedPhases((prev) => new Set([...prev, ph]));
        }
        if (e.event_type === "cost_update") {
          setCost(e.data as unknown as CostSnapshot);
        }
        if (e.event_type === "task_complete") {
          setProgress(100);
          setPhase("Complete");
          setTaskStatus("delivered");
          setCompletedPhases(new Set(PHASES.map((p) => p.key)));
        }
        if (e.event_type === "task_failed") {
          setPhase((e.data.error as string) ?? "Failed");
          setTaskStatus("failed");
        }
      },
      () =>
        // Stable id so sonner dedupes — the SSE auto-reconnect loop
        // would otherwise stack hundreds of toasts during a flap.
        toast.error(`Lost connection to ${task.id.slice(0, 8)}`, {
          id: `sse-error-${task.id}`,
        }),
    );
    return unsub;
  }, [task.id]);

  const currentPhase = useMemo(() => parseCurrentPhase(phase), [phase]);
  const fileProgress = useMemo(() => parseFileProgress(phase), [phase]);
  const elapsed = startTime > 0 ? Math.floor((now - startTime) / 1000) : 0;

  const isRunning = taskStatus === "running";
  const isPaused = taskStatus === "paused";
  const isActive =
    isRunning || isPaused || taskStatus === "received" || taskStatus === "pending";
  const isDone = taskStatus === "delivered";
  const isFailed = taskStatus === "failed" || taskStatus === "cancelled";

  const handle = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      toast.success(ok);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <article className="overflow-hidden rounded-lg border border-border bg-bg-1">
      {/* Header */}
      <header className="flex items-start justify-between gap-4 border-b border-border px-5 py-3.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <StatusDot status={taskStatus} />
            <h3 className="truncate text-[14px] font-semibold text-fg-0">
              {task.query}
            </h3>
          </div>
          <p className="mt-1 flex items-center gap-2 text-[12px] text-fg-2">
            {isRunning && (
              <Loader2 className="h-3 w-3 animate-spin text-accent" strokeWidth={1.75} />
            )}
            <span>{phase}</span>
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {isRunning && (
            <IconBtn
              onClick={() => handle(() => pauseTask(task.id), "Paused")}
              title="Pause"
            >
              <Pause className="h-3.5 w-3.5" strokeWidth={1.75} />
            </IconBtn>
          )}
          {isPaused && (
            <IconBtn
              onClick={() => handle(() => resumeTask(task.id), "Resumed")}
              title="Resume"
            >
              <Play className="h-3.5 w-3.5" strokeWidth={1.75} />
            </IconBtn>
          )}
          {isActive && (
            <IconBtn
              onClick={() => {
                if (!confirm("Cancel this extraction? Progress will be lost.")) return;
                handle(() => cancelTask(task.id), "Cancelled");
              }}
              title="Cancel"
              destructive
            >
              <Square className="h-3.5 w-3.5" strokeWidth={1.75} />
            </IconBtn>
          )}
        </div>
      </header>

      {/* Progress bar */}
      <div className="px-5 py-4">
        <div className="mb-1.5 flex items-baseline justify-between text-[11px]">
          <span className="text-fg-2">Overall progress</span>
          <span className="tabular font-medium text-fg-0">{progress}%</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-bg-2">
          <div
            className={cn(
              "h-full rounded-full transition-[width] duration-500",
              isFailed && "bg-err",
              isDone && "bg-ok",
              !isFailed && !isDone && "bg-accent",
            )}
            style={{ width: `${Math.max(0, Math.min(100, progress))}%` }}
          />
        </div>
      </div>

      {/* Phase timeline */}
      <div className="border-t border-border px-5 py-4">
        <div className="section-title mb-3">Pipeline phases</div>
        <ol className="grid grid-cols-7 gap-0">
          {PHASES.map((p, idx) => {
            const done = completedPhases.has(p.key);
            const active = p.key === currentPhase && !done && !isFailed;
            const pending = !done && !active;
            return (
              <li key={p.key} className="relative flex flex-col items-center text-center">
                {idx > 0 && (
                  <span
                    aria-hidden
                    className={cn(
                      "absolute left-0 top-[11px] h-0.5 w-1/2 -translate-y-1/2",
                      done || active ? "bg-accent" : "bg-border",
                    )}
                  />
                )}
                {idx < PHASES.length - 1 && (
                  <span
                    aria-hidden
                    className={cn(
                      "absolute right-0 top-[11px] h-0.5 w-1/2 -translate-y-1/2",
                      done ? "bg-accent" : "bg-border",
                    )}
                  />
                )}
                <span
                  className={cn(
                    "relative z-10 flex h-[22px] w-[22px] items-center justify-center rounded-full border text-[10px] font-semibold",
                    done && "border-accent bg-accent text-accent-fg",
                    active && "border-accent bg-bg-1 text-accent ring-2 ring-accent-ring",
                    pending && "border-border bg-bg-1 text-fg-3",
                  )}
                >
                  {p.key}
                </span>
                <span
                  className={cn(
                    "mt-1.5 text-[10.5px]",
                    done && "text-fg-1",
                    active && "font-medium text-accent",
                    pending && "text-fg-3",
                  )}
                >
                  {p.label}
                </span>
              </li>
            );
          })}
        </ol>
      </div>

      {/* Metrics */}
      <dl className="grid grid-cols-2 gap-0 border-t border-border sm:grid-cols-4">
        <Metric
          label="Files"
          value={
            fileProgress
              ? `${fileProgress.done.toLocaleString()} / ${fileProgress.total.toLocaleString()}`
              : "—"
          }
          sub={fileProgress ? `${Math.round((fileProgress.done / fileProgress.total) * 100)}%` : "—"}
        />
        <Metric
          label="Cost"
          value={cost ? `$${cost.total_cost_usd.toFixed(cost.total_cost_usd < 10 ? 3 : 2)}` : "$0.00"}
          sub={cost ? `${cost.calls.toLocaleString()} calls` : "—"}
        />
        <Metric
          label="Tokens"
          value={cost ? fmtCount(cost.input_tokens + cost.output_tokens) : "—"}
          sub={
            cost
              ? `${fmtCount(cost.input_tokens)} in · ${fmtCount(cost.output_tokens)} out`
              : "—"
          }
        />
        <Metric
          label="Errors"
          value={errorCount.toLocaleString()}
          sub={`elapsed ${fmtElapsed(elapsed)}`}
          tone={errorCount > 0 ? "err" : "default"}
        />
      </dl>

      {/* Cache footer */}
      {cost && cost.input_tokens + cost.cache_read_tokens > 0 && (
        <footer className="flex items-center justify-between border-t border-border bg-bg-0 px-5 py-2 text-[11px] text-fg-2">
          <span>
            Cache hit{" "}
            <span className="tabular font-medium text-fg-0">
              {Math.round(
                (cost.cache_read_tokens /
                  (cost.input_tokens + cost.cache_read_tokens)) *
                  100,
              )}
              %
            </span>
          </span>
          <span>
            {fmtCount(cost.cache_read_tokens)} cached · {fmtCount(cost.input_tokens)} fresh
          </span>
        </footer>
      )}
    </article>
  );
}

/* ── Helpers ─────────────────────────────────────────── */

function StatusDot({ status }: { status: string }) {
  // Use the global ``.dot`` + ``.live-pulse`` primitives so this dot
  // beats at the same 1.8s heartbeat as the inbox header — coprime
  // with our 30s poll, never flickers against data refreshes.
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
  return (
    <span
      aria-hidden
      className={cn("dot", cls, status === "running" && "live-pulse")}
    />
  );
}

function IconBtn({
  onClick,
  children,
  title,
  destructive,
}: {
  onClick: () => void;
  children: React.ReactNode;
  title: string;
  destructive?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md border border-border bg-bg-1 text-fg-1 transition-colors hover:bg-bg-2 hover:text-fg-0",
        destructive && "hover:border-err hover:text-err",
      )}
    >
      {children}
    </button>
  );
}

function Metric({
  label,
  value,
  sub,
  tone = "default",
}: {
  label: string;
  value: string;
  sub: string;
  tone?: "default" | "err";
}) {
  return (
    <div className="border-r border-border px-5 py-3 last:border-r-0">
      <dt className="text-[11px] font-medium text-fg-2">{label}</dt>
      <dd
        className={cn(
          "mt-0.5 tabular text-[18px] font-semibold",
          tone === "err" && "text-err",
          tone === "default" && "text-fg-0",
        )}
      >
        {value}
      </dd>
      <dd className="tabular text-[11px] text-fg-3">{sub}</dd>
    </div>
  );
}
