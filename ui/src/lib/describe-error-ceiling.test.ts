import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { describeError } from "./utils";

describe("describeError on a walk breadth ceiling refusal", () => {
  it("shows the server message instead of the raw JSON body", () => {
    const body = {
      error: {
        code: "settings.walk_breadth_above_ceiling",
        message: "walk_breadth_soft_cap 500 is above the server ceiling of 200",
        retryable: false,
      },
    };
    const err = new ApiError(400, body, `API 400: ${JSON.stringify(body)}`);
    expect(describeError(err)).toBe("walk_breadth_soft_cap 500 is above the server ceiling of 200");
  });
});
