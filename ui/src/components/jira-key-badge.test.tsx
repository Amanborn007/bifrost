/**
 * A Jira key links out only when the API supplies a URL (sources.jira.base_url
 * is configured); without one it is plain text, never a guessed link.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { JiraKeyBadge } from "./jira-key-badge";

describe("JiraKeyBadge", () => {
  it("renders an anchor to the url when one is provided", () => {
    render(<JiraKeyBadge jiraKey="ABC-1" url="https://jira.example.com/browse/ABC-1" />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("https://jira.example.com/browse/ABC-1");
    expect(link.textContent).toContain("ABC-1");
  });

  it("opens the link in a new tab without leaking the opener", () => {
    render(<JiraKeyBadge jiraKey="ABC-1" url="https://jira.example.com/browse/ABC-1" />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("renders plain text and no anchor when the url is null", () => {
    render(<JiraKeyBadge jiraKey="ABC-1" url={null} />);
    expect(screen.queryByRole("link")).toBeNull();
    const text = screen.getByText("ABC-1");
    expect(text.tagName).toBe("SPAN");
  });

  it("treats an empty url like a missing one", () => {
    render(<JiraKeyBadge jiraKey="ABC-1" url="" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("ABC-1")).toBeTruthy();
  });
});
