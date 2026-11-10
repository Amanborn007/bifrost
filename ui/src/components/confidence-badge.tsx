import { cn } from "@/lib/utils";

interface ConfidenceConfig {
  dot: string;
  text: string;
  /** Plain-language label shown as the primary badge text. */
  label: string;
  /** Technical tier name shown in the tooltip for engineers who want it. */
  technicalTier: string;
}

const CONFIG: Record<string, ConfidenceConfig> = {
  confirmed: {
    dot: "bg-ok",
    text: "text-ok",
    label: "Based on source code",
    technicalTier: "confirmed",
  },
  inferred: {
    dot: "bg-info",
    text: "text-info",
    label: "Based on documentation",
    technicalTier: "inferred",
  },
  uncertain: {
    dot: "bg-warn",
    text: "text-warn",
    label: "Not yet fully indexed — answer may be incomplete",
    technicalTier: "uncertain",
  },
  low: {
    dot: "bg-fg-3",
    text: "text-fg-2",
    label: "Inferred from metadata only",
    technicalTier: "low",
  },
  stale: { dot: "bg-fg-3", text: "text-fg-2", label: "Stale", technicalTier: "stale" },
  unknown: { dot: "bg-fg-3", text: "text-fg-2", label: "Unknown", technicalTier: "unknown" },
  conflicting: { dot: "bg-err", text: "text-err", label: "Conflicting", technicalTier: "conflicting" },
  high: { dot: "bg-ok", text: "text-ok", label: "High", technicalTier: "high" },
  medium: { dot: "bg-warn", text: "text-warn", label: "Medium", technicalTier: "medium" },
};

export function ConfidenceBadge({
  level,
  className,
}: {
  level: string;
  className?: string;
}) {
  const c = CONFIG[level] ?? CONFIG.unknown;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border bg-bg-1 px-2 py-0.5 text-[11px] font-medium",
        c.text,
        className,
      )}
      title={`Confidence tier: ${c.technicalTier}`}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", c.dot)} aria-hidden />
      {c.label}
    </span>
  );
}
