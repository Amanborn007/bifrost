/**
 * Restore a handover body to a prior snapshot.
 *
 * The diff modal in the detail page already fetches the body-history
 * list to render a unified diff against the current body; this
 * component piggy-backs on that data and lets the operator pick any
 * snapshot and *restore* it. The restore endpoint snapshots the
 * current body as ``pre-restore`` first, so the operation is
 * reversible — undoing the undo just picks the new ``pre-restore``
 * entry from the history list.
 */

import { useEffect, useMemo, useState } from "react";
import {
  History,
  Loader2,
  RotateCcw,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  restoreHandoverVersion,
  type HandoverBodyHistoryEntry,
  type HandoverBodyHistoryEventType,
} from "@/lib/api";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";

const HISTORY_PILL: Record<
  HandoverBodyHistoryEventType,
  { cls: string; label: string }
> = {
  "pre-regen": { cls: "bg-accent-soft text-accent", label: "Before regen" },
  "pre-edit": { cls: "bg-info-soft text-info", label: "Before edit" },
  "pre-promote": {
    cls: "bg-warn-soft text-warn",
    label: "Before promote",
  },
};

interface HandoverRestoreVersionProps {
  handoverId: string;
  snapshots: HandoverBodyHistoryEntry[];
  onClose: () => void;
  /** Called after a successful restore; parent should invalidate caches + refetch. */
  onRestored: () => void;
}

export function HandoverRestoreVersion({
  handoverId,
  snapshots,
  onClose,
  onRestored,
}: HandoverRestoreVersionProps) {
  // Sort newest-first so the most-likely target is at the top. We
  // copy the array because the parent might pass an already-sorted
  // (or differently-sorted) reference and we don't want to mutate.
  const ordered = useMemo(
    () =>
      [...snapshots].sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      ),
    [snapshots],
  );

  const [restoringId, setRestoringId] = useState<string | null>(null);

  // ESC dismisses — bound at window level so the dialog doesn't have
  // to be focused for the key to fire (matches HandoverDiffModal).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !restoringId) {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose, restoringId]);

  const onRestore = async (entry: HandoverBodyHistoryEntry) => {
    if (restoringId) return;
    const ok = window.confirm(
      "Restore body to this version? Current body will be snapshotted as 'pre-restore' so the restore itself is reversible.",
    );
    if (!ok) return;
    setRestoringId(entry.id);
    try {
      await restoreHandoverVersion(handoverId, entry.id);
      const pill = HISTORY_PILL[entry.event_type];
      toast.success(
        `Restored ${pill?.label ?? entry.event_type} (${formatRelativeDate(
          entry.created_at,
        )})`,
      );
      onRestored();
      onClose();
    } catch (err) {
      toast.error(`Restore failed: ${describeError(err)}`);
    } finally {
      setRestoringId(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-center bg-bg-0/80 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="Restore a previous handover body version"
      onClick={() => {
        if (!restoringId) onClose();
      }}
    >
      <div
        className="my-auto flex h-[min(80vh,720px)] w-full max-w-[720px] flex-col rounded-lg border border-border bg-bg-1 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-2 border-b border-border px-5 py-3">
          <History className="h-4 w-4 text-fg-2" strokeWidth={1.75} />
          <h2 className="text-[14px] font-semibold text-fg-0">
            Restore previous version
          </h2>
          <span className="font-mono text-[10.5px] text-fg-3">
            ({ordered.length})
          </span>
          <button
            type="button"
            onClick={onClose}
            disabled={!!restoringId}
            className="ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md border border-border bg-bg-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0 disabled:opacity-50"
            title="Close (Esc)"
            aria-label="Close restore picker"
          >
            <X className="h-3.5 w-3.5" strokeWidth={1.75} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-5 py-4">
          <p className="text-[12px] text-fg-2">
            Pick a snapshot to roll the on-disk Markdown back to. The
            current body is snapshotted as{" "}
            <code className="rounded bg-bg-2 px-1 text-fg-1">pre-restore</code>{" "}
            first, so this is reversible.
          </p>
          {ordered.length === 0 ? (
            <p className="mt-4 rounded-md border border-dashed border-border bg-bg-0 px-3 py-6 text-center text-[12px] text-fg-3">
              No prior versions yet. A snapshot is captured before
              every regen, edit, or promote — make a change to start
              the history.
            </p>
          ) : (
            <ul className="mt-3 space-y-1.5">
              {ordered.map((entry) => {
                const pill = HISTORY_PILL[entry.event_type] ?? {
                  cls: "bg-bg-3 text-fg-2",
                  label: entry.event_type,
                };
                const restoring = restoringId === entry.id;
                const otherInFlight = !!restoringId && !restoring;
                const previewLen = 90;
                const preview = entry.body_md
                  .split("\n")
                  .find((line) => line.trim().length > 0)
                  ?.trim()
                  .slice(0, previewLen);
                return (
                  <li
                    key={entry.id}
                    className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-bg-0 px-3 py-2"
                  >
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                        pill.cls,
                      )}
                    >
                      {pill.label}
                    </span>
                    <span className="text-[11.5px] text-fg-1">
                      {formatRelativeDate(entry.created_at)}
                    </span>
                    {preview && (
                      <span
                        className="hidden min-w-0 flex-1 truncate text-[11px] text-fg-3 sm:inline"
                        title={preview}
                      >
                        — {preview}
                        {entry.body_md.length > previewLen ? "…" : ""}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => onRestore(entry)}
                      disabled={otherInFlight || restoring}
                      className={cn(
                        "ml-auto inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium",
                        otherInFlight
                          ? "bg-bg-3 text-fg-3"
                          : "bg-accent text-accent-fg hover:bg-accent-hover",
                      )}
                      title="Restore the on-disk body to this snapshot"
                    >
                      {restoring ? (
                        <Loader2
                          className="h-3 w-3 animate-spin"
                          strokeWidth={2}
                        />
                      ) : (
                        <RotateCcw className="h-3 w-3" strokeWidth={1.75} />
                      )}
                      {restoring ? "Restoring…" : "Restore this version"}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
