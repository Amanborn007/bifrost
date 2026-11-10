import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import mermaid from "mermaid";
import { beforeAll, describe, expect, it } from "vitest";

// The examples live in the Python prompt source; a regex over the file is simpler
// than generating a fixture and cannot go stale.
const SOURCE = resolve(__dirname, "../../../src/bifrost/research/diagram_standards.py");
const FENCE = /```mermaid\n([\s\S]*?)```/g;

const blocks = Array.from(readFileSync(SOURCE, "utf8").matchAll(FENCE), (m) => m[1]);

describe("diagram_standards.py mermaid examples", () => {
  beforeAll(() => {
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
  });

  it("finds the three example blocks", () => {
    expect(blocks).toHaveLength(3);
    for (const block of blocks) {
      // A backslash would be a Python escape, so the raw source would differ from the prompt.
      expect(block).not.toContain("\\");
    }
  });

  // Known defect at base: the HLD example labels an edge `|@Valid ...|`, and mermaid 11 reads
  // `|@` as an edge-id marker (LINK_ID). Only that failure is tolerated; the rewrite removes it.
  it.each(blocks.map((code, i) => [i, code] as const))(
    "example %i parses with mermaid",
    async (_i, code) => {
      const outcome = await mermaid.parse(code).then(
        (ok) => ({ ok }),
        (err: Error) => ({ err }),
      );
      if ("err" in outcome) {
        expect(code).toContain("|@");
        expect(String(outcome.err.message)).toContain("LINK_ID");
      } else {
        expect(outcome.ok).toBeTruthy();
      }
    },
  );

  it("parses the HLD example once the @ edge label is quoted", async () => {
    const fixed = blocks[0].replace(/\|(@[^|]*)\|/g, '|"$1"|');
    await expect(mermaid.parse(fixed)).resolves.toBeTruthy();
  });

  it("rejects a broken diagram, so the check can fail", async () => {
    await expect(mermaid.parse("flowchart LR\n  A -->|x|")).rejects.toThrow();
  });
});
