import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { describeError } from "./utils";

describe("describeError on a role refusal", () => {
  it("shows the server's role message instead of the raw JSON body", () => {
    const body = {
      error: {
        code: "auth.role_required",
        message: "this action requires the admin role",
        retryable: false,
      },
    };
    const err = new ApiError(403, body, `API 403: ${JSON.stringify(body)}`);
    expect(describeError(err)).toBe("this action requires the admin role");
  });

  it("leaves other 403 bodies on the generic path", () => {
    const err = new ApiError(403, { error: "csrf check failed" }, "API 403: csrf");
    expect(describeError(err)).toBe("API 403: csrf");
  });
});
