import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Circle,
  Play,
  RefreshCw,
  Settings2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  getRepoCheckpoint,
  type RepoCheckpointStatus,
} from "@/lib/api";

/**
 * Canonical phase ordering surfaced in the UI. B and C are reserved
 * placeholders in the backend PHASE_ORDER; we hide them here because the
 * user only cares about phases that actually do work.
 */
const PHASE_ORDER = ["A", "D", "E", "F", "G", "H", "I"] as const;
const PHASE_LABELS: Record<string, string> = {
  A: "Triage",
  D: "Components",
  E: "Flows",
  F: "Dependencies",
  G: "Relations",
  H: "Knowledge index",
  I: "Confidence",
};

export interface RepoActionHandlers {
  onResume: () => void;
  onReextract: () => void;
  onInvalidate: (phases: string[]) => void;
}

/**
 * Resume (primary) + Re-extract (secondary) + Phases dropdown.
 *
 * - **Resume**  — trigger a new task with no force/invalidate. The worker
 *   picks up the repo-level checkpoint and skips done phases.
 * - **Re-extract** — force_full=true, wipes the checkpoint, runs A→I.
 * - **Phases ▾** — checkbox list of A/D/E/F/G/H/I showing which are done.
 *   Ticking phases → invalidate_phases=[…] (downstream dependents cleared
 *   automatically by the worker).
 */
export function RepoActionGroup({
  repoId,
  handlers,
  size = "sm",
  dense = false,
}: {
  repoId: string;
  handlers: RepoActionHandlers;
  size?: "sm" | "xs";
  /** When true, show Resume + a single ▾ menu (collapses Re-extract into menu). */
  dense?: boolean;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const cp = useQuery({
    queryKey: ["repo-checkpoint", repoId],
    queryFn: () => getRepoCheckpoint(repoId),
    enabled: menuOpen,
    staleTime: 5_000,
  });

  const completedCount = _doneCount(cp.data);
  const totalPhases = PHASE_ORDER.length;

  const btn =
    size === "xs"
      ? "px-1.5 py-0.5 text-[11px]"
      : "px-2 py-1 text-[11.5px]";

  return (
    <div className="relative inline-flex items-center gap-1">
      <button
        onClick={handlers.onResume}
        className={cn(
          "inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 font-medium text-fg-1 hover:border-accent hover:text-accent",
          btn,
        )}
        title="Resume from last checkpoint (skips already-done phases)"
      >
        <Play className="h-3 w-3" strokeWidth={1.75} />
        Resume
      </button>
      {!dense && (
        <button
          onClick={handlers.onReextract}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 font-medium text-fg-2 hover:border-err/60 hover:text-err",
            btn,
          )}
          title="Wipe checkpoint and re-run all phases"
        >
          <RefreshCw className="h-3 w-3" strokeWidth={1.75} />
          Re-extract
        </button>
      )}
      <button
        onClick={() => {
          setMenuOpen(!menuOpen);
          setPicked(new Set());
        }}
        className={cn(
          "inline-flex items-center gap-1 rounded-md border font-medium",
          btn,
          menuOpen
            ? "border-accent bg-accent-soft text-accent"
            : "border-border bg-bg-1 text-fg-2 hover:border-accent hover:text-accent",
        )}
        title="Pick specific phases to re-run"
      >
        <Settings2 className="h-3 w-3" strokeWidth={1.75} />
        Phases
      </button>
      {menuOpen && (
        <div
          className="absolute right-0 top-full z-30 mt-1 w-[300px] rounded-md border border-border bg-bg-0 p-2 shadow-lg"
          onMouseLeave={() => setMenuOpen(false)}
        >
          <div className="flex items-center justify-between border-b border-border pb-1.5">
            <span className="text-[11.5px] font-semibold text-fg-0">
              Checkpoint — pick phases to re-run
            </span>
            <span className="font-mono text-[10.5px] text-fg-3">
              {cp.isLoading ? "…" : `${completedCount}/${totalPhases} done`}
            </span>
          </div>
          <div className="mt-1.5 space-y-0.5">
            {PHASE_ORDER.map((ph) => {
              const done = cp.data?.completed_phases.includes(ph) ?? false;
              const unit = cp.data?.phases.find((p) => p.phase === ph);
              const checked = picked.has(ph);
              return (
                <label
                  key={ph}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-[11.5px]",
                    checked ? "bg-accent-soft" : "hover:bg-bg-1",
                  )}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(e) => {
                      const next = new Set(picked);
                      if (e.target.checked) next.add(ph);
                      else next.delete(ph);
                      setPicked(next);
                    }}
                    className="h-3 w-3 accent-accent"
                  />
                  {done ? (
                    <CheckCircle2
                      className="h-3 w-3 text-ok"
                      strokeWidth={1.75}
                    />
                  ) : (
                    <Circle
                      className="h-3 w-3 text-fg-3"
                      strokeWidth={1.75}
                    />
                  )}
                  <span className="font-mono text-fg-2">{ph}</span>
                  <span className="text-fg-1">{PHASE_LABELS[ph]}</span>
                  {unit && unit.units_done > 0 && (
                    <span
                      className="ml-auto font-mono text-[10px] text-fg-3"
                      title="Files processed in this phase"
                    >
                      {unit.units_done}
                    </span>
                  )}
                </label>
              );
            })}
          </div>
          {dense && (
            <div className="mt-1 border-t border-border pt-1.5">
              <button
                onClick={() => {
                  setMenuOpen(false);
                  handlers.onReextract();
                }}
                className="flex w-full items-center gap-1 rounded px-1.5 py-1 text-[11px] text-fg-2 hover:bg-err-soft hover:text-err"
              >
                <RefreshCw className="h-3 w-3" strokeWidth={1.75} />
                Wipe checkpoint and re-extract everything
              </button>
            </div>
          )}
          <div className="mt-2 flex items-center justify-between gap-2 border-t border-border pt-1.5">
            <span className="text-[10.5px] text-fg-3">
              Downstream phases invalidate automatically.
            </span>
            <button
              disabled={picked.size === 0}
              onClick={() => {
                handlers.onInvalidate(Array.from(picked).sort());
                setMenuOpen(false);
              }}
              className="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-[11px] font-medium text-accent-fg disabled:opacity-40"
            >
              Re-run {picked.size > 0 ? `(${picked.size})` : ""}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function _doneCount(cp: RepoCheckpointStatus | undefined): number {
  if (!cp) return 0;
  // Ignore reserved B/C placeholders
  return cp.completed_phases.filter((p) => p !== "B" && p !== "C").length;
}
