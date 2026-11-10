/**
 * IdentityChip — top-bar widget showing the current operator's email
 * + the identity provider name ("header", "oidc_jwt", "none"). Clicking
 * opens a dropdown with:
 *
 *   • full email + groups
 *   • "Verify identity" button (POST /auth/refresh), shown only when
 *     /auth/me reports `capabilities.interactive_refresh`
 *   • "Sign out" button (POST /auth/logout)
 *
 * Auto-polls /auth/me on mount + every 5 minutes so the chip stays
 * in sync. The polling stops when the page is hidden (visibilitychange)
 * to avoid background fan-out.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, ShieldCheck, ShieldAlert, ChevronDown } from "lucide-react";
import {
  getMe,
  logout as logoutApi,
  refreshAuth,
  type Me,
  type Principal,
} from "@/lib/auth-api";
import { DISMISS_KEY } from "@/components/identity-gate";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

// Badge colors per source — ok = verified identity, info = operator env
// override, err = demo (no real identity).
const SOURCE_CHIP_CLASS: Record<string, string> = {
  sso_header: "badge badge-ok",
  oidc_jwt: "badge badge-ok",
  env_override: "badge badge-info",
  demo_fallback: "badge badge-err",
};


export function IdentityChip({ className }: { className?: string }) {
  const [me, setMe] = useState<Me | null>(null);
  const [open, setOpen] = useState(false);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const refreshTimer = useRef<number | null>(null);

  const loadPrincipal = useCallback(async () => {
    try {
      setMe(await getMe());
    } catch {
      setMe(null);
    }
  }, []);

  useEffect(() => {
    void loadPrincipal();
    const tick = () => {
      if (document.visibilityState === "visible") void loadPrincipal();
    };
    refreshTimer.current = window.setInterval(tick, REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      if (refreshTimer.current !== null) {
        clearInterval(refreshTimer.current);
      }
      document.removeEventListener("visibilitychange", tick);
    };
  }, [loadPrincipal]);

  if (me === null || me.principal === null) {
    return null;                                              // gate handles this
  }

  const principal = me.principal;
  const sourceLabel = me.provider || principal.source;
  const sourceClass =
    SOURCE_CHIP_CLASS[principal.source] ?? "badge badge-neutral";
  const isWarning =
    principal.source === "demo_fallback" || !principal.email;

  return (
    <div className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-2 rounded-lg border border-border bg-background px-2.5 py-1 text-xs font-medium hover:bg-muted"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {isWarning ? (
          <ShieldAlert className="h-3.5 w-3.5 text-err" strokeWidth={1.75} />
        ) : (
          <ShieldCheck
            className="h-3.5 w-3.5 text-ok"
            strokeWidth={1.75}
          />
        )}
        <span className="max-w-[160px] truncate text-foreground">
          {principal.email || "(no identity)"}
        </span>
        <span
          className={cn(
            "font-mono uppercase tracking-wider",
            sourceClass,
          )}
        >
          {sourceLabel}
        </span>
        <ChevronDown
          className={cn(
            "h-3 w-3 text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
          strokeWidth={1.75}
        />
      </button>

      {open && (
        <>
          {/* Click-outside backdrop — invisible, captures the close. */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => setOpen(false)}
            aria-hidden
          />
          <div
            role="menu"
            className="absolute right-0 z-50 mt-1 w-80 rounded-lg border border-border bg-popover p-3 shadow-lg"
          >
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Identity
            </div>
            <div className="mb-1 break-all text-sm font-medium text-foreground">
              {principal.email || "(no identity)"}
            </div>
            <div className="mb-3 flex flex-wrap gap-1">
              <span
                className={cn(
                  "font-mono uppercase tracking-wider",
                  sourceClass,
                )}
              >
                {sourceLabel}
              </span>
              {principal.expires_at && (
                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground ring-1 ring-border">
                  expires {new Date(principal.expires_at).toLocaleString()}
                </span>
              )}
            </div>
            {principal.groups.length > 0 && (
              <div className="mb-3">
                <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  Groups ({principal.groups.length})
                </div>
                <div className="flex flex-wrap gap-1">
                  {principal.groups.slice(0, 12).map((g) => (
                    <span
                      key={g}
                      className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-foreground ring-1 ring-border"
                    >
                      {g}
                    </span>
                  ))}
                  {principal.groups.length > 12 && (
                    <span className="text-[10px] text-muted-foreground">
                      +{principal.groups.length - 12} more
                    </span>
                  )}
                </div>
              </div>
            )}
            {isWarning && (
              <div className="mb-3 rounded bg-err-soft p-2 text-xs text-err">
                You're in demo mode. Actions that write to Jira /
                Confluence / GitHub will be attributed to a placeholder
                — configure an identity provider before triggering writes.
              </div>
            )}
            <div className="flex gap-2">
              {me.capabilities.interactive_refresh && (
                <Button
                  size="sm"
                  variant="default"
                  onClick={() => {
                    setOpen(false);
                    setVerifyOpen(true);
                  }}
                >
                  Verify identity
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={async () => {
                  try {
                    const after = await logoutApi();
                    setMe({ ...me, principal: after.email ? after : null });
                  } finally {
                    sessionStorage.removeItem(DISMISS_KEY);
                    setOpen(false);
                  }
                }}
              >
                Sign out
              </Button>
            </div>
          </div>
        </>
      )}

      {verifyOpen && (
        <VerifyModal
          onClose={() => setVerifyOpen(false)}
          onVerified={(p) => {
            setMe({ ...me, principal: p });
            setVerifyOpen(false);
          }}
        />
      )}
    </div>
  );
}


function VerifyModal({
  onClose,
  onVerified,
}: {
  onClose: () => void;
  onVerified: (p: Principal) => void;
}) {
  const [phase, setPhase] = useState<"running" | "error">("running");
  const [errorMsg, setErrorMsg] = useState<string>("");
  const [errorDetail, setErrorDetail] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await refreshAuth();
        if (cancelled) return;
        if (resp.kind === "verified") {
          onVerified(resp.principal);
          return;
        }
        setPhase("error");
        if (resp.kind === "demo_fallback") {
          setErrorMsg("Verification fell back to the demo identity");
        } else {
          setErrorMsg(resp.error);
          setErrorDetail(resp.detail ?? "");
        }
      } catch (err) {
        if (cancelled) return;
        setPhase("error");
        setErrorMsg(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [onVerified]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-lg rounded-lg border border-border bg-popover p-5 shadow-xl">
        <div className="mb-3 flex items-center gap-2">
          {phase === "running" ? (
            <Loader2 className="h-4 w-4 animate-spin text-primary" strokeWidth={1.75} />
          ) : (
            <ShieldAlert className="h-4 w-4 text-err" strokeWidth={1.75} />
          )}
          <h3 className="text-base font-semibold text-foreground">
            {phase === "running" ? "Verifying identity…" : "Verify failed"}
          </h3>
        </div>
        {phase === "error" && (
          <div className="space-y-3">
            <div className="whitespace-pre-wrap rounded bg-err-soft p-2 font-mono text-xs text-err">
              {errorMsg}
            </div>
            {errorDetail && (
              <div className="whitespace-pre-wrap rounded border border-border bg-muted p-2 font-mono text-xs text-foreground">
                {errorDetail}
              </div>
            )}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={onClose}>
            {phase === "running" ? "Cancel" : "Close"}
          </Button>
        </div>
      </div>
    </div>
  );
}
