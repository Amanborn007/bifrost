/**
 * MasterDetail — three-column triage layout.
 *
 *   ┌──────────┬─────────────────────────┬───────────────────────┐
 *   │ Bucket   │ Filter bar              │ Detail header         │
 *   │ rail     ├─────────────────────────┤                       │
 *   │ • Open   │ Item row                │ Detail body           │
 *   │ • Block  │ Item row [selected]     │                       │
 *   │ • Done   │ Item row                │ ──────────────────────│
 *   │          │ ...                     │ Actions               │
 *   └──────────┴─────────────────────────┴───────────────────────┘
 *
 * Built once, applied wherever the operator triages a stream of
 * objects (Tasks, Asks, PR reviews). Item identity stays in the URL
 * (``?bucket=blocked&id=abc-123``) so refresh/back/share work; nothing
 * is lost when the operator pivots to another tab and comes back.
 *
 * Keyboard:
 *   ↑ / ↓     navigate within the visible list
 *   [ / ]     previous / next bucket
 *   Enter     trigger ``onOpenFull`` for the selected item
 *               (typically navigates to a deep route)
 *   Esc       clear selection
 *
 * Empty + loading states are owned by the consumer — pass
 * ``loading`` and rendered ``emptyList`` / ``emptyDetail`` nodes so
 * each surface can speak its own copy.
 */

import {
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import { cn } from "@/lib/utils";


// ── Types ─────────────────────────────────────────────────────────


export type BucketTone =
  | "ok" | "warn" | "err" | "info" | "review" | "default";


export interface Bucket {
  id: string;
  label: string;
  icon?: ReactNode;
  count?: number;
  tone?: BucketTone;
  hint?: string;          // hover tooltip
}


export interface MasterDetailProps<T> {
  // ── Buckets (left rail) ──────────────────────────────────────
  buckets: Bucket[];
  /** Optional default bucket id when the URL has none. Falls back to
   *  the first bucket. */
  defaultBucketId?: string;

  // ── Items (middle list) ───────────────────────────────────────
  items: T[];
  itemKey: (item: T) => string;
  renderItem: (
    item: T,
    state: { selected: boolean; index: number },
  ) => ReactNode;

  // ── Detail (right panel) ──────────────────────────────────────
  renderDetail: (item: T | null) => ReactNode;

  // ── Optional ─────────────────────────────────────────────────
  filterBar?: ReactNode;
  /** Called when ↑/↓ moves selection. Useful for preloading. */
  onSelectionChange?: (id: string | null) => void;
  /** Called when Enter is pressed on the selected item. */
  onOpenFull?: (item: T) => void;
  emptyList?: ReactNode;
  emptyDetail?: ReactNode;
  loading?: boolean;
  loadingNode?: ReactNode;
  className?: string;
}


// ── URL sync ──────────────────────────────────────────────────────


function useURLState(defaultBucket: string) {
  const [params, setParams] = useSearchParams();
  const bucket = params.get("bucket") ?? defaultBucket;
  const selectedId = params.get("id");
  const setBucket = (b: string) => {
    const next = new URLSearchParams(params);
    next.set("bucket", b);
    // Switching bucket clears the selection — we don't know if the
    // current item is in the new bucket, and stale selection is worse
    // than no selection.
    next.delete("id");
    setParams(next, { replace: true });
  };
  const setSelectedId = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("id", id);
    else next.delete("id");
    setParams(next, { replace: true });
  };
  return { bucket, selectedId, setBucket, setSelectedId };
}


// ── Component ─────────────────────────────────────────────────────


