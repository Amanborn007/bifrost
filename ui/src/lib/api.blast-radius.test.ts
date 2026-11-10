/**
 * Wiring test for the blast-radius client (S6 dependency-impact).
 *
 * Confirms `getBlastRadius` POSTs to /dependencies/blast-radius with
 * the seed urn + options in the body, and unwraps the BifrostEnvelope
 * `.data` payload. The endpoint itself is exercised separately; this
 * guards the client contract so the Knowledge UI affordance keeps
 * hitting the right route with the right shape.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { getBlastRadius, type BlastRadiusResponse } from "./api";

const SAMPLE: BlastRadiusResponse = {
  seed_urn: "urn:bifrost:repo/svc/component/Foo",
  seed_name: "Foo",
  direction: "downstream",
  depth: 2,
  impacted_count: 1,
  impacts: [
    {
      urn: "urn:bifrost:repo/svc/component/Bar",
      name: "Bar",
      entity_type: "component",
      repo_id: "r1",
      is_cross_repo: false,
      distance_from_seed: 1,
      path_confidence: 0.8,
      source_file: "svc/bar.py",
      line: 42,
      qualified_name: "svc.bar.Bar",
      path: [
        {
          from_urn: "urn:bifrost:repo/svc/component/Foo",
          from_name: "Foo",
          to_urn: "urn:bifrost:repo/svc/component/Bar",
          to_name: "Bar",
          rel_type: "calls",
          confidence: 0.8,
          is_cross_repo: false,
          to_source_file: "svc/bar.py",
          to_line: 42,
          to_qualified_name: "svc.bar.Bar",
        },
      ],
      reason: "`Foo` is called by `Bar` (calls) at svc/bar.py:42",
    },
  ],
  tail_count: 0,
  nodes: [
    {
      id: "n1",
      urn: "urn:bifrost:repo/svc/component/Bar",
      name: "Bar",
      entity_type: "component",
      repo_id: "r1",
      distance_from_seed: 1,
      is_seed: false,
      min_path_confidence: 0.8,
      source_file: "svc/bar.py",
      line: 42,
      qualified_name: "svc.bar.Bar",
    },
  ],
  edges: [],
  confidence_buckets: { high: ["n1"], medium: [], low: [] },
  soft_cap_exceeded: false,
  stats: { node_count: 2 },
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getBlastRadius", () => {
  it("POSTs the seed urn + options and unwraps the envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ data: SAMPLE, meta: {} }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const result = await getBlastRadius("urn:bifrost:repo/svc/component/Foo", {
      direction: "downstream",
      depth: 2,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/dependencies/blast-radius");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body));
    expect(body).toMatchObject({
      urn: "urn:bifrost:repo/svc/component/Foo",
      direction: "downstream",
      depth: 2,
    });
    expect(result.impacted_count).toBe(1);
    expect(result.nodes[0].name).toBe("Bar");
  });
});
