/**
 * IdentityGate — wraps the entire Bifrost app, blocking it from
 * rendering until the operator's identity has been resolved by the
 * configured identity provider (trusted proxy header, verified bearer
 * JWT, or operator env override).
 *
 * State machine:
 *
 *   loading     → "Resolving identity…"
 *   verified    → render {children}
 *   demo        → "demo mode active" panel with retry / continue
 *   unresolved  → no identity resolved; retry
 *   error       → /auth/me itself failed; retry
 *
 * The interactive "Verify identity" button is offered only when
 * /auth/me reports `capabilities.interactive_refresh`; "Retry" just
 * re-resolves through /auth/me.
 *
 * Dismiss flag: once an operator clicks "Continue in demo mode", we
 * set sessionStorage["bifrost_identity_gate_dismissed"] = "true" so the
 * gate is skipped for the rest of the tab.
 */

import { useEffect, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import {
  AuthApiError,
  getMe,
  refreshAuth,
  type Me,
  type Principal,
} from "@/lib/auth-api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type GateState =
  | { kind: "loading" }
  | { kind: "verified"; principal: Principal }
  | { kind: "demo"; principal: Principal; me: Me }
  | { kind: "unresolved"; me: Me }
  | { kind: "error"; message: string; detail?: string };

export const DISMISS_KEY = "bifrost_identity_gate_dismissed";

export function IdentityGate({ children }: { children: ReactNode }) {
  const dismissed =
    typeof window !== "undefined"
    && sessionStorage.getItem(DISMISS_KEY) === "true";

  const [state, setState] = useState<GateState>({ kind: "loading" });

  const run = (step: () => Promise<GateState>) => {
    setState({ kind: "loading" });
    step().then(setState, (err) =>
      setState({ kind: "error", message: String(err) }),
    );
  };

  useEffect(() => {
    if (dismissed) {
      // Background resolve so the IdentityChip still gets a Principal,
      // but don't block children.
      verify().then(
        (s) => {
          if (s.kind === "verified") setState(s);
        },
        () => {
          /* ignore */
        },
      );
      return;
    }
    verify().then(setState, (err) => {
      setState({ kind: "error", message: String(err) });
    });
    // Only the buttons re-trigger resolution; effect deps intentionally empty.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (dismissed || state.kind === "verified") {
    return <>{children}</>;
  }

  if (state.kind === "loading") {
    return (
      <GateShell>
        <LoadingBody />
      </GateShell>
    );
  }

  if (state.kind === "error") {
    return (
      <GateShell>
        <ProblemBody
          badge="resolve failed"
          title="Couldn't resolve your identity"
          message={state.message}
          detail={state.detail}
          interactive={false}
          onRetry={() => run(verify)}
          onVerify={() => undefined}
        />
      </GateShell>
    );
  }

  const me = state.me;
  const interactive = me.capabilities.interactive_refresh;
  if (state.kind === "demo") {
    return (
      <GateShell>
        <ProblemBody
          badge="demo mode"
          badgeTone="warn"
          title="Demo mode active"
          message={`No operator identity was resolved (provider: ${me.provider || "unknown"}). All actions will be attributed to ${state.principal.email}.`}
          detail="Safe for read-only browsing; not safe for actions that write to Jira, Confluence, or GitHub."
          interactive={interactive}
          onRetry={() => run(verify)}
          onVerify={() => run(interactiveVerify)}
          onContinue={() => {
            sessionStorage.setItem(DISMISS_KEY, "true");
            window.location.reload();
          }}
        />
      </GateShell>
    );
  }

  return (
    <GateShell>
      <ProblemBody
        badge="no identity"
        title="No identity resolved"
        message={`The ${me.provider || "configured"} identity provider did not resolve a user for this request.`}
        interactive={interactive}
        onRetry={() => run(verify)}
        onVerify={() => run(interactiveVerify)}
      />
    </GateShell>
  );
}

async function verify(): Promise<GateState> {
  try {
    const me = await getMe();
    if (me.principal === null) {
      return { kind: "unresolved", me };
    }
    if (me.principal.source === "demo_fallback") {
      return { kind: "demo", principal: me.principal, me };
    }
    return { kind: "verified", principal: me.principal };
  } catch (err) {
    if (err instanceof AuthApiError) {
      return {
        kind: "error",
        message: `GET /auth/me failed (${err.status})`,
        detail:
          typeof err.body === "string"
            ? err.body
            : JSON.stringify(err.body),
      };
    }
    return { kind: "error", message: String(err) };
  }
}

async function interactiveVerify(): Promise<GateState> {
  const resp = await refreshAuth();
  if (resp.kind === "verified") {
    return { kind: "verified", principal: resp.principal };
  }
  if (resp.kind === "demo_fallback") {
    return verify();
  }
  return { kind: "error", message: resp.error, detail: resp.detail };
}

// ── Sub-views ────────────────────────────────────────────────────────

function GateShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md p-6">{children}</Card>
    </div>
  );
}

function LoadingBody() {
  return (
    <div className="flex flex-col items-center gap-4 py-6 text-center">
      <Loader2 className="h-7 w-7 animate-spin text-primary" />
      <div className="text-base font-semibold text-foreground">
        Resolving identity…
      </div>
    </div>
  );
}

function ProblemBody({
  badge,
  badgeTone = "err",
  title,
  message,
  detail,
  interactive,
  onRetry,
  onVerify,
  onContinue,
}: {
  badge: string;
  badgeTone?: "warn" | "err";
  title: string;
  message: string;
  detail?: string;
  interactive: boolean;
  onRetry: () => void;
  onVerify: () => void;
  onContinue?: () => void;
}) {
  const tone =
    badgeTone === "warn"
      ? "bg-yellow-500/15 text-yellow-600 dark:text-yellow-400"
      : "bg-destructive/15 text-destructive";
  return (
    <div className="space-y-4">
      <div>
        <span
          className={`inline-block rounded px-2 py-0.5 font-mono text-xs uppercase tracking-wider ${tone}`}
        >
          {badge}
        </span>
        <h2 className="mt-2 text-base font-semibold text-foreground">{title}</h2>
      </div>
      <div className="space-y-2 text-sm leading-relaxed text-muted-foreground">
        <div className="whitespace-pre-wrap">{message}</div>
        {detail && (
          <div className="whitespace-pre-wrap rounded border border-border bg-muted p-2 font-mono text-xs text-foreground">
            {detail}
          </div>
        )}
      </div>
      <div className="flex gap-2">
        <Button variant="default" onClick={onRetry}>
          Retry
        </Button>
        {interactive && (
          <Button variant="default" onClick={onVerify}>
            Verify identity
          </Button>
        )}
        {onContinue && (
          <Button variant="ghost" onClick={onContinue}>
            Continue in demo mode
          </Button>
        )}
      </div>
    </div>
  );
}
