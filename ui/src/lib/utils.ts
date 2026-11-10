import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Single source of truth for date display. The codebase had three
 * different date formats across pages (Dashboard's
 * ``toLocaleDateString``, Query's ``Nov 25``, Task-detail's
 * ``11/25/2025, 3:45 PM``); engineers building mental models of
 * "when did this happen" got tripped up by the format shifts.
 *
 * Pass an ISO string or Date; returns one of:
 *   "just now"         (< 60s ago)
 *   "5m ago"           (< 60m ago)
 *   "3h ago"           (< 24h ago)
 *   "Mon Nov 25"       (this year)
 *   "Nov 25 2024"      (older)
 */
export function formatRelativeDate(input: string | Date | null | undefined): string {
  if (!input) return "—";
  const d = input instanceof Date ? input : new Date(input);
  if (isNaN(d.getTime())) return "—";
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString("en-US", {
    weekday: sameYear ? "short" : undefined,
    month: "short",
    day: "numeric",
    year: sameYear ? undefined : "numeric",
  });
}

/**
 * Single source of truth for "show this error to the user" messages.
 *
 * The codebase had ``toast.error(\`Failed: ${err.message}\`)`` patterns
 * that produced ``Failed: undefined`` when the err was a non-Error
 * (e.g., an ApiError with a ``detail`` field, or a thrown plain
 * object). Engineers panic-refreshed thinking features were broken
 * when really a transient server hiccup just hadn't surfaced its
 * message.
 *
 * Pass any thrown value; returns a non-empty user-facing string.
 */
export function describeError(err: unknown): string {
  if (err == null) return "Unknown error";
  if (typeof err === "object") {
    const body = (err as { detail?: { error?: { code?: string; message?: string } } }).detail;
    const code = body?.error?.code;
    if (
      (code === "auth.role_required" || code === "settings.walk_breadth_above_ceiling") &&
      body?.error?.message
    ) {
      return body.error.message;
    }
  }
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message || err.name || "Error";
  if (typeof err === "object") {
    const e = err as Record<string, unknown>;
    // ApiError shape from lib/api.ts carries detail.message or detail.error.
    if (e.detail && typeof e.detail === "object") {
      const d = e.detail as Record<string, unknown>;
      if (typeof d.message === "string") return d.message;
      if (typeof d.error === "string") return d.error;
    }
    if (typeof e.message === "string") return e.message;
    if (typeof e.error === "string") return e.error;
    if (typeof e.detail === "string") return e.detail;
  }
  return "Unknown error";
}
