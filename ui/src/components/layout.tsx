import { useState } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/sonner";
import { cn } from "@/lib/utils";
import { ClaudeCliBanner } from "@/components/claude-cli-banner";
import { IdentityChip } from "@/components/identity-chip";
import { CommandPalette } from "@/components/command-palette";
import { getInbox } from "@/lib/api";
import {
  ClipboardList,
  LayoutDashboard,
  MessageSquare,
  Receipt,
  BookOpen,
  PanelLeftClose,
  PanelLeft,
  Activity,
  Users,
  Search,
} from "lucide-react";

/**
 * Studio-style shell. Narrow sidebar with icon+label nav, subtle active
 * state (no cyberpunk glow), clean 6-8px radii, calm slate surfaces.
 */

// Chat is the product — it's the first item and the root route.
// Knowledge = unified repo-control + entity browser (the old Entities page
// was folded in here so users don't have to toggle between two views of
// the same data).
//
// ``badge`` resolves a per-nav count from the inbox aggregator so the
// operator can see at-a-glance where their attention is needed without
// opening each page. ``tone`` matches the inbox section's tone so the
// badge in the sidebar looks like the section pill on the page.
type InboxCounts = {
  total_actionable: number;
  escalated: number;
  blocked: number;
  awaiting_me: number;
  in_review: number;
  open_work: number;
};

type BadgeTone = "ok" | "warn" | "err" | "info" | "review" | "default";

const NAV: ReadonlyArray<{
  to: string;
  label: string;
  icon: typeof LayoutDashboard;
  badge?: (c: InboxCounts) => { value: number; tone: BadgeTone } | null;
}> = [
  {
    to: "/overview",
    label: "Overview",
    icon: LayoutDashboard,
    badge: (c) =>
      c.total_actionable > 0
        ? { value: c.total_actionable, tone: "default" }
        : null,
  },
  { to: "/query", label: "Chat", icon: MessageSquare },
  {
    to: "/tasks",
    label: "Tasks",
    icon: ClipboardList,
    badge: (c) => {
      // Blocked = highest urgency; show that. Fall back to open_work.
      if (c.blocked > 0) return { value: c.blocked, tone: "warn" };
      if (c.open_work > 0) return { value: c.open_work, tone: "ok" };
      return null;
    },
  },
  {
    to: "/collaboration",
    label: "Collaboration",
    icon: Users,
    badge: (c) => {
      // Escalated trumps everything; otherwise sum the squad-routed work.
      if (c.escalated > 0) return { value: c.escalated, tone: "err" };
      if (c.awaiting_me > 0) return { value: c.awaiting_me, tone: "info" };
      return null;
    },
  },
  { to: "/knowledge", label: "Knowledge", icon: BookOpen },
  { to: "/costs", label: "Spend", icon: Receipt },
  { to: "/admin/learning", label: "Learning", icon: Activity },
];

// Sidebar badges use the same tone vocabulary as the rest of the app
// — single soft-tint pill primitive, no per-component colour drift.
const BADGE_TONE_CLASS: Record<BadgeTone, string> = {
  ok: "badge-ok",
  warn: "badge-warn",
  err: "badge-err",
  info: "badge-info",
  review: "badge-review",
  default: "badge-neutral",
};

