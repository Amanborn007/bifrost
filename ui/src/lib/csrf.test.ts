import { afterEach, describe, expect, it, vi } from "vitest";

import { getBlastRadius } from "./api";
import { logout, refreshAuth } from "./auth-api";
import { CSRF_HEADER_NAME, CSRF_HEADER_VALUE, csrfHeaders } from "./csrf";

function stubFetch(body: unknown) {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
    json: async () => body,
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentHeaders(fetchMock: ReturnType<typeof stubFetch>): Record<string, string> {
  const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
  return calls[0][1].headers as Record<string, string>;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("csrf header", () => {
  it("is the value the server middleware requires", () => {
    expect(CSRF_HEADER_NAME).toBe("X-Requested-With");
    expect(CSRF_HEADER_VALUE).toBe("bifrost-ui");
    expect(csrfHeaders()).toEqual({ "X-Requested-With": "bifrost-ui" });
  });

  it("is sent by the main api request wrapper", async () => {
    const fetchMock = stubFetch({ data: { impacts: [] } });
    await getBlastRadius("urn:x");
    expect(sentHeaders(fetchMock)["X-Requested-With"]).toBe("bifrost-ui");
  });

  it("is sent by the auth client on POSTs", async () => {
    const fetchMock = stubFetch({ email: "a@example.com", groups: [], source: "header" });
    await logout();
    expect(sentHeaders(fetchMock)["X-Requested-With"]).toBe("bifrost-ui");

    const refreshMock = stubFetch({ email: "a@example.com", groups: [], source: "oidc_jwt" });
    await refreshAuth();
    expect(sentHeaders(refreshMock)["X-Requested-With"]).toBe("bifrost-ui");
  });
});
