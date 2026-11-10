import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const HOSTILE_SVG =
  '<svg viewBox="0 0 100 50" width="100" height="50">' +
  "<script>window.__pwned = 1</script>" +
  '<a href="javascript:alert(1)" xlink:href="javascript:alert(2)"><g onclick="alert(3)"><rect width="5" height="5"/></g></a>' +
  '<foreignObject><div onmouseover="alert(4)"><img src="x" onerror="alert(5)"></div></foreignObject>' +
  "</svg>";

const mocks = vi.hoisted(() => ({
  initialize: vi.fn(),
  parse: vi.fn(async () => true),
  render: vi.fn(),
}));

vi.mock("mermaid", () => ({ default: mocks }));

const HOSTILE_SOURCE = [
  "graph TD",
  '  A["<img src=x onerror=alert(1)>"] --> B',
  '  click A "javascript:alert(1)"',
].join("\n");

async function renderHostile() {
  vi.resetModules();
  const { Mermaid } = await import("./mermaid");
  const view = render(<Mermaid code={HOSTILE_SOURCE} />);
  await waitFor(() => expect(view.container.querySelector("svg")).not.toBeNull());
  return view;
}

describe("Mermaid security", () => {
  beforeEach(() => {
    mocks.initialize.mockClear();
    mocks.parse.mockClear();
    mocks.render.mockReset();
    mocks.render.mockResolvedValue({ svg: HOSTILE_SVG });
  });

  it("initialises mermaid with a non-loose securityLevel", async () => {
    await renderHostile();
    expect(mocks.initialize).toHaveBeenCalledTimes(1);
    const cfg = mocks.initialize.mock.calls[0][0] as { securityLevel: string };
    expect(["strict", "sandbox"]).toContain(cfg.securityLevel);
  });

  it("inserts rendered SVG without scripts, event handlers or javascript: URLs", async () => {
    const { container } = await renderHostile();
    expect(container.querySelector("script")).toBeNull();
    const all = Array.from(container.querySelectorAll("*"));
    for (const el of all) {
      for (const attr of Array.from(el.attributes)) {
        expect(attr.name.toLowerCase().startsWith("on")).toBe(false);
        expect(attr.value.trim().toLowerCase().startsWith("javascript:")).toBe(false);
      }
    }
    expect(container.querySelector("rect")).not.toBeNull();
  });
});
