/**
 * Auth client for /auth/me, /auth/refresh and /auth/logout.
 *
 * These endpoints return raw Principal JSON (NOT wrapped in
 * BifrostEnvelope<T>) so they're isolated from the main api.ts request()
 * helper which assumes envelope unwrapping.
 */

import { csrfHeaders } from "./csrf";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export interface Principal {
  email: string;
  groups: string[];
  /** "member" or "admin"; every authenticated principal is a member. */
  roles?: string[];
  /** "sso_header" | "oidc_jwt" | "env_override" | "demo_fallback" | "api_token" | "" */
  source: string;
  /** ISO 8601 timestamp; absent unless the source carries an expiry. */
  expires_at?: string;
}

export interface IdentityCapabilities {
  /** True when the provider supports an interactive re-verification. */
  interactive_refresh: boolean;
}

/** Result of GET /auth/me: the principal (null on 401) plus the active
 * provider and what it can do. */
export interface Me {
  principal: Principal | null;
  provider: string;
  capabilities: IdentityCapabilities;
}

export class AuthApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.name = "AuthApiError";
    this.status = status;
    this.body = body;
  }
}

async function fetchJson<T>(
  path: string,
  init?: RequestInit,
): Promise<{ status: number; body: T | null; raw: unknown }> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: init?.method ?? "GET",
    credentials: "include",                                     // keep bifrost_session cookie
    ...init,
    headers: { "Content-Type": "application/json", ...csrfHeaders(), ...init?.headers },
  });
  const text = await res.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    /* leave as text */
  }
  return { status: res.status, body: body as T | null, raw: body };
}

interface MeBody extends Partial<Principal> {
  provider?: string;
  capabilities?: Partial<IdentityCapabilities>;
}

/** GET /auth/me — never throws on 401; returns `principal: null` instead
 * so the gate component can branch on "no principal" without try/catch. */
export async function getMe(): Promise<Me> {
  const { status, body } = await fetchJson<MeBody>("/auth/me");
  if ((status === 200 && body && body.email !== undefined) || status === 401) {
    const provider = body?.provider ?? "";
    const capabilities = {
      interactive_refresh: body?.capabilities?.interactive_refresh === true,
    };
    if (status === 401) {
      return { principal: null, provider, capabilities };
    }
    return { principal: body as Principal, provider, capabilities };
  }
  throw new AuthApiError(status, body, `GET /auth/me failed (${status})`);
}

/** Discriminated-union response for POST /auth/refresh. */
export type RefreshResponse =
  | { kind: "verified"; principal: Principal }
  | { kind: "demo_fallback"; principal: Principal }
  | { kind: "error"; error: string; detail?: string };

/** POST /auth/refresh — interactive verify; only meaningful when the
 * provider reports `capabilities.interactive_refresh`. */
export async function refreshAuth(): Promise<RefreshResponse> {
  const { status, body } = await fetchJson<
    Principal & { error?: string; detail?: string }
  >("/auth/refresh", { method: "POST" });

  if (status === 200 && body && body.email !== undefined) {
    return body.source === "demo_fallback"
      ? { kind: "demo_fallback", principal: body }
      : { kind: "verified", principal: body };
  }
  return {
    kind: "error",
    error: body?.error ?? `Unexpected status ${status}`,
    detail: body?.detail,
  };
}

/** POST /auth/logout — returns the principal the provider still
 * resolves (providers keep no server-side session). */
export async function logout(): Promise<Principal> {
  const { status, body } = await fetchJson<Principal>("/auth/logout", {
    method: "POST",
  });
  if (status === 200 && body) {
    return body;
  }
  throw new AuthApiError(status, body, `POST /auth/logout failed (${status})`);
}
