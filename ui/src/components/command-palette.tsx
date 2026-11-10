/**
 * CommandPalette — universal Cmd+K (or Ctrl+K) overlay.
 *
 * One keystroke pulls up:
 *
 *   • Jump to: every top-level page in the app
 *   • My inbox: live items from the same `/collaboration/inbox`
 *     aggregator that powers the dashboard hero + sidebar badges
 *   • Recent chats: last 8 conversations
 *   • Actions: New chat, New ask, New task (Tasks page)
 *
 * Why this exists: the whole product is one connected graph (chats →
 * tasks → Jira → PRs → asks → benchmarks). The palette is the
 * "everywhere link" — operator hits ⌘K, types two letters, presses
 * Enter, lands on the right object. No mouse, no menu-diving.
 *
 * Filter: case-insensitive substring match on every visible label.
 * Keyboard: ↑/↓ navigates, Enter selects, Esc closes, ⌘K toggles.
 *
 * Mounted once in `Layout` so the global key listener never doubles
 * up. Reuses TanStack Query keys so it shares fetches with the rest
 * of the app.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  ClipboardList,
  Command,
  GitPullRequest,
  Hourglass,
  LayoutDashboard,
  MessageSquare,
  Plus,
  Receipt,
  BookOpen,
  Activity,
  Users,
  Zap,
  AlertTriangle,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { getInbox, listConversations } from "@/lib/api";


// ── Types ─────────────────────────────────────────────────────────


type ItemKind =
  | "page"
  | "task"
  | "ask"
  | "pr"
  | "chat"
  | "action";


type HintTone = "ok" | "warn" | "err" | "info" | "review" | "default";


interface PaletteItem {
  id: string;
  kind: ItemKind;
  label: string;
  hint?: string;          // right-aligned tone-coded badge text
  hintTone?: HintTone;
  meta?: string;          // dim secondary text (e.g., jira_key, route)
  icon: ReactNode;
  onSelect: () => void;
}


interface PaletteSection {
  id: string;
  title: string;
  items: PaletteItem[];
}


// ── Hint tone — same five-tone vocabulary as the inbox + sidebar.
// Just the foreground colour here; the palette row already supplies
// its own background, so we don't want a second tinted bg layered on
// top.


const HINT_TONE_CLASS: Record<HintTone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  err: "text-err",
  info: "text-info",
  review: "text-review",
  default: "text-fg-3",
};


// ── Component ─────────────────────────────────────────────────────


export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Global ⌘K / Ctrl+K listener. Esc closes. Mounted once. Also
  // listens for a custom ``bifrost:palette:open`` event so a top-bar
  // search button (or anything else) can open the palette without
  // prop-drilling — keeps the palette decoupled from its triggers.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        return;
      }
      if (e.key === "Escape" && open) {
        setOpen(false);
      }
    };
    const onCustomOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("bifrost:palette:open", onCustomOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("bifrost:palette:open", onCustomOpen);
    };
  }, [open]);

  // Re-focus the input every time we open and reset state.
  useEffect(() => {
    if (open) {
      // Reset palette state on the open transition, paired with imperative
      // focus of the just-mounted input (an external/DOM side effect).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setQuery("");
      setSelected(0);
      // Wait for the dialog to mount then focus.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // Inbox + recent conversations — same query keys as the rest of the
  // app so we don't re-fetch.
  const inbox = useQuery({
    queryKey: ["inbox", "me"],
    queryFn: () => getInbox(),
    enabled: open,
    staleTime: 10_000,
    retry: false,
  });
  const convs = useQuery({
    queryKey: ["conversations"],
    queryFn: listConversations,
    enabled: open,
    staleTime: 10_000,
    retry: false,
  });

  // Build sections. Memoized on the data + nav callback so we don't
  // rebuild on every keystroke.
  const sections = useMemo<PaletteSection[]>(() => {
    const close = () => setOpen(false);
    const go = (path: string) => {
      navigate(path);
      close();
    };

    const pages: PaletteItem[] = [
      {
        id: "page:overview",
        kind: "page",
        label: "Overview",
        meta: "/overview",
        icon: <LayoutDashboard className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/overview"),
      },
      {
        id: "page:chat",
        kind: "page",
        label: "Chat",
        meta: "/query",
        icon: <MessageSquare className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/query"),
      },
      {
        id: "page:tasks",
        kind: "page",
        label: "Tasks",
        meta: "/tasks",
        icon: <ClipboardList className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/tasks"),
      },
      {
        id: "page:collab",
        kind: "page",
        label: "Collaboration",
        meta: "/collaboration",
        icon: <Users className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/collaboration"),
      },
      {
        id: "page:knowledge",
        kind: "page",
        label: "Knowledge",
        meta: "/knowledge",
        icon: <BookOpen className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/knowledge"),
      },
      {
        id: "page:spend",
        kind: "page",
        label: "Spend",
        meta: "/costs",
        icon: <Receipt className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/costs"),
      },
      {
        id: "page:learning",
        kind: "page",
        label: "Learning",
        meta: "/admin/learning",
        icon: <Activity className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go("/admin/learning"),
      },
    ];

    const actions: PaletteItem[] = [
      {
        id: "action:new-chat",
        kind: "action",
        label: "New chat",
        meta: "Start a new conversation",
        icon: <Plus className="h-3.5 w-3.5" strokeWidth={2} />,
        onSelect: () => go("/query"),
      },
      {
        id: "action:new-ask",
        kind: "action",
        label: "New ask",
        meta: "Route a question to a squad",
        icon: <Plus className="h-3.5 w-3.5" strokeWidth={2} />,
        onSelect: () => go("/collaboration/new"),
      },
      {
        id: "action:new-task",
        kind: "action",
        label: "New task",
        meta: "Spawn a task from chat",
        icon: <Plus className="h-3.5 w-3.5" strokeWidth={2} />,
        onSelect: () => go("/tasks"),
      },
    ];

    const inboxData = inbox.data;
    const inboxItems: PaletteItem[] = [];
    if (inboxData) {
      for (const a of inboxData.escalated_asks.slice(0, 3)) {
        inboxItems.push({
          id: `ask:${a.id}`,
          kind: "ask",
          label: a.question,
          hint: "escalated",
          hintTone: "err",
          meta: a.target_squad ? `→ ${a.target_squad}` : "no route",
          icon: <AlertTriangle className="h-3.5 w-3.5" strokeWidth={1.75} />,
          onSelect: () => go("/collaboration#asks"),
        });
      }
      for (const t of inboxData.blocked_tasks.slice(0, 3)) {
        inboxItems.push({
          id: `task:blocked:${t.id}`,
          kind: "task",
          label: t.title,
          hint: "blocked",
          hintTone: "warn",
          meta: t.jira_key ?? t.state,
          icon: <Zap className="h-3.5 w-3.5" strokeWidth={1.75} />,
          onSelect: () => go(`/tasks/${t.id}`),
        });
      }
      for (const a of inboxData.awaiting_my_squad.slice(0, 3)) {
        inboxItems.push({
          id: `ask:squad:${a.id}`,
          kind: "ask",
          label: a.question,
          hint: "awaiting",
          hintTone: "info",
          meta: a.target_squad ? `→ ${a.target_squad}` : "no route",
          icon: <Hourglass className="h-3.5 w-3.5" strokeWidth={1.75} />,
          onSelect: () => go("/collaboration#asks"),
        });
      }
      for (const pr of inboxData.my_prs_in_review.slice(0, 3)) {
        inboxItems.push({
          id: `pr:${pr.pr_url}`,
          kind: "pr",
          label: pr.title || pr.pr_url,
          hint: "review",
          hintTone: "review",
          meta: pr.pr_number ? `#${pr.pr_number}` : pr.pr_url,
          icon: <GitPullRequest className="h-3.5 w-3.5" strokeWidth={1.75} />,
          onSelect: () => {
            window.open(pr.pr_url, "_blank", "noreferrer");
            close();
          },
        });
      }
      for (const t of inboxData.open_work.slice(0, 3)) {
        inboxItems.push({
          id: `task:open:${t.id}`,
          kind: "task",
          label: t.title,
          hint: t.state,
          hintTone: "ok",
          meta: t.jira_key ?? "open",
          icon: <Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />,
          onSelect: () => go(`/tasks/${t.id}`),
        });
      }
    }

    const chatItems: PaletteItem[] = (convs.data ?? [])
      .filter((c) => !c.archived)
      .slice(0, 8)
      .map((c) => ({
        id: `chat:${c.id}`,
        kind: "chat",
        label: c.title || "Untitled chat",
        meta:
          c.last_message_preview?.slice(0, 60) ??
          `${c.message_count} messages`,
        icon: <MessageSquare className="h-3.5 w-3.5" strokeWidth={1.75} />,
        onSelect: () => go(`/query?conversation=${c.id}`),
      }));

    return [
      { id: "actions", title: "Actions", items: actions },
      { id: "inbox", title: "My inbox", items: inboxItems },
      { id: "chats", title: "Recent chats", items: chatItems },
      { id: "pages", title: "Jump to", items: pages },
    ];
  }, [inbox.data, convs.data, navigate]);

  // Apply the filter — keep section structure but drop empty sections.
  const filtered = useMemo<PaletteSection[]>(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sections.filter((s) => s.items.length > 0);
    const match = (item: PaletteItem) => {
      const hay = `${item.label} ${item.meta ?? ""} ${item.hint ?? ""}`.toLowerCase();
      return hay.includes(q);
    };
    return sections
      .map((s) => ({ ...s, items: s.items.filter(match) }))
      .filter((s) => s.items.length > 0);
  }, [sections, query]);

  // Flatten to determine the ↑/↓ index target.
  const flatItems = useMemo(
    () => filtered.flatMap((s) => s.items),
    [filtered],
  );

  // Clamp selection on filter change. Guarded so it only fires when the
  // filtered list actually shrank past the cursor; deriving in render here
  // would require restructuring the keyboard-nav state.
  useEffect(() => {
    if (selected >= flatItems.length) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSelected(0);
    }
  }, [flatItems.length, selected]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, Math.max(0, flatItems.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      flatItems[selected]?.onSelect();
    }
  };

  if (!open) return null;

  let runningIndex = -1;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 px-4 pt-[12vh]"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-[640px] overflow-hidden rounded-xl border border-border bg-bg-1 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search bar */}
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <Command className="h-4 w-4 text-fg-3" strokeWidth={1.75} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Jump to a page, task, ask, or chat…"
            className="flex-1 bg-transparent text-[14px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
          />
          <kbd className="kbd">esc</kbd>
        </div>

        {/* Results — scroll if long. Empty state if filter clears all. */}
        <div className="max-h-[60vh] overflow-y-auto py-1">
          {filtered.length === 0 ? (
            <div className="px-4 py-8 text-center text-[12.5px] text-fg-3">
              No matches. Try a different keyword.
            </div>
          ) : (
            filtered.map((section) => (
              <div key={section.id} className="py-1">
                <div className="px-4 py-1 font-mono text-[10px] uppercase tracking-wider text-fg-3">
                  {section.title}
                </div>
                <ul>
                  {section.items.map((item) => {
                    runningIndex += 1;
                    const isSelected = runningIndex === selected;
                    const idx = runningIndex;
                    return (
                      <li key={item.id}>
                        <button
                          type="button"
                          onMouseEnter={() => setSelected(idx)}
                          onClick={() => item.onSelect()}
                          className={cn(
                            "flex w-full items-center gap-2.5 px-4 py-2 text-left transition-colors",
                            isSelected
                              ? "bg-bg-2 text-fg-0"
                              : "text-fg-1 hover:bg-bg-2",
                          )}
                        >
                          <span
                            className={cn(
                              "flex h-6 w-6 shrink-0 items-center justify-center rounded-md ring-1",
                              isSelected
                                ? "bg-accent/15 text-accent ring-accent/30"
                                : "bg-bg-2 text-fg-2 ring-border",
                            )}
                          >
                            {item.icon}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
                            {item.label}
                          </span>
                          {item.hint && (
                            <span
                              className={cn(
                                "shrink-0 font-mono text-[10px] uppercase tracking-wider",
                                HINT_TONE_CLASS[item.hintTone ?? "default"],
                              )}
                            >
                              {item.hint}
                            </span>
                          )}
                          {item.meta && (
                            <span className="hidden shrink-0 truncate font-mono text-[10.5px] text-fg-3 sm:inline">
                              {item.meta}
                            </span>
                          )}
                          {isSelected && (
                            <ArrowRight
                              className="h-3 w-3 shrink-0 text-accent"
                              strokeWidth={2}
                            />
                          )}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))
          )}
        </div>

        {/* Footer hint row */}
        <div className="flex items-center justify-between gap-3 border-t border-border bg-bg-2 px-4 py-2 text-[10.5px] text-fg-3">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1">
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd>
              <span>navigate</span>
            </span>
            <span className="flex items-center gap-1">
              <Kbd>↵</Kbd>
              <span>select</span>
            </span>
            <span className="flex items-center gap-1">
              <Kbd>esc</Kbd>
              <span>close</span>
            </span>
          </div>
          <div className="flex items-center gap-1">
            <Kbd>⌘</Kbd>
            <Kbd>K</Kbd>
            <span>toggle</span>
          </div>
        </div>
      </div>
    </div>
  );
}


function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}