export function Layout() {
  const loc = useLocation();
  const [collapsed, setCollapsed] = useState(false);

  // Single inbox fetch shared with InboxPanel via TanStack's dedupe
  // (same queryKey ⇒ one network call). 30s refetch keeps badges live
  // without battering the server. Failures are silent — badges just
  // disappear, the rest of the UI keeps working.
  const inbox = useQuery({
    queryKey: ["inbox", "me"],
    queryFn: () => getInbox(),
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const counts: InboxCounts | null = inbox.data?.counts ?? null;

  return (
    <div className="flex h-screen overflow-hidden bg-bg-0 text-fg-0">
      <aside
        className={cn(
          "flex shrink-0 flex-col border-r border-border bg-bg-1 transition-[width] duration-150",
          collapsed ? "w-[60px]" : "w-[220px]",
        )}
      >
        {/* Wordmark */}
        <Link
          to="/"
          className="flex h-14 items-center gap-3 border-b border-border px-4"
        >
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-accent text-accent-fg">
            <span className="text-[13px] font-bold leading-none">B</span>
          </div>
          {!collapsed && (
            <span className="text-[15px] font-semibold tracking-tight text-fg-0">
              Bifrost
            </span>
          )}
        </Link>

        {/* Nav */}
        <nav className="flex-1 px-2 py-3">
          <ul className="space-y-0.5">
            {NAV.map(({ to, label, icon: Icon, badge }) => {
              // ``/overview`` is the new landing route, so ``/`` and
              // ``/overview`` should both highlight Overview. Other
              // routes use prefix-startswith.
              const active =
                to === "/overview"
                  ? loc.pathname === "/" || loc.pathname.startsWith("/overview")
                  : loc.pathname.startsWith(to);
              const badgeData = counts && badge ? badge(counts) : null;
              return (
                <li key={to}>
                  <Link
                    to={to}
                    className={cn(
                      "relative flex items-center gap-3 rounded-md px-2.5 py-2 text-[13.5px] font-medium transition-colors",
                      active
                        ? "bg-bg-3 text-fg-0"
                        : "text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                    )}
                    title={
                      collapsed
                        ? badgeData
                          ? `${label} (${badgeData.value})`
                          : label
                        : undefined
                    }
                  >
                    <Icon
                      className={cn(
                        "h-4 w-4 shrink-0",
                        active ? "text-accent" : "text-fg-2",
                      )}
                      strokeWidth={1.75}
                    />
                    {!collapsed && <span className="flex-1">{label}</span>}
                    {badgeData && !collapsed && (
                      <span
                        className={cn(
                          "badge shrink-0 rounded-full font-mono",
                          BADGE_TONE_CLASS[badgeData.tone],
                        )}
                      >
                        {badgeData.value > 99 ? "99+" : badgeData.value}
                      </span>
                    )}
                    {/* Collapsed mode: solid 6×6 dot in the corner —
                        same tone as the expanded badge so the visual
                        cue carries across collapse states. */}
                    {badgeData && collapsed && (
                      <span
                        className={cn(
                          "dot absolute right-2 top-2",
                          badgeData.tone === "err" && "bg-err",
                          badgeData.tone === "warn" && "bg-warn",
                          badgeData.tone === "info" && "bg-info",
                          badgeData.tone === "review" && "bg-review",
                          badgeData.tone === "ok" && "bg-ok",
                          badgeData.tone === "default" && "bg-accent",
                        )}
                      />
                    )}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Collapse control */}
        <div className="border-t border-border p-2">
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="flex w-full items-center justify-center gap-2 rounded-md px-2 py-1.5 text-[12px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title={collapsed ? "Expand" : "Collapse"}
          >
            {collapsed ? (
              <PanelLeft className="h-3.5 w-3.5" strokeWidth={1.75} />
            ) : (
              <>
                <PanelLeftClose className="h-3.5 w-3.5" strokeWidth={1.75} />
                <span>Collapse</span>
              </>
            )}
          </button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        {/* Top bar — Claude CLI banner on the left (full-width
            informational), IdentityChip pinned right so the
            operator can always see who they're acting as + verify or
            sign out from anywhere in the app. */}
        {/* Glass top bar — subtle backdrop-blur so any future
            scroll-under interactions read as "lightweight" rather
            than "fixed slab". ``.glass`` primitive lives in
            ``index.css``. */}
        <div className="glass flex items-center justify-between gap-3 border-b border-border px-4 py-1.5">
          <div className="min-w-0 flex-1">
            <ClaudeCliBanner />
          </div>
          {/* ⌘K search trigger — discoverable affordance for the
              global command palette. Dispatches a custom event the
              palette listens for so we don't have to lift state. */}
          <button
            type="button"
            onClick={() =>
              window.dispatchEvent(new Event("bifrost:palette:open"))
            }
            className="hidden items-center gap-2 rounded-md border border-border bg-bg-2 px-2.5 py-1 text-[12px] text-fg-2 hover:bg-bg-3 hover:text-fg-0 md:inline-flex"
            title="Open command palette"
          >
            <Search className="h-3.5 w-3.5" strokeWidth={1.75} />
            <span>Search</span>
            <kbd className="kbd">⌘K</kbd>
          </button>
          <IdentityChip />
        </div>
        <div className="flex-1 overflow-y-auto">
          {/* No max-width here — pages control their own container. */}
          <Outlet />
        </div>
      </main>

      <CommandPalette />
      <Toaster />
    </div>
  );
}
