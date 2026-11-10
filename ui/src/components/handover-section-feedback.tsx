/**
 * Handover section feedback.
 *
 * Two pieces of UI on top of a single endpoint pair:
 *   1. ``HandoverSectionFeedback``: a collapsed-by-default "Flag
 *      section" affordance that expands into a kind picker + optional
 *      note when the reader wants to drop a flag.
 *   2. ``HandoverFeedbackList``: the owner-side view that lists open
 *      flags and lets the owner mark each resolved.
 *
 * Both share the same ``["handover-feedback", handoverId]`` query key
 * so a new submission immediately surfaces in the list without a
 * second round-trip.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Flag,
  Loader2,
  MessageSquare,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  listHandoverSectionFeedback,
  resolveHandoverSectionFeedback,
  submitHandoverSectionFeedback,
  type HandoverFeedbackEntry,
} from "@/lib/api";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";

const NOTE_MAX_CHARS = 2000;

const FEEDBACK_KIND_OPTIONS: ReadonlyArray<{
  value: HandoverFeedbackEntry["kind"];
  label: string;
  hint: string;
}> = [
  {
    value: "outdated",
    label: "Outdated",
    hint: "The content was right once but no longer matches reality.",
  },
  {
    value: "incorrect",
    label: "Incorrect",
    hint: "The content is factually wrong as stated.",
  },
  {
    value: "unclear",
    label: "Unclear",
    hint: "Hard to follow or open to multiple interpretations.",
  },
  {
    value: "missing_info",
    label: "Missing info",
    hint: "A reader-relevant detail isn't captured here.",
  },
  {
    value: "other",
    label: "Other",
    hint: "Doesn't fit the buckets above — describe in the note.",
  },
];

const FEEDBACK_KIND_LABEL: Record<HandoverFeedbackEntry["kind"], string> =
  Object.fromEntries(
    FEEDBACK_KIND_OPTIONS.map((o) => [o.value, o.label]),
  ) as Record<HandoverFeedbackEntry["kind"], string>;

const FEEDBACK_KIND_PILL: Record<
  HandoverFeedbackEntry["kind"],
  string
> = {
  outdated: "bg-warn-soft text-warn",
  incorrect: "bg-err-soft text-err",
  unclear: "bg-info-soft text-info",
  missing_info: "bg-accent-soft text-accent",
  other: "bg-bg-3 text-fg-2",
};

interface HandoverSectionFeedbackProps {
  handoverId: string;
  sectionKey: string;
  sectionTitle: string;
  className?: string;
}

export function HandoverSectionFeedback({
  handoverId,
  sectionKey,
  sectionTitle,
  className,
}: HandoverSectionFeedbackProps) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<HandoverFeedbackEntry["kind"]>("outdated");
  const [note, setNote] = useState("");

  const submit = useMutation({
    mutationFn: () =>
      submitHandoverSectionFeedback(handoverId, {
        section_key: sectionKey,
        kind,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: () => {
      toast.success("Flagged for owner review");
      qc.invalidateQueries({ queryKey: ["handover-feedback", handoverId] });
      qc.invalidateQueries({
        queryKey: ["handover-section-metadata", handoverId],
      });
      qc.invalidateQueries({ queryKey: ["handover-inbox"] });
      reset();
    },
    onError: (err) => {
      toast.error(`Couldn't flag section: ${describeError(err)}`);
    },
  });

  const reset = () => {
    setKind("outdated");
    setNote("");
    setOpen(false);
  };

  const onCancel = () => {
    if (
      note.trim().length > 0 &&
      !window.confirm("Discard this flag draft?")
    ) {
      return;
    }
    reset();
  };

  const overLimit = note.length > NOTE_MAX_CHARS;
  const submitting = submit.isPending;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-1.5 py-0.5 text-[10.5px] font-medium text-fg-2",
          "hover:border-warn/40 hover:bg-warn-soft hover:text-warn",
          className,
        )}
        title={`Flag the "${sectionTitle}" section for owner review`}
        aria-label={`Flag section ${sectionTitle}`}
      >
        <Flag className="h-3 w-3" strokeWidth={1.75} />
        Flag section
      </button>
    );
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (overLimit || submitting) return;
        submit.mutate();
      }}
      className={cn(
        "rounded-md border border-warn/40 bg-warn-soft/20 px-3 py-2.5",
        className,
      )}
      aria-label={`Flag section ${sectionTitle}`}
    >
      <div className="flex items-center gap-2">
        <Flag className="h-3.5 w-3.5 text-warn" strokeWidth={1.75} />
        <p className="text-[12px] font-semibold text-fg-1">
          Flag this section
        </p>
        <span className="text-[10.5px] text-fg-3">
          {sectionTitle}
        </span>
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="ml-auto inline-flex h-5 w-5 items-center justify-center rounded text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          title="Cancel"
          aria-label="Cancel flag"
        >
          <X className="h-3 w-3" strokeWidth={1.75} />
        </button>
      </div>

      <fieldset
        className="mt-2 grid grid-cols-2 gap-1 sm:grid-cols-5"
        aria-label="Reason"
      >
        <legend className="sr-only">Reason</legend>
        {FEEDBACK_KIND_OPTIONS.map((opt) => {
          const active = opt.value === kind;
          return (
            <label
              key={opt.value}
              className={cn(
                "flex cursor-pointer items-center justify-center rounded-md border px-2 py-1 text-[11px] font-medium",
                active
                  ? "border-warn bg-warn text-bg-0"
                  : "border-border bg-bg-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0",
              )}
              title={opt.hint}
            >
              <input
                type="radio"
                name={`feedback-kind-${sectionKey}`}
                value={opt.value}
                checked={active}
                onChange={() => setKind(opt.value)}
                className="sr-only"
              />
              {opt.label}
            </label>
          );
        })}
      </fieldset>

      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Optional note — what specifically is wrong / unclear / missing?"
        className={cn(
          "mt-2 min-h-[68px] w-full resize-none rounded-md border bg-bg-1 px-2.5 py-1.5 text-[12px] text-fg-0 placeholder:text-fg-3 focus:outline-none",
          overLimit
            ? "border-err focus:border-err"
            : "border-border focus:border-accent",
        )}
        maxLength={NOTE_MAX_CHARS + 200}
      />

      <div className="mt-2 flex items-center justify-between gap-2">
        <span
          className={cn(
            "text-[10.5px] font-mono",
            overLimit ? "text-err" : "text-fg-3",
          )}
        >
          {note.length} / {NOTE_MAX_CHARS}
          {overLimit && " · trim before submitting"}
        </span>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || overLimit}
            className={cn(
              "inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium",
              submitting || overLimit
                ? "bg-bg-3 text-fg-3"
                : "bg-warn text-bg-0 hover:bg-warn/90",
            )}
          >
            {submitting ? (
              <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
            ) : (
              <Flag className="h-3 w-3" strokeWidth={1.75} />
            )}
            {submitting ? "Flagging…" : "Submit flag"}
          </button>
        </div>
      </div>
    </form>
  );
}

interface HandoverFeedbackListProps {
  handoverId: string;
  /** When true, surface already-resolved entries alongside the open list. */
  includeResolved?: boolean;
  className?: string;
}

