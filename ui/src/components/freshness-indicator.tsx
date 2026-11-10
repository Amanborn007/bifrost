import { cn } from "@/lib/utils";

const CONFIG: Record<string, { dot: string; label: string }> = {
  fresh: { dot: "bg-ok", label: "Fresh" },
  slightly_stale: { dot: "bg-warn", label: "Slightly stale" },
  stale: { dot: "bg-warn", label: "Stale" },
  very_stale: { dot: "bg-err", label: "Very stale" },
  unknown: { dot: "bg-fg-3", label: "Unknown" },
};

export function FreshnessIndicator({ state }: { state: string }) {
  const c = CONFIG[state] ?? CONFIG.unknown;
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-fg-2">
      <span className={cn("h-1.5 w-1.5 rounded-full", c.dot)} aria-hidden />
      {c.label}
    </span>
  );
}