export function MasterDetail<T>({
  buckets,
  defaultBucketId,
  items,
  itemKey,
  renderItem,
  renderDetail,
  filterBar,
  onSelectionChange,
  onOpenFull,
  emptyList,
  emptyDetail,
  loading,
  loadingNode,
  className,
}: MasterDetailProps<T>) {
  const fallbackBucket = defaultBucketId ?? buckets[0]?.id ?? "";
  const { bucket, selectedId, setBucket, setSelectedId } =
    useURLState(fallbackBucket);

  // Derive the selected item from the URL.
  const selected = useMemo(
    () => items.find((it) => itemKey(it) === selectedId) ?? null,
    [items, itemKey, selectedId],
  );

  // Keep callers informed of selection changes (for prefetch hooks).
  useEffect(() => {
    onSelectionChange?.(selectedId);
  }, [selectedId, onSelectionChange]);

  // Keyboard navigation. Scoped to ``window`` so the page-level
  // shortcuts work even when focus is in the bucket rail or filter
  // bar — but we ignore typing inside an <input> / <textarea>.
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const bucketRef = useRef(bucket);
  bucketRef.current = bucket;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName ?? "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      const list = itemsRef.current;
      const curId = selectedRef.current;
      const curIdx = curId
        ? list.findIndex((it) => itemKey(it) === curId)
        : -1;

      if (e.key === "ArrowDown" || e.key === "j") {
        if (list.length === 0) return;
        e.preventDefault();
        const next = curIdx < 0 ? 0 : Math.min(curIdx + 1, list.length - 1);
        setSelectedId(itemKey(list[next]));
      } else if (e.key === "ArrowUp" || e.key === "k") {
        if (list.length === 0) return;
        e.preventDefault();
        const next = curIdx <= 0 ? 0 : curIdx - 1;
        setSelectedId(itemKey(list[next]));
      } else if (e.key === "]") {
        e.preventDefault();
        const idx = buckets.findIndex((b) => b.id === bucketRef.current);
        if (idx >= 0 && idx < buckets.length - 1) {
          setBucket(buckets[idx + 1].id);
        }
      } else if (e.key === "[") {
        e.preventDefault();
        const idx = buckets.findIndex((b) => b.id === bucketRef.current);
        if (idx > 0) {
          setBucket(buckets[idx - 1].id);
        }
      } else if (e.key === "Enter" && curId) {
        const it = list.find((x) => itemKey(x) === curId);
        if (it && onOpenFull) {
          e.preventDefault();
          onOpenFull(it);
        }
      } else if (e.key === "Escape" && curId) {
        e.preventDefault();
        setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // setBucket / setSelectedId are URL-derived and stable across
    // renders for the purposes of this listener.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buckets, itemKey, onOpenFull]);

  // Auto-scroll the selected row into view when keyboard nav moves
  // off-screen.
  const listRef = useRef<HTMLUListElement | null>(null);
  useEffect(() => {
    if (!selectedId || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLLIElement>(
      `[data-md-id="${CSS.escape(selectedId)}"]`,
    );
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-bg-1",
        className,
      )}
    >
      {/* ── Bucket rail ─────────────────────────────────────────── */}
      <aside className="hidden w-[180px] shrink-0 flex-col border-r border-border bg-bg-1 md:flex">
        <div className="section-title px-4 pt-3 pb-1.5">View</div>
        <ul className="flex-1 overflow-y-auto px-2 pb-2">
          {buckets.map((b) => {
            const active = b.id === bucket;
            return (
              <li key={b.id}>
                <button
                  type="button"
                  onClick={() => setBucket(b.id)}
                  title={b.hint}
                  className={cn(
                    "group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition-colors",
                    active
                      ? "bg-bg-3 text-fg-0"
                      : "text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                  )}
                >
                  {b.icon && (
                    <span
                      className={cn(
                        "shrink-0",
                        active ? "text-accent" : "text-fg-2",
                      )}
                    >
                      {b.icon}
                    </span>
                  )}
                  <span className="flex-1 truncate">{b.label}</span>
                  {typeof b.count === "number" && b.count > 0 && (
                    <span
                      className={cn(
                        "badge shrink-0 font-mono",
                        bucketBadgeClass(b.tone, active),
                      )}
                    >
                      {b.count > 99 ? "99+" : b.count}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </aside>

      {/* ── List ────────────────────────────────────────────────── */}
      <div className="flex w-full max-w-[460px] shrink-0 flex-col border-r border-border md:w-[420px]">
        {filterBar && (
          <div className="border-b border-border px-3 py-2">{filterBar}</div>
        )}
        {loading ? (
          <div className="flex-1 overflow-y-auto p-2">
            {loadingNode ?? <DefaultLoading />}
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-1 items-center justify-center p-6">
            {emptyList ?? <DefaultEmpty />}
          </div>
        ) : (
          <ul
            ref={listRef}
            className="flex-1 divide-y divide-border overflow-y-auto"
            role="listbox"
            aria-label="Items"
          >
            {items.map((it, idx) => {
              const id = itemKey(it);
              const isSelected = id === selectedId;
              return (
                <li
                  key={id}
                  data-md-id={id}
                  role="option"
                  aria-selected={isSelected}
                  className={cn(
                    "cursor-pointer transition-colors",
                    isSelected
                      ? "bg-bg-2"
                      : "hover:bg-bg-2/60",
                    isSelected && "border-l-2 border-l-accent",
                  )}
                  onClick={() => setSelectedId(id)}
                >
                  {renderItem(it, { selected: isSelected, index: idx })}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* ── Detail ──────────────────────────────────────────────── */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {selected ? (
          <div className="flex-1 overflow-y-auto">{renderDetail(selected)}</div>
        ) : (
          <div className="flex flex-1 items-center justify-center p-8">
            {emptyDetail ?? <DefaultDetailEmpty />}
          </div>
        )}
      </div>
    </div>
  );
}


// ── Helpers ──────────────────────────────────────────────────────


function bucketBadgeClass(
  tone: BucketTone | undefined,
  active: boolean,
): string {
  // Active rail items deserve a slightly more saturated badge so the
  // selected row visually anchors. Inactive use the soft tint.
  if (active && (!tone || tone === "default")) return "badge-neutral";
  switch (tone) {
    case "ok":
      return "badge-ok";
    case "warn":
      return "badge-warn";
    case "err":
      return "badge-err";
    case "info":
      return "badge-info";
    case "review":
      return "badge-review";
    default:
      return "badge-neutral";
  }
}


function DefaultLoading() {
  return (
    <ul className="space-y-2">
      {[1, 2, 3, 4, 5].map((i) => (
        <li key={i} className="skel h-14" />
      ))}
    </ul>
  );
}


function DefaultEmpty() {
  return (
    <p className="text-center text-[12.5px] text-fg-3">
      Nothing in this bucket.
    </p>
  );
}


function DefaultDetailEmpty() {
  return (
    <div className="text-center text-[12.5px] text-fg-3">
      <p>Select an item to view details.</p>
      <p className="mt-1 text-[11px]">
        Use <kbd className="kbd">↑</kbd> <kbd className="kbd">↓</kbd>{" "}
        to navigate, <kbd className="kbd">Enter</kbd> to open the full
        page.
      </p>
    </div>
  );
}
