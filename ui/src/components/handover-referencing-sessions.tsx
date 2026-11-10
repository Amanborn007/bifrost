/**
 * Referencing collaboration sessions panel.
 *
 * On a handover detail page, surfaces the collab sessions that
 * referenced this doc during clarification — so the owner can see at
 * a glance "who's currently asking about this thing" and jump into
 * the live conversation if they want to help unblock it directly
 * (instead of waiting for the clarifier to ask back).
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  ArrowUpRight,
  ChevronRight,
  Loader2,
  MessageSquare,
} from "lucide-react";

import {
  getHandoverReferencingSessions,
  type ReferencingSessionEntry,
} from "@/lib/api";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";

const STATE_PILL: Record<
  ReferencingSessionEntry["state"],
  { cls: string; label: string }
> = {
  open: { cls: "bg-info-soft text-info", label: "Open" },
  clarifying: { cls: "bg-warn-soft text-warn", label: "Clarifying" },
  resolved: { cls: "bg-ok-soft text-ok", label: "Resolved" },
  briefed: { cls: "bg-accent-soft text-accent", label: "Briefed" },
  closed: { cls: "bg-bg-3 text-fg-2", label: "Closed" },
};

const REQUIREMENT_PREVIEW_LEN = 110;

interface HandoverReferencingSessionsProps {
  handoverId: string;
  className?: string;
  /** Cap the rendered list (default 8). */
  limit?: number;
}

export function HandoverReferencingSessions({
  handoverId,
  className,
  limit = 8,
}: HandoverReferencingSessionsProps) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["handover-referencing-sessions", handoverId],
    queryFn: () => getHandoverReferencingSessions(handoverId),
    enabled: !!handoverId,
    staleTime: 15_000,
  });

  const entries: ReferencingSessionEntry[] = data ?? [];
  // Sort: open / clarifying first (most actionable), then by recency.
  // Not strictly necessary if the backend already orders, but cheap.
  const ordered = [...entries].sort((a, b) => {
    const rank = (s: ReferencingSessionEntry["state"]) =>
      s === "clarifying" ? 0 : s === "open" ? 1 : s === "briefed" ? 2 : 3;
    const rankDelta = rank(a.state) - rank(b.state);
    if (rankDelta !== 0) return rankDelta;
    return (
      new Date(b.last_activity_at).getTime() -
      new Date(a.last_activity_at).getTime()
    );
  });
  const visible = ordered.slice(0, limit);
  const overflow = ordered.length - visible.length;

  return (
    <section
      className={cn(
        "rounded-lg border border-border bg-bg-1 px-4 py-3",
        className,
      )}
      aria-label="Sessions referencing this handover"
    >
      <header className="flex items-center gap-2">
        <MessageSquare
          className="h-3.5 w-3.5 text-fg-2"
          strokeWidth={1.75}
        />
        <h3 className="text-[13px] font-semibold text-fg-0">
          Referencing sessions
        </h3>
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
            entries.length > 0
              ? "bg-info-soft text-info"
              : "bg-bg-3 text-fg-2",
          )}
        >
          {entries.length}
        </span>
        {isLoading && (
          <Loader2
            className="h-3 w-3 animate-spin text-fg-2"
            strokeWidth={2}
          />
        )}
      </header>

      {isError && (
        <p className="mt-2 inline-flex items-center gap-1 text-[12px] text-err">
          <AlertCircle className="h-3 w-3" strokeWidth={1.75} />
          Couldn't load referencing sessions: {describeError(error)}
        </p>
      )}

      {!isLoading && !isError && entries.length === 0 && (
        <p className="mt-2 rounded-md border border-dashed border-border bg-bg-0 px-3 py-4 text-center text-[12px] text-fg-3">
          No sessions have referenced this handover yet.
        </p>
      )}

      {visible.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {visible.map((entry) => (
            <ReferencingSessionRow key={entry.id} entry={entry} />
          ))}
        </ul>
      )}

      {overflow > 0 && (
        <p className="mt-2 text-[10.5px] text-fg-3">
          +{overflow} more not shown.
        </p>
      )}
    </section>
  );
}

function ReferencingSessionRow({
  entry,
}: {
  entry: ReferencingSessionEntry;
}) {
  const pill = STATE_PILL[entry.state] ?? {
    cls: "bg-bg-3 text-fg-2",
    label: entry.state,
  };
  const truncated =
    entry.requirement_text.length > REQUIREMENT_PREVIEW_LEN
      ? entry.requirement_text.slice(0, REQUIREMENT_PREVIEW_LEN).trim() + "…"
      : entry.requirement_text;
  return (
    <li>
      <Link
        to={`/collaboration/sessions/${entry.id}`}
        className="group flex items-center gap-2 rounded-md border border-border bg-bg-0 px-3 py-2 hover:border-accent/40 hover:bg-bg-2"
        title={entry.requirement_text}
      >
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                pill.cls,
              )}
            >
              {pill.label}
            </span>
            <span className="text-[11px] font-medium text-fg-1">
              {entry.requester}
            </span>
            {entry.doubt_count_open > 0 && (
              <span
                className="inline-flex items-center gap-1 rounded-full bg-warn-soft px-1.5 py-0.5 font-mono text-[10px] font-semibold text-warn"
                title={`${entry.doubt_count_open} open doubt(s)`}
              >
                {entry.doubt_count_open} open
              </span>
            )}
            <span className="ml-auto text-[10.5px] text-fg-3">
              {formatRelativeDate(entry.last_activity_at)}
            </span>
          </div>
          <p className="mt-1 truncate text-[12px] text-fg-2">
            {truncated}
          </p>
        </div>
        <span
          className="inline-flex items-center gap-0.5 text-[10.5px] font-medium text-accent opacity-0 transition-opacity group-hover:opacity-100"
          aria-hidden
        >
          Open session
          <ArrowUpRight className="h-3 w-3" strokeWidth={1.75} />
        </span>
        <ChevronRight
          className="h-3.5 w-3.5 shrink-0 text-fg-3 group-hover:text-fg-1"
          strokeWidth={1.75}
          aria-hidden
        />
      </Link>
    </li>
  );
}
