import { useQuery } from "@tanstack/react-query";
import { getClaudeCliStatus } from "@/lib/api";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Compact auth indicator used at the top of the main content area.
 * Collapsed when everything is OK (small pill); expanded into a warning bar
 * only when Claude CLI is not authenticated.
 */
export function ClaudeCliBanner() {
  const status = useQuery({
    queryKey: ["claude-cli-status"],
    queryFn: getClaudeCliStatus,
    // Auth state changes rarely but a backend restart or login lapse
    // is something the user wants surfaced quickly. 15s refetch +
    // 5s stale window means the banner clears within ~20s of `claude
    // login` succeeding.
    refetchInterval: 15_000,
    staleTime: 5_000,
    refetchOnWindowFocus: true,
    // A failing request (network blip, restart) shouldn't pin a
    // permanent "not authenticated" banner — retry quickly instead.
    retry: 2,
    retryDelay: 1000,
  });
  const s = status.data;

  // Two reasons we don't show the banner:
  //  - No data yet (initial mount, before the first response).
  //  - Last fetch errored (network down) — we don't actually KNOW
  //    the auth state, so we shouldn't pin a misleading red banner.
  if (!s || status.isError) return null;

  if (!s.ok) {
    return (
      <div
        className="flex items-center gap-3 border-b border-err-soft bg-err-soft px-6 py-2.5"
        role="alert"
      >
        <AlertTriangle className="h-4 w-4 shrink-0 text-err" strokeWidth={1.75} />
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-medium text-err">
            Claude CLI is not authenticated
          </p>
          <p className="truncate text-[12px] text-fg-1">
            {s.hint ?? "Run `claude auth login` in a terminal, then reload."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex items-center gap-2 border-b border-border bg-bg-0 px-6 py-1.5",
        "text-[11.5px] text-fg-2",
      )}
    >
      <CheckCircle2 className="h-3.5 w-3.5 text-ok" strokeWidth={1.75} />
      <span>
        Signed in as <span className="font-medium text-fg-1">{s.email}</span>
      </span>
      {s.org_name && (
        <>
          <Sep />
          <span>{s.org_name}</span>
        </>
      )}
      {s.subscription_type && (
        <>
          <Sep />
          <span className="rounded-full bg-bg-2 px-1.5 py-[1px] text-[10px] font-medium text-fg-1">
            {s.subscription_type}
          </span>
        </>
      )}
      {s.extraction_model && (
        <span className="ml-auto hidden items-center gap-1 text-[11px] text-fg-3 md:inline-flex">
          <span className="font-mono">{s.extraction_model}</span>
        </span>
      )}
    </div>
  );
}

function Sep() {
  return (
    <span aria-hidden className="text-fg-4">
      ·
    </span>
  );
}
