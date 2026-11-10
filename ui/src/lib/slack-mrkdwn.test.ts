import { describe, expect, it } from "vitest";
import { toSlackMrkdwn } from "./slack-mrkdwn";

describe("toSlackMrkdwn", () => {
  it("converts **bold** to Slack single-asterisk bold", () => {
    expect(toSlackMrkdwn("This is **important** text")).toBe(
      "This is *important* text",
    );
  });

  it("converts __bold__ to single-asterisk bold", () => {
    expect(toSlackMrkdwn("__strong__")).toBe("*strong*");
  });

  it("keeps inline `code` verbatim (Slack supports backticks)", () => {
    expect(toSlackMrkdwn("call `getUser()` first")).toBe(
      "call `getUser()` first",
    );
  });

  it("never rewrites markdown inside code spans", () => {
    // **bold** and [^1] inside backticks must survive untouched.
    expect(toSlackMrkdwn("use `arr[^1]` and `a**b`")).toBe(
      "use `arr[^1]` and `a**b`",
    );
  });

  it("keeps fenced code blocks verbatim", () => {
    const md = "```\nconst x = **5**;\n```";
    expect(toSlackMrkdwn(md)).toBe(md);
  });

  it("turns ### headings into a bold line (Slack has no headings)", () => {
    expect(toSlackMrkdwn("### Summary")).toBe("*Summary*");
    expect(toSlackMrkdwn("# Title")).toBe("*Title*");
    expect(toSlackMrkdwn("###### Deep")).toBe("*Deep*");
  });

  it("converts [label](url) links to Slack <url|label>", () => {
    expect(toSlackMrkdwn("see [the docs](https://x.com/d)")).toBe(
      "see <https://x.com/d|the docs>",
    );
  });

  it("converts ~~strike~~ to single-tilde", () => {
    expect(toSlackMrkdwn("~~gone~~")).toBe("~gone~");
  });

  it("normalizes bullet markers to •", () => {
    expect(toSlackMrkdwn("- one\n- two")).toBe("• one\n• two");
    expect(toSlackMrkdwn("* star\n+ plus")).toBe("• star\n• plus");
  });

  it("inlines [^n] footnote markers as [n] and appends a Sources list", () => {
    const md =
      "The API is rate-limited [^1] and cached [^2].\n\n" +
      "[^1]: src/api.py:40\n" +
      "[^2]: src/cache.py:12";
    const out = toSlackMrkdwn(md);
    expect(out).toContain("rate-limited [1] and cached [2]");
    expect(out).toContain("*Sources:*");
    expect(out).toContain("[1] src/api.py:40");
    expect(out).toContain("[2] src/cache.py:12");
    // The definition lines must not leak into the body verbatim.
    expect(out).not.toMatch(/\[\^1\]:/);
  });

  it("handles a realistic mixed Bifrost answer end-to-end", () => {
    const md = [
      "### Findings",
      "",
      "The **OrderService** calls `placeOrder()` [^1].",
      "",
      "- It is **regulated**",
      "- See [runbook](https://wiki/rb)",
      "",
      "[^1]: services/orders.py:88",
    ].join("\n");
    const out = toSlackMrkdwn(md);
    expect(out).toContain("*Findings*");
    expect(out).toContain("*OrderService*");
    expect(out).toContain("`placeOrder()`");
    expect(out).toContain("calls `placeOrder()` [1]");
    expect(out).toContain("• It is *regulated*");
    expect(out).toContain("<https://wiki/rb|runbook>");
    expect(out).toContain("*Sources:*\n[1] services/orders.py:88");
  });

  it("returns empty string for empty input", () => {
    expect(toSlackMrkdwn("")).toBe("");
  });

  it("collapses 3+ blank lines to 2", () => {
    expect(toSlackMrkdwn("a\n\n\n\nb")).toBe("a\n\nb");
  });
});
