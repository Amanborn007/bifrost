/**
 * Onboarding lens transformer.
 *
 * Pure-function unit tests. We don't render `HandoverDocDetailPage`
 * here because it depends on react-query + react-router; the lens
 * itself is the load-bearing logic and we exercise it directly.
 */
import { describe, it, expect } from "vitest";
import { _diffHandoverBodyLines, applyHandoverLens } from "./collaboration";

const SAMPLE = `---
front: matter
---

Some preamble paragraph the generator emits before any h2.

## Repos in scope

- repo-a
- repo-b

## Executive summary

The high-level pitch.

## Architecture

Boxes and arrows.

### Sub heading should stay nested

Nested content body.

## Critical flows

Flow text.

## Ownership

Owner table.

## Dependencies

Deps text.

## KTLO surface

Toil text.

## Glossary

Term: definition.

## Common questions

- Q1
- Q2

## Collaboration playbook

Playbook text.
`;

describe("applyHandoverLens", () => {
  it("returns input verbatim for the default lens", () => {
    expect(applyHandoverLens(SAMPLE, "default")).toBe(SAMPLE);
  });

  it("re-orders sections for the onboarding lens", () => {
    const out = applyHandoverLens(SAMPLE, "onboarding");
    // Helper: index of each section header in the output.
    const idx = (header: string) => out.indexOf(header);
    // Repos in scope must remain at the top.
    expect(idx("## Repos in scope")).toBeGreaterThan(-1);
    // Onboarding-pinned order: Glossary → Ownership → Architecture →
    // Critical flows → KTLO surface, all before Dependencies (which
    // falls into the "rest" bucket) and the closing Executive summary.
    expect(idx("## Glossary")).toBeLessThan(idx("## Ownership"));
    expect(idx("## Ownership")).toBeLessThan(idx("## Architecture"));
    expect(idx("## Architecture")).toBeLessThan(idx("## Critical flows"));
    expect(idx("## Critical flows")).toBeLessThan(idx("## KTLO surface"));
    expect(idx("## KTLO surface")).toBeLessThan(idx("## Dependencies"));
    expect(idx("## Dependencies")).toBeLessThan(idx("## Executive summary"));
    // Repos in scope still pinned above all the re-shuffled sections.
    expect(idx("## Repos in scope")).toBeLessThan(idx("## Glossary"));
  });

  it("hides Common questions and Collaboration playbook in onboarding", () => {
    const out = applyHandoverLens(SAMPLE, "onboarding");
    expect(out).not.toContain("## Common questions");
    expect(out).not.toContain("## Collaboration playbook");
    // Body text from those sections is gone too.
    expect(out).not.toContain("Playbook text.");
    expect(out).not.toContain("- Q1");
  });

  it("preserves pre-h2 preamble verbatim at the top", () => {
    const out = applyHandoverLens(SAMPLE, "onboarding");
    // The frontmatter and first paragraph appear before any h2.
    const firstH2 = out.indexOf("## ");
    expect(firstH2).toBeGreaterThan(-1);
    const head = out.slice(0, firstH2);
    expect(head).toContain("---\nfront: matter\n---");
    expect(head).toContain("Some preamble paragraph the generator emits");
  });

  it("preserves nested h3+ content within their parent h2", () => {
    const out = applyHandoverLens(SAMPLE, "onboarding");
    // The h3 lives inside Architecture and must travel with it, not
    // become its own top-level section.
    expect(out).toContain("### Sub heading should stay nested");
    expect(out).toContain("Nested content body.");
  });

  it("falls through unchanged when no h2 sections exist", () => {
    const flat = "Just a paragraph with no headers.\n";
    expect(applyHandoverLens(flat, "onboarding")).toBe(flat);
  });

  it("is pure — same input + lens → same output across calls", () => {
    const a = applyHandoverLens(SAMPLE, "onboarding");
    const b = applyHandoverLens(SAMPLE, "onboarding");
    expect(a).toBe(b);
  });
});

/* ─── Body-history diff ──────────────────────────── */

describe("diffHandoverBodyLines", () => {
  it("returns all-equal lines for identical inputs", () => {
    const text = "alpha\nbeta\ngamma";
    const out = _diffHandoverBodyLines(text, text);
    expect(out.every((l) => l.type === "eq")).toBe(true);
    expect(out.map((l) => l.text)).toEqual(["alpha", "beta", "gamma"]);
  });

  it("marks added lines as add", () => {
    const out = _diffHandoverBodyLines("a\nb", "a\nb\nc");
    expect(out).toEqual([
      { type: "eq", text: "a" },
      { type: "eq", text: "b" },
      { type: "add", text: "c" },
    ]);
  });

  it("marks removed lines as del", () => {
    const out = _diffHandoverBodyLines("a\nb\nc", "a\nc");
    expect(out).toEqual([
      { type: "eq", text: "a" },
      { type: "del", text: "b" },
      { type: "eq", text: "c" },
    ]);
  });

  it("interleaves additions and deletions for a swap", () => {
    const out = _diffHandoverBodyLines("a\nx\nb", "a\ny\nb");
    // The replace surfaces as one del + one add (order depends on
    // the LCS walk-back; both are valid, both surface ‘x’ removed
    // and ‘y’ added with ‘a’ and ‘b’ preserved).
    const types = out.map((l) => l.type);
    expect(types).toContain("del");
    expect(types).toContain("add");
    expect(out.find((l) => l.text === "x")?.type).toBe("del");
    expect(out.find((l) => l.text === "y")?.type).toBe("add");
    expect(out.filter((l) => l.text === "a")[0].type).toBe("eq");
    expect(out.filter((l) => l.text === "b")[0].type).toBe("eq");
  });

  it("handles empty old → all-add against new", () => {
    // Note: ``"".split("\n")`` is ``[""]``, so the empty-string
    // line is treated as a removed token from the old text. What we
    // care about: every line of the new text is surfaced as ``add``.
    const out = _diffHandoverBodyLines("", "x\ny");
    const adds = out.filter((l) => l.type === "add").map((l) => l.text);
    expect(adds).toEqual(["x", "y"]);
  });

  it("handles empty new → all-del against old", () => {
    const out = _diffHandoverBodyLines("x\ny", "");
    const dels = out.filter((l) => l.type === "del").map((l) => l.text);
    expect(dels).toEqual(["x", "y"]);
  });
});
