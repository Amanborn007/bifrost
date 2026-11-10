import { describe, it, expect } from "vitest";
import {
  applyResearchEvent,
  EMPTY_RESEARCH_STATE,
} from "./research-progress";

describe("applyResearchEvent", () => {
  it("captures scope candidates", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "scope", {
      candidates: [{ id: "r1", name: "repo-one" }],
    });
    expect(s.scope).toHaveLength(1);
    expect(s.scope[0].name).toBe("repo-one");
  });

  it("tracks freshness + staleness warning", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "freshness", {
      repos: [
        { id: "r1", name: "r1", stale: false, cloned_now: false },
        { id: "r2", name: "r2", stale: true, cloned_now: false },
      ],
    });
    expect(s.freshness).toHaveLength(2);

    s = applyResearchEvent(s, "staleness_warning", {
      repos: [
        {
          id: "r2",
          name: "r2",
          last_extracted_sha: "abc",
          remote_head: "def",
          refresh_endpoint: "/api/v1/repos/r2/extract",
        },
      ],
    });
    expect(s.staleness).toHaveLength(1);
    expect(s.staleness[0].name).toBe("r2");
  });

  it("builds 2D roster from repo_roster events", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "repo_roster", {
      repo_id: "r1",
      repo_name: "webhook-api",
      squad: "DLVR",
      relevance: "high",
      agents: { code: true, jira: true, confluence: true },
    });
    expect(s.roster.r1.repoName).toBe("webhook-api");
    expect(s.roster.r1.enabledAgents.code).toBe(true);
    expect(s.roster.r1.enabledAgents.jira).toBe(true);
  });

  it("transitions agent cells through start → done", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "repo_roster", {
      repo_id: "r1",
      repo_name: "webhook-api",
      squad: "DLVR",
      relevance: "high",
      agents: { code: true, jira: true, confluence: false },
    });

    s = applyResearchEvent(s, "agent_start", {
      repo_name: "webhook-api",
      agent: "code",
    });
    expect(s.roster.r1.agentStates.code?.status).toBe("running");

    s = applyResearchEvent(s, "agent_done", {
      repo_name: "webhook-api",
      agent: "code",
      relevance: "high",
      files: 4,
    });
    expect(s.roster.r1.agentStates.code?.status).toBe("done");
    expect(s.roster.r1.agentStates.code?.files).toBe(4);

    s = applyResearchEvent(s, "agent_done", {
      repo_name: "webhook-api",
      agent: "jira",
      hits: 26,
      relevance: "high",
    });
    expect(s.roster.r1.agentStates.jira?.hits).toBe(26);
  });

  it("marks agent errored when agent_done carries an error", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "repo_roster", {
      repo_id: "r1",
      repo_name: "foo",
      squad: null,
      relevance: "medium",
      agents: { code: true },
    });
    s = applyResearchEvent(s, "agent_done", {
      repo_name: "foo",
      agent: "code",
      error: "claude -p timed out",
    });
    expect(s.roster.r1.agentStates.code?.status).toBe("error");
    expect(s.roster.r1.agentStates.code?.error).toBe("claude -p timed out");
  });

  it("accumulates synthesis tokens", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "synthesis_delta", {
      text: "Hello ",
    });
    expect(s.synthesisStarted).toBe(true);
    expect(s.streamingText).toBe("Hello ");

    s = applyResearchEvent(s, "synthesis_delta", { text: "world" });
    expect(s.streamingText).toBe("Hello world");
  });

  it("ignores unknown event types cleanly", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "mystery_event", {
      foo: "bar",
    });
    expect(s).toEqual(EMPTY_RESEARCH_STATE);
  });

  it("captures auto_extracting wait budget + per-repo wait state", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "auto_extracting", {
      message: "Started deep extraction on 2 repo(s) — waiting up to 90s",
      wait_seconds: 90,
      repos: [
        { repo_id: "r1", repo_name: "orders", task_id: "t1" },
        { repo_id: "r2", repo_name: "auth", task_id: "t2" },
      ],
    });
    expect(s.autoExtracting?.waitSeconds).toBe(90);
    expect(s.autoExtracting?.waitStatus?.r1.state).toBe("waiting");
    expect(s.autoExtracting?.waitStatus?.r2.state).toBe("waiting");
  });

  it("promotes a repo on extraction_completed_inline", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "auto_extracting", {
      message: "x",
      wait_seconds: 90,
      repos: [{ repo_id: "r1", repo_name: "orders", task_id: "t1" }],
    });
    s = applyResearchEvent(s, "extraction_completed_inline", {
      repo_id: "r1",
      repo_name: "orders",
      task_id: "t1",
      entities: 50,
      message: "orders: deep extraction finished (50 entities)",
    });
    expect(s.autoExtracting?.waitStatus?.r1.state).toBe("completed");
    expect(s.autoExtracting?.waitStatus?.r1.entities).toBe(50);
  });

  it("marks a repo as timed-out on extraction_timeout", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "auto_extracting", {
      message: "x",
      wait_seconds: 0.1,
      repos: [{ repo_id: "rs", repo_name: "slow", task_id: "ts" }],
    });
    s = applyResearchEvent(s, "extraction_timeout", {
      repo_id: "rs",
      repo_name: "slow",
      task_id: "ts",
      wait_seconds: 0.1,
      message: "slow: extraction still running, falling back",
    });
    expect(s.autoExtracting?.waitStatus?.rs.state).toBe("timeout");
  });

  it("appends to promotedRepos on research_upgraded", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "research_upgraded", {
      promoted: [
        { repo_id: "r1", repo_name: "orders" },
        { repo_id: "r2", repo_name: "auth" },
      ],
      message: "Promoted 2 repo(s) to deep-indexed",
    });
    expect(s.promotedRepos).toEqual(["orders", "auth"]);
  });

  it("captures bundle_reused for follow-up turns", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "bundle_reused", {
      conversation_id: "c1",
      prior_repos: ["orders", "auth"],
      extension_repos: ["order-fulfillment"],
      message: "Extending prior research; new agents will only fan out…",
    });
    expect(s.bundleReuse?.priorRepos).toEqual(["orders", "auth"]);
    expect(s.bundleReuse?.extensionRepos).toEqual(["order-fulfillment"]);
    expect(s.activityLog.find((e) => e.kind === "note")?.message).toContain(
      "Extending",
    );
  });

  it("logs the research_plan classification", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "research_plan", {
      mode: "extend_bundle",
      reason: "high similarity (0.92)",
    });
    const note = s.activityLog.find((e) => e.kind === "note");
    expect(note?.message).toBe(
      "Research plan: extend_bundle — high similarity (0.92)",
    );
  });

  it("captures the clarify_from_bundle shortcut on bundle_reused", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "bundle_reused", {
      conversation_id: "c1",
      prior_repos: ["orders"],
      shortcut: "clarify_from_bundle",
      message: "Skipping fresh research — answering from the prior bundle…",
    });
    expect(s.bundleReuse?.shortcut).toBe("clarify_from_bundle");
  });

  it("leaves shortcut null on a non-shortcut bundle_reused", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "bundle_reused", {
      conversation_id: "c1",
      prior_repos: ["orders"],
      message: "Builds on prior research…",
    });
    expect(s.bundleReuse?.shortcut).toBeNull();
  });

  it("logs scope_narrowed messages for EXTEND fan-out narrowing", () => {
    const s = applyResearchEvent(EMPTY_RESEARCH_STATE, "scope_narrowed", {
      from_count: 5,
      to_count: 1,
      to_repos: ["order-fulfillment"],
      prior_repos: ["orders", "auth"],
      message: "Narrowed fan-out from 5 to 1 repo(s); reusing prior research…",
    });
    const note = s.activityLog.find((e) => e.kind === "note");
    expect(note?.message).toContain("Narrowed fan-out from 5 to 1");
  });

  it("attaches hydration counts to existing bundleReuse", () => {
    let s = applyResearchEvent(EMPTY_RESEARCH_STATE, "bundle_reused", {
      conversation_id: "c1",
      prior_repos: ["orders"],
      message: "Builds on prior research…",
    });
    s = applyResearchEvent(s, "prior_subgraph_hydrated", {
      total_entities: 12,
      total_edges: 23,
      repo_count: 2,
      repo_names: ["orders", "auth"],
      missing_urns: 1,
    });
    expect(s.bundleReuse?.hydration?.totalEntities).toBe(12);
    expect(s.bundleReuse?.hydration?.totalEdges).toBe(23);
    expect(s.bundleReuse?.hydration?.repoCount).toBe(2);
    expect(s.bundleReuse?.hydration?.missingUrns).toBe(1);
  });

  it("creates a bundleReuse stub on prior_subgraph_hydrated when none yet", () => {
    const s = applyResearchEvent(
      EMPTY_RESEARCH_STATE, "prior_subgraph_hydrated", {
        total_entities: 5,
        total_edges: 10,
        repo_count: 1,
        repo_names: ["orders"],
        missing_urns: 0,
      },
    );
    expect(s.bundleReuse?.hydration?.totalEntities).toBe(5);
    expect(s.bundleReuse?.priorRepos).toEqual(["orders"]);
  });

  it("logs a hydration note in the activity log", () => {
    const s = applyResearchEvent(
      EMPTY_RESEARCH_STATE, "prior_subgraph_hydrated", {
        total_entities: 12,
        total_edges: 23,
        repo_count: 2,
        repo_names: ["a", "b"],
        missing_urns: 0,
      },
    );
    const note = s.activityLog.find((e) => e.kind === "note");
    expect(note?.message).toContain("12 prior entities");
    expect(note?.message).toContain("23 edges");
    expect(note?.message).toContain("2 repo(s)");
  });
});
