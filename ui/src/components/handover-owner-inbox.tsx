/**
 * Owner inbox panel.
 *
 * "What needs my attention right now?" digest for handover owners.
 * Each row aggregates the four signals an owner cares about:
 *   1. ``freshness_state`` — has the underlying source drifted
 *   2. ``open_feedback_count`` — has anyone flagged a section
 *   3. ``referencing_sessions_count`` — are people actively asking
 *   4. ``doubt_candidates_count`` — are clarifiers landing on the same
 *      gap repeatedly (loop-back signal worth promoting)
 *
 * Designed to drop into the /collaboration landing page alongside
 * the existing sessions / handovers panels — no wiring done here,
 * just the standalone block.
 */

import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  AlertCircle,
  ChevronRight,
  Flag,
  HelpCircle,
  Inbox,
  Loader2,
  MessageSquare,
} from "lucide-react";

import { getHandoverInbox, type HandoverInboxEntry } from "@/lib/api";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";

const FRESHNESS_PILL: Record<string, string> = {
  fresh: "bg-ok-soft text-ok",
  stale: "bg-warn-soft text-warn",
  very_stale: "bg-err-soft text-err",
  enriched_by_children: "bg-info-soft text-info",
  stale_due_to_children: "bg-warn-soft text-warn",
};

const FRESHNESS_LABEL: Record<string, string> = {
  fresh: "fresh",
  stale: "stale",
  very_stale: "very stale",
  enriched_by_children: "enriched",
  stale_due_to_children: "stale (children)",
};

const POLL_INTERVAL = 30_000;

interface HandoverOwnerInboxProps {
  className?: string;
  /** Cap the rendered list (default 10). Server is expected to rank already. */
  limit?: number;
}

export function HandoverOwnerInbox({
  className,
  limit = 10,
}: HandoverOwnerInboxProps) {
  const navigate = useNavigate();
  const { data, isLoading, isError, error, isRefetching } = useQuery({
    queryKey: ["handover-inbox"],
    queryFn: getHandoverInbox,
    refetchInterval: POLL_INTERVAL,
    staleTime: 15_000,
  });

  const entries: HandoverInboxEntry[] = data ?? [];
  const visible = entries.slice(0, limit);
  const overflow = entries.length - visible.length;

  return (
    <section
      className={cn(
        "rounded-lg border border-border bg-bg-1 px-5 py-4",
        className,
      )}
      aria-label="Handover owner inbox"
    >
      <header className="flex items-center gap-2">
        <Inbox className="h-4 w-4 text-fg-2" strokeWidth={1.75} />
        <h3 className="text-[13px] font-semibold text-fg-0">
          Needs your attention
        </h3>
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
            entries.length > 0
              ? "bg-warn-soft text-warn"
              : "bg-bg-3 text-fg-2",
          )}
        >
          {entries.length}
        </span>
        {(isLoading || isRefetching) && (
          <Loader2
            className="h-3 w-3 animate-spin text-fg-2"
            strokeWidth={2}
          />
        )}
      </header>

      <p className="mt-1 text-[11.5px] text-fg-2">
        Handovers with stale source data, open section flags, active
        clarifier sessions, or recurring doubts worth promoting.
      </p>

      {isError && (
        <p className="mt-3 inline-flex items-center gap-1 text-[12px] text-err">
          <AlertCircle className="h-3 w-3" strokeWidth={1.75} />
          Couldn't load inbox: {describeError(error)}
        </p>
      )}

      {!isLoading && !isError && entries.length === 0 && (
        <p className="mt-3 rounded-md border border-dashed border-border bg-bg-0 px-3 py-5 text-center text-[12px] text-fg-3">
          No handovers need your attention right now.
        </p>
      )}

      {visible.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {visible.map((entry) => (
            <InboxRow
              key={entry.id}
              entry={entry}
              onOpen={() => navigate(`/collaboration/handovers/${entry.id}`)}
            />
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

function InboxRow({
  entry,
  onOpen,
}: {
  entry: HandoverInboxEntry;
  onOpen: () => void;
}) {
  const freshnessCls =
    FRESHNESS_PILL[entry.freshness_state] ?? "bg-bg-3 text-fg-2";
  const freshnessLabel =
    FRESHNESS_LABEL[entry.freshness_state] ?? entry.freshness_state;
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="group flex w-full items-center gap-2 rounded-md border border-border bg-bg-0 px-3 py-2 text-left hover:border-accent/40 hover:bg-bg-2"
      >
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-fg-3">
              {entry.scope_kind}
            </span>
            <span
              className="truncate text-[12.5px] font-medium text-fg-0"
              title={entry.scope_value}
            >
              {entry.scope_value}
            </span>
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide",
                freshnessCls,
              )}
            >
              {freshnessLabel}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-fg-3">
            <CountChip
              icon={
                <Flag className="h-2.5 w-2.5" strokeWidth={1.75} aria-hidden />
              }
              count={entry.open_feedback_count}
              label={`${entry.open_feedback_count} ${entry.open_feedback_count === 1 ? "flag" : "flags"}`}
              tone="warn"
              title={`${entry.open_feedback_count} open section flag(s)`}
            />
            <CountChip
              icon={
                <MessageSquare
                  className="h-2.5 w-2.5"
                  strokeWidth={1.75}
                  aria-hidden
                />
              }
              count={entry.referencing_sessions_count}
              label={`${entry.referencing_sessions_count} ${entry.referencing_sessions_count === 1 ? "session" : "sessions"}`}
              tone="info"
              title={`${entry.referencing_sessions_count} active clarifier session(s)`}
            />
            <CountChip
              icon={
                <HelpCircle
                  className="h-2.5 w-2.5"
                  strokeWidth={1.75}
                  aria-hidden
                />
              }
              count={entry.doubt_candidates_count}
              label={`${entry.doubt_candidates_count} ${entry.doubt_candidates_count === 1 ? "doubt" : "doubts"}`}
              tone="accent"
              title={`${entry.doubt_candidates_count} recurring doubt candidate(s)`}
            />
            <span aria-hidden>·</span>
            <span title={`Last touched at ${entry.last_touched_at}`}>
              {formatRelativeDate(entry.last_touched_at)}
            </span>
          </div>
        </div>
        <ChevronRight
          className="h-3.5 w-3.5 shrink-0 text-fg-3 group-hover:text-fg-1"
          strokeWidth={1.75}
          aria-hidden
        />
      </button>
    </li>
  );
}

function CountChip({
  icon,
  count,
  label,
  tone,
  title,
}: {
  icon: React.ReactNode;
  count: number;
  label: string;
  tone: "warn" | "info" | "accent";
  title: string;
}) {
  // Zero-counts render in muted neutral so the eye glides to the
  // rows with actual signal.
  const cls =
    count === 0
      ? "bg-bg-2 text-fg-3"
      : tone === "warn"
        ? "bg-warn-soft text-warn"
        : tone === "info"
          ? "bg-info-soft text-info"
          : "bg-accent-soft text-accent";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
        cls,
      )}
      title={title}
    >
      {icon}
      {label}
    </span>
  );
}
