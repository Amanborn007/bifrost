import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { Me } from "@/lib/auth-api";
import { IdentityGate } from "./identity-gate";

const getMe = vi.fn<() => Promise<Me>>();
const refreshAuth = vi.fn();

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return { ...actual, getMe: () => getMe(), refreshAuth: () => refreshAuth() };
});

const verified = (interactive: boolean): Me => ({
  principal: { email: "a@example.com", groups: [], source: "header" },
  provider: "header",
  capabilities: { interactive_refresh: interactive },
});

const demo = (interactive: boolean): Me => ({
  principal: { email: "demo@example.com", groups: [], source: "demo_fallback" },
  provider: "none",
  capabilities: { interactive_refresh: interactive },
});

const unresolved = (interactive: boolean): Me => ({
  principal: null,
  provider: "oidc_jwt",
  capabilities: { interactive_refresh: interactive },
});

const VERIFY = /verify identity/i;

beforeEach(() => {
  sessionStorage.clear();
  getMe.mockReset();
  refreshAuth.mockReset();
});

describe("IdentityGate", () => {
  it("renders children once a real principal resolves", async () => {
    getMe.mockResolvedValue(verified(false));
    render(<IdentityGate><div>app body</div></IdentityGate>);
    expect(await screen.findByText("app body")).toBeInTheDocument();
  });

  it.each([
    ["demo", demo],
    ["unresolved", unresolved],
  ])("hides the verify button for %s when the provider is not interactive", async (_n, make) => {
    getMe.mockResolvedValue(make(false));
    render(<IdentityGate><div>app body</div></IdentityGate>);
    await screen.findByRole("button", { name: /retry/i });
    expect(screen.queryByRole("button", { name: VERIFY })).toBeNull();
    expect(screen.queryByText("app body")).toBeNull();
  });

  it.each([
    ["demo", demo],
    ["unresolved", unresolved],
  ])("shows the verify button for %s when interactive_refresh is true", async (_n, make) => {
    getMe.mockResolvedValue(make(true));
    refreshAuth.mockResolvedValue({ kind: "verified", principal: verified(true).principal });
    render(<IdentityGate><div>app body</div></IdentityGate>);
    fireEvent.click(await screen.findByRole("button", { name: VERIFY }));
    await waitFor(() => expect(refreshAuth).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("app body")).toBeInTheDocument();
  });

  it("retry re-resolves through /auth/me, not the interactive flow", async () => {
    getMe.mockResolvedValueOnce(unresolved(false)).mockResolvedValueOnce(verified(false));
    render(<IdentityGate><div>app body</div></IdentityGate>);
    fireEvent.click(await screen.findByRole("button", { name: /retry/i }));
    expect(await screen.findByText("app body")).toBeInTheDocument();
    expect(refreshAuth).not.toHaveBeenCalled();
  });

  it("names the provider", async () => {
    getMe.mockResolvedValue(unresolved(false));
    const { container } = render(<IdentityGate><div>app body</div></IdentityGate>);
    await screen.findByRole("button", { name: /retry/i });
    expect(container.textContent).toMatch(/oidc_jwt/);
  });
});
