import { describe, expect, it } from "vitest";
import { resolveChannelFromOwnership } from "./chat-actions-panel";

describe("resolveChannelFromOwnership", () => {
  it("returns null when no ownership result has a channel (no guessing)", () => {
    // Bug 1 root cause: we used to guess `<repo>-collab`. Now: empty.
    expect(
      resolveChannelFromOwnership(
        [{ repo_name: "webhook-api", slack_channel: null }],
        "webhook-api",
      ),
    ).toBeNull();
  });

  it("returns null for empty results", () => {
    expect(resolveChannelFromOwnership([], "anything")).toBeNull();
  });

  it("uses the real RepoOwnership.slack_channel, not a slug", () => {
    const out = resolveChannelFromOwnership(
      [{ repo_name: "webhook-api", slack_channel: "#dlvr-core" }],
      "webhook-api",
    );
    expect(out).toEqual({ channel: "dlvr-core", fromRepo: "webhook-api" });
  });

  it("strips a leading # from the stored channel", () => {
    const out = resolveChannelFromOwnership(
      [{ repo_name: "r", slack_channel: "#eng-handovers" }],
      "r",
    );
    expect(out?.channel).toBe("eng-handovers");
  });

  it("prefers the exact repo-name match (case-insensitive) over others", () => {
    const out = resolveChannelFromOwnership(
      [
        { repo_name: "other-repo", slack_channel: "#wrong" },
        { repo_name: "Webhook-Api", slack_channel: "#right" },
      ],
      "webhook-api",
    );
    expect(out).toEqual({ channel: "right", fromRepo: "Webhook-Api" });
  });

  it("falls back to the first channel-bearing result when no exact match", () => {
    const out = resolveChannelFromOwnership(
      [
        { repo_name: "a", slack_channel: null },
        { repo_name: "b", slack_channel: "#b-channel" },
      ],
      "nonexistent",
    );
    expect(out).toEqual({ channel: "b-channel", fromRepo: "b" });
  });

  it("skips an exact match with no channel and uses a channel-bearing peer", () => {
    const out = resolveChannelFromOwnership(
      [
        { repo_name: "target", slack_channel: null },
        { repo_name: "peer", slack_channel: "#peer-ch" },
      ],
      "target",
    );
    expect(out).toEqual({ channel: "peer-ch", fromRepo: "peer" });
  });
});