export function HandoverFeedbackList({
  handoverId,
  includeResolved = false,
  className,
}: HandoverFeedbackListProps) {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["handover-feedback", handoverId, { includeResolved }],
    queryFn: () =>
      listHandoverSectionFeedback(handoverId, {
        include_resolved: includeResolved,
      }),
    enabled: !!handoverId,
    staleTime: 15_000,
  });

  const entries: HandoverFeedbackEntry[] = data ?? [];
  const openCount = entries.filter((e) => e.resolved_at == null).length;

  return (
    <section
      className={cn(
        "rounded-lg border border-border bg-bg-1 px-4 py-3",
        className,
      )}
      aria-label="Section feedback"
    >
      <header className="flex items-center gap-2">
        <Flag className="h-3.5 w-3.5 text-warn" strokeWidth={1.75} />
        <h3 className="text-[13px] font-semibold text-fg-0">
          Section feedback
        </h3>
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
            openCount > 0
              ? "bg-warn-soft text-warn"
              : "bg-bg-3 text-fg-2",
          )}
        >
          {openCount} open
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
          Couldn't load feedback: {describeError(error)}
        </p>
      )}

      {!isLoading && !isError && entries.length === 0 && (
        <p className="mt-2 text-[12px] text-fg-3">
          No flags yet. Readers can drop a flag on any section header
          and it'll show up here for review.
        </p>
      )}

      {entries.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {entries.map((entry) => (
            <FeedbackRow
              key={entry.id}
              entry={entry}
              onResolve={async () => {
                try {
                  await resolveHandoverSectionFeedback(handoverId, entry.id);
                  toast.success("Marked resolved");
                  qc.invalidateQueries({
                    queryKey: ["handover-feedback", handoverId],
                  });
                  qc.invalidateQueries({
                    queryKey: ["handover-section-metadata", handoverId],
                  });
                  qc.invalidateQueries({ queryKey: ["handover-inbox"] });
                } catch (err) {
                  toast.error(`Resolve failed: ${describeError(err)}`);
                }
              }}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function FeedbackRow({
  entry,
  onResolve,
}: {
  entry: HandoverFeedbackEntry;
  onResolve: () => void | Promise<void>;
}) {
  const [resolving, setResolving] = useState(false);
  const resolved = entry.resolved_at != null;
  return (
    <li
      className={cn(
        "rounded-md border border-border bg-bg-0 p-2.5",
        resolved && "opacity-60",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
            FEEDBACK_KIND_PILL[entry.kind],
          )}
        >
          {FEEDBACK_KIND_LABEL[entry.kind] ?? entry.kind}
        </span>
        <span className="font-mono text-[10.5px] text-fg-3">
          {entry.section_key}
        </span>
        <span className="text-[10.5px] text-fg-3">·</span>
        <span className="text-[10.5px] text-fg-3">
          {formatRelativeDate(entry.created_at)}
        </span>
        {entry.actor && (
          <>
            <span className="text-[10.5px] text-fg-3">·</span>
            <span className="text-[10.5px] text-fg-3">{entry.actor}</span>
          </>
        )}
        {!resolved ? (
          <button
            type="button"
            onClick={async () => {
              if (resolving) return;
              setResolving(true);
              try {
                await onResolve();
              } finally {
                setResolving(false);
              }
            }}
            disabled={resolving}
            className={cn(
              "ml-auto inline-flex items-center gap-1 rounded-md border border-ok/40 bg-ok-soft px-1.5 py-0.5 text-[10.5px] font-medium text-ok",
              "hover:bg-ok hover:text-bg-0",
              resolving && "opacity-60",
            )}
            title="Mark this flag resolved"
          >
            {resolving ? (
              <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
            ) : (
              <CheckCircle2 className="h-3 w-3" strokeWidth={1.75} />
            )}
            {resolving ? "Resolving…" : "Resolve"}
          </button>
        ) : (
          <span className="ml-auto inline-flex items-center gap-1 text-[10.5px] text-ok">
            <CheckCircle2 className="h-3 w-3" strokeWidth={1.75} />
            Resolved {formatRelativeDate(entry.resolved_at)}
            {entry.resolved_by ? ` by ${entry.resolved_by}` : ""}
          </span>
        )}
      </div>
      {entry.note && (
        <p className="mt-1.5 inline-flex items-start gap-1 text-[12px] leading-snug text-fg-1">
          <MessageSquare
            className="mt-[3px] h-3 w-3 shrink-0 text-fg-3"
            strokeWidth={1.75}
            aria-hidden
          />
          <span>{entry.note}</span>
        </p>
      )}
    </li>
  );
}
