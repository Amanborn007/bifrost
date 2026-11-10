/**
 * Compact integration health chip.
 * Shows ● ok / ⚠ degraded / ○ not configured for a given source.
 * Used in collaboration landing page panels to explain why data may be empty.
 */
import { useQuery } from "@tanstack/react-query";
import { cn, formatRelativeDate } from "@/lib/utils";
import { getIntegrationsHealth } from "@/lib/api";

type Source = "jira" | "github" | "confluence" | "extraction";

export function IntegrationStatus({
  source,
  className,
}: {
  source: Source;
  className?: string;
}) {
  const { data } = useQuery({
    queryKey: ["integrations-health"],
    queryFn: getIntegrationsHealth,
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
  });

  if (!data) return null;

  const s = source === "extraction" ? data.extraction : data[source as Exclude<Source, "extraction">];

  if (source === "extraction") {
    const e = data.extraction;
    const note = e.last_indexed_at
      ? "indexed " + formatRelativeDate(e.last_indexed_at)
      : "never indexed";
    return (
      <span className={cn("inline-flex items-center gap-1 text-[10.5px] text-fg-3", className)}>
        <span className="h-1.5 w-1.5 rounded-full bg-ok" />
        {e.repos_fresh} fresh · {note}
      </span>
    );
  }

  const src = data[source as Exclude<Source, "extraction">];
  const dotColor = {
    ok: "bg-ok",
    degraded: "bg-warn",
    not_configured: "bg-fg-3",
    error: "bg-err",
  }[src.status];

  const label = src.status === "not_configured"
    ? source + " not configured"
    : src.status === "ok" && src.last_synced_at
      ? source + " synced " + formatRelativeDate(src.last_synced_at)
      : src.status === "degraded"
        ? source + " sync degraded"
        : source + " sync error";

  return (
    <span
      className={cn("inline-flex items-center gap-1 text-[10.5px] text-fg-3", className)}
      title={src.error_count > 0 ? `${src.error_count} sync errors` : undefined}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", dotColor)} />
      {label}
    </span>
  );
}
