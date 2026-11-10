import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle,
  Book,
  CheckCircle2,
  ChevronRight,
  Circle,
  GitBranch,
  Globe,
  LayoutGrid,
  Loader2,
  RefreshCcw,
  Share2,
  Sparkles,
  XCircle,
} from "lucide-react";
import { cn, describeError } from "@/lib/utils";
import { AgentGraph } from "@/components/agent-graph";
import { cancelAgent, cancelExtractionTask, triggerExtraction } from "@/lib/api";
import { BlastRadiusGraph } from "@/components/blast-radius-graph";

const _AGENT_VIEW_KEY = "bifrost.agentView";
type AgentView = "graph" | "grid";

/* ── Types ────────────────────────────────────────────────────────── */

export interface ScopeCandidate {
  id: string;
  name: string;
}

export interface FreshnessEntry {
  id: string;
  name: string;
  stale: boolean;
  cloned_now: boolean;
  last_extracted_sha?: string | null;
  remote_head?: string | null;
  error?: string | null;
}

export interface StalenessEntry {
  id: string;
  name: string;
  last_extracted_sha: string;
  remote_head: string;
  refresh_endpoint: string;
}

export interface RouteEntry {
  id: string;
  name: string;
  relevance: "high" | "medium" | "low" | "none";
  reason: string;
  /** ``true`` when the ownership manifest flagged this repo as primary —
   * shown as a small star next to the name in the route list. */
  is_primary?: boolean;
}

export interface AgentState {
  name: string;
  kind: "repo" | "external";
  status: "pending" | "running" | "done" | "error";
  relevance?: "high" | "medium" | "low" | "none";
  fileCount?: number;
  findingsCount?: number;
  sources?: string[];
  error?: string | null;
}

export type AgentType = "code" | "jira" | "confluence" | "github" | "slack";

// Per-repo agent columns shown in the 2D grid. Order = display order.
const AGENT_TYPES: AgentType[] = ["code", "jira", "confluence", "github", "slack"];

export interface RepoRosterEntry {
  repoId: string;
  repoName: string;
  squad: string | null;
  relevance: "high" | "medium" | "low" | "none";
  /** Ownership manifest primary flag — primary repo for the squad. */
  isPrimary: boolean;
  /** Which per-repo agents are enabled for this turn (by the ownership manifest) */
  enabledAgents: Partial<Record<AgentType, boolean>>;
  /** Live state per agent-type for this repo */
  agentStates: Partial<
    Record<
      AgentType,
      {
        status: "pending" | "running" | "done" | "error";
        relevance?: string;
        files?: number;  // for code agent
        hits?: number;   // for jira/confluence agents
        error?: string | null;
        /** Per-shard progress under the "code" agent. Empty
         *  or absent = single-agent (no sharding) — renderers should
         *  fall back to the parent state. */
        shards?: ShardState[];
      }
    >
  >;
}

/** Per-shard progress for one repo's code agent. */
export interface ShardState {
  shardIndex: number;
  totalShards: number;
  communityLabel: string | null;
  status: "running" | "done" | "error";
  relevance?: string;
  files?: number;
  error?: string | null;
}

export interface CatalogExpansion {
  message: string;
  repos: { owner: string; name: string; url: string }[];
}

export interface AutoRegistered {
  repoName: string;
  url: string;
  reason: string;
}

export interface AlreadyBuiltMatch {
  name: string;
  urn: string;
  entity_type: string;
  repo_name: string;
}

export interface AlreadyBuilt {
  message: string;
  matches: AlreadyBuiltMatch[];
}

export interface AutoExtracting {
  message: string;
  repos: { repo_id: string; repo_name: string; task_id: string }[];
  /** Wait-and-integrate budget (seconds). Set when the coordinator is
   * blocking the chat for first-touch extractions. */
  waitSeconds?: number;
  /** Per-repo wait status, populated as extraction_completed_inline /
   * extraction_failed_inline / extraction_timeout events stream in. */
  waitStatus?: Record<
    string,
    {
      state: "waiting" | "completed" | "no_entities" | "failed" | "timeout";
      entities?: number;
      message?: string;
    }
  >;
}

export interface BundleReuse {
  conversationId: string;
  priorRepos: string[];
  extensionRepos?: string[];
  message: string;
  /** Set when the coordinator will short-circuit the research pipeline
   * and answer from the prior bundle (CLARIFY shortcut). */
  shortcut?: "clarify_from_bundle" | null;
  /** Counts populated when ``prior_subgraph_hydrated`` arrives mid-turn
   * — the prior URNs got re-fetched into structured evidence for cross-
   * turn flow reasoning. */
  hydration?: {
    totalEntities: number;
    totalEdges: number;
    repoCount: number;
    repoNames: string[];
    missingUrns: number;
  };
}

/** One line in the scrollable activity log. */
export interface ActivityEntry {
  t: number;                      // epoch ms — for age rendering
  kind: "milestone" | "status" | "agent" | "tool" | "note";
  message: string;
  repo?: string;                  // when tied to a specific repo
  agent?: string;                 // when tied to a specific agent type
}

/** Rolling cap so long research runs don't balloon memory/DOM. */
export const ACTIVITY_LOG_CAP = 80;

/** Orchestrator emits a plan at the top of every turn. */
export interface OrchestrationPlan {
  intent:
    | "architectural"
    | "impact_radius"
    | "debug"
    | "discovery"
    | "feature_estimate";
  reasoning: string;
  is_fallback: boolean;
  resolved_walk: {
    walk_depth: number;
    walk_breadth_soft_cap: number;
    confidence_floor: number;
    edge_directions: "upstream" | "downstream" | "both";
  };
}

/**
 * Subgraph stats captured for the BlastRadiusGraph component —
 * the synthesizer-side cross-repo edge count + per-repo footprint.
 */
export interface SubgraphStats {
  totalNodes: number;
  totalEdges: number;
  crossRepoEdges: number;
  /** Repo IDs whose entities show up in the master subgraph. */
  repoFootprint: string[];
}

/**
 * Per-repo status chip emitted before agents fan out so the UI
 * can show why each repo is in scope. Mirrors the Pydantic
 * ``RepoStatusInfo`` shape.
 */
export interface RepoStatusEntry {
  repo_id: string;
  repo_name: string;
  status: "deep_indexed" | "manifest_only" | "not_cloned" | "auto_registered";
  code_entity_count: number;
  auto_registered: boolean;
  clone_error: string | null;
}

export interface RepoStatusSummary {
  deep_indexed: number;
  manifest_only: number;
  not_cloned: number;
  auto_registered: number;
}

export interface ResearchState {
  status: string;
  /** Orchestrator's read of the user's intent. Populated by the
   *  ``plan`` SSE event at the top of every turn. */
  plan: OrchestrationPlan | null;
  scope: ScopeCandidate[];
  /** Per-repo classification (`status_classification` SSE event)
   *  rendered as chips before agents start so the user sees the
   *  full discovered set, not just router top-K. */
  statusByRepo: RepoStatusEntry[];
  statusSummary: RepoStatusSummary | null;
  /** Master-subgraph stats from the `subgraph` SSE event —
   *  feeds the BlastRadiusGraph component's cross-repo edge
   *  rendering. */
  subgraphStats: SubgraphStats | null;
  freshness: FreshnessEntry[];
  staleness: StalenessEntry[];
  route: RouteEntry[];
  /** 2D grid: repos × agent types */
  roster: Record<string, RepoRosterEntry>;
  /** Flat legacy map — kept for the external (org-wide) agent only. */
  agents: Record<string, AgentState>;
  externalFinishedSearchingIn?: string[];
  /** Catalog expansion: when chat found unknown-but-relevant repos */
  catalogExpansion: CatalogExpansion | null;
  autoRegistered: AutoRegistered[];
  autoExtracting: AutoExtracting | null;
  /** Set when this turn reuses a prior turn's research bundle. */
  bundleReuse: BundleReuse | null;
  /** Repo names promoted from manifest-only to deep-indexed mid-turn after
   * a wait-and-integrate auto-extraction completed in time. */
  promotedRepos: string[];
  synthesisStarted: boolean;
  streamingText: string;
  /** Running activity log — every status / agent_start / agent_done
   *  gets a line so the user can see what bifrost is doing right now. */
  activityLog: ActivityEntry[];
  /** Already-built advisory: set when the pre-check finds existing
   *  implementations matching a build-request question. Persists
   *  alongside the answer — does not disappear when synthesis starts. */
  alreadyBuilt: AlreadyBuilt | null;
  /** Number of claim corrections found by the adversarial code-check step.
   *  null = check hasn't run yet; 0 = ran and found nothing. */
  adversarialCorrections: number | null;
}

export const EMPTY_RESEARCH_STATE: ResearchState = {
  status: "",
  plan: null,
  scope: [],
  statusByRepo: [],
  statusSummary: null,
  subgraphStats: null,
  freshness: [],
  staleness: [],
  route: [],
  roster: {},
  agents: {},
  catalogExpansion: null,
  autoRegistered: [],
  autoExtracting: null,
  bundleReuse: null,
  promotedRepos: [],
  synthesisStarted: false,
  streamingText: "",
  activityLog: [],
  alreadyBuilt: null,
  adversarialCorrections: null,
};


function _pushActivity(
  prev: ResearchState, entry: Omit<ActivityEntry, "t">,
): ResearchState {
  const log = prev.activityLog;
  const last = log[log.length - 1];
  // Collapse back-to-back duplicates — the SSE stream can repeat the
  // same status a few times during retries; no point filling the log.
  if (
    last &&
    last.kind === entry.kind &&
    last.message === entry.message &&
    last.repo === entry.repo &&
    last.agent === entry.agent
  ) {
    return prev;
  }
  const next = [...log, { t: Date.now(), ...entry }];
  if (next.length > ACTIVITY_LOG_CAP) next.splice(0, next.length - ACTIVITY_LOG_CAP);
  return { ...prev, activityLog: next };
}

/* ── Event-reducer entry point ────────────────────────────────────── */

/**
 * Apply one SSE event dict to the research state. Called from query.tsx's
 * stream handler. Pure reducer — returns a new state.
 */
export function applyResearchEvent(
  prev: ResearchState,
  eventType: string,
  data: Record<string, unknown>,
): ResearchState {
  switch (eventType) {
    case "plan": {
      // Orchestrator's intent + reasoning + resolved walk params.
      // Surface as a status line so the user sees "Bifrost classified
      // this as impact_radius — walking 3 hops upstream from
      // OrderExecutor" before the actual work starts.
      const intent = String(data.intent ?? "discovery");
      const reasoning = String(data.reasoning ?? "");
      const resolved =
        (data.resolved_walk as OrchestrationPlan["resolved_walk"]) ?? {
          walk_depth: 2,
          walk_breadth_soft_cap: 200,
          confidence_floor: 0.55,
          edge_directions: "both",
        };
      const plan: OrchestrationPlan = {
        intent: intent as OrchestrationPlan["intent"],
        reasoning,
        is_fallback: Boolean(data.is_fallback),
        resolved_walk: resolved,
      };
      const next = { ...prev, plan };
      return _pushActivity(next, {
        kind: "milestone",
        message: `Plan: ${intent} — ${reasoning || "(no reasoning)"}`,
      });
    }
    case "status": {
      const message = String(data.message ?? "");
      const next = { ...prev, status: message };
      return message
        ? _pushActivity(next, { kind: "status", message })
        : next;
    }

    case "scope": {
      const candidates = (data.candidates as ScopeCandidate[]) ?? [];
      return { ...prev, scope: candidates };
    }

    case "status_classification": {
      // Per-repo deep_indexed / manifest_only / not_cloned /
      // auto_registered chips rendered as a row under "Repos
      // discovered" before any agent has spoken.
      const repos = (data.repos as RepoStatusEntry[]) ?? [];
      const summary =
        (data.summary as RepoStatusSummary | undefined) ?? null;
      const next = { ...prev, statusByRepo: repos, statusSummary: summary };
      const summaryParts: string[] = [];
      if (summary) {
        if (summary.deep_indexed) summaryParts.push(`${summary.deep_indexed} deep-indexed`);
        if (summary.manifest_only) summaryParts.push(`${summary.manifest_only} manifest-only`);
        if (summary.auto_registered) summaryParts.push(`${summary.auto_registered} auto-registered`);
        if (summary.not_cloned) summaryParts.push(`${summary.not_cloned} not-cloned`);
      }
      const message = summaryParts.length
        ? `Repos in scope: ${summaryParts.join(", ")}`
        : `Classified ${repos.length} repo(s)`;
      return _pushActivity(next, { kind: "status", message });
    }

    case "freshness": {
      const repos = (data.repos as FreshnessEntry[]) ?? [];
      return { ...prev, freshness: repos };
    }

    case "subgraph": {
      // Capture master-subgraph stats so BlastRadiusGraph can
      // show cross-repo edge counts + the discovered repo footprint.
      const stats = (data.stats as Record<string, number> | undefined) ?? {};
      const footprint = (data.repo_footprint as string[] | undefined) ?? [];
      const cre = typeof data.cross_repo_edge_count === "number"
        ? data.cross_repo_edge_count
        : (stats.cross_repo_edges ?? 0);
      const totalNodes = Number(stats.total_nodes ?? 0);
      const totalEdges = Number(stats.total_edges ?? 0);
      const next = {
        ...prev,
        subgraphStats: {
          totalNodes,
          totalEdges,
          crossRepoEdges: Number(cre),
          repoFootprint: footprint,
        },
      };
      return _pushActivity(next, {
        kind: "milestone",
        message: `Built subgraph: ${totalNodes} entit${totalNodes === 1 ? "y" : "ies"}, ${totalEdges} edge${totalEdges === 1 ? "" : "s"} (${cre} cross-repo) across ${footprint.length} repo${footprint.length === 1 ? "" : "s"}`,
      });
    }

    case "staleness_warning": {
      const repos = (data.repos as StalenessEntry[]) ?? [];
      return { ...prev, staleness: repos };
    }

    case "route": {
      const selected = (data.selected as RouteEntry[]) ?? [];
      const next = { ...prev, route: selected };
      const top = selected.slice(0, 3).map((r) => r.name).join(", ");
      const more = selected.length > 3 ? ` (+${selected.length - 3} more)` : "";
      return _pushActivity(next, {
        kind: "milestone",
        message: `Routed to ${selected.length} repo${selected.length === 1 ? "" : "s"}: ${top}${more}`,
      });
    }

    case "repo_roster": {
      const repoId = String(data.repo_id ?? "");
      const repoName = String(data.repo_name ?? "");
      if (!repoId || !repoName) return prev;
      const enabled = (data.agents as Record<string, boolean>) ?? {};
      const existing = prev.roster[repoId];
      return {
        ...prev,
        roster: {
          ...prev.roster,
          [repoId]: {
            repoId,
            repoName,
            squad: (data.squad as string | null) ?? null,
            relevance:
              (data.relevance as RepoRosterEntry["relevance"]) ?? "medium",
            isPrimary: data.is_primary === true,
            enabledAgents: {
              code: enabled.code !== false,
              jira: enabled.jira === true,
              confluence: enabled.confluence === true,
              github: enabled.github === true,
              slack: enabled.slack === true,
            },
            agentStates: existing?.agentStates ?? {},
          },
        },
      };
    }

    case "agent_start": {
      const repoName = String(data.repo_name ?? "");
      const agent = String(data.agent ?? "") as AgentType;
      if (!repoName || !agent) return prev;
      const entry = Object.values(prev.roster).find((r) => r.repoName === repoName);
      if (!entry) return prev;
      // Per-shard agent_start has shard_index + total_shards.
      // Append/update the shard entry under the parent agent state
      // without flipping the parent back to "running" (the parent
      // started already at the legacy event).
      const shardIndex = typeof data.shard_index === "number"
        ? data.shard_index : null;
      const prevAgentState = entry.agentStates[agent] ?? { status: "pending" as const };
      let nextAgentState = { ...prevAgentState, status: "running" as const };
      if (shardIndex !== null) {
        const shards = prevAgentState.shards ?? [];
        const existingIdx = shards.findIndex((s) => s.shardIndex === shardIndex);
        const newShard: ShardState = {
          shardIndex,
          totalShards: typeof data.total_shards === "number"
            ? data.total_shards : shardIndex,
          communityLabel: data.community_label != null
            ? String(data.community_label) : null,
          status: "running",
        };
        const nextShards = existingIdx >= 0
          ? shards.map((s, i) => i === existingIdx ? newShard : s)
          : [...shards, newShard].sort((a, b) => a.shardIndex - b.shardIndex);
        nextAgentState = { ...nextAgentState, shards: nextShards };
      }
      const next = {
        ...prev,
        roster: {
          ...prev.roster,
          [entry.repoId]: {
            ...entry,
            agentStates: {
              ...entry.agentStates,
              [agent]: nextAgentState,
            },
          },
        },
      };
      // Don't push per-agent "started" rows into the
      // activity log — they were 50+ rows of noise drowning out
      // the real milestones (plan / scope / synthesis). The 2D
      // AgentGrid above already shows live per-agent status; the
      // log now only narrates major beats. The state still updates
      // so the grid renders the running spinner.
      return next;
    }

    case "agent_done": {
      const repoName = String(data.repo_name ?? "");
      const agent = String(data.agent ?? "") as AgentType;
      if (!repoName || !agent) return prev;
      const entry = Object.values(prev.roster).find((r) => r.repoName === repoName);
      if (!entry) return prev;
      const err = data.error ? String(data.error) : null;
      const shardIndex = typeof data.shard_index === "number"
        ? data.shard_index : null;
      const prevAgentState = entry.agentStates[agent] ?? { status: "pending" as const };
      let nextAgentState;
      if (shardIndex !== null) {
        // Per-shard agent_done — update the shard entry but
        // leave the parent agent in "running" until the merged
        // ``agent_done`` (no shard_index) lands.
        const shards = prevAgentState.shards ?? [];
        const updatedShard: ShardState = {
          shardIndex,
          totalShards: typeof data.total_shards === "number"
            ? data.total_shards : shardIndex,
          communityLabel: data.community_label != null
            ? String(data.community_label) : null,
          status: (err ? "error" : "done"),
          relevance: data.relevance ? String(data.relevance) : undefined,
          files: typeof data.files === "number" ? data.files : undefined,
          error: err,
        };
        const existingIdx = shards.findIndex((s) => s.shardIndex === shardIndex);
        const nextShards = existingIdx >= 0
          ? shards.map((s, i) => i === existingIdx ? updatedShard : s)
          : [...shards, updatedShard].sort((a, b) => a.shardIndex - b.shardIndex);
        nextAgentState = {
          ...prevAgentState,
          shards: nextShards,
        };
      } else {
        // Legacy / merged agent_done — finalize the parent state.
        nextAgentState = {
          ...prevAgentState,
          status: (err ? "error" : "done") as "error" | "done",
          relevance: data.relevance
            ? String(data.relevance)
            : prevAgentState.relevance,
          files: typeof data.files === "number" ? data.files : prevAgentState.files,
          hits: typeof data.hits === "number" ? data.hits : prevAgentState.hits,
          error: err,
        };
      }
      const updatedEntry = {
        ...entry,
        agentStates: {
          ...entry.agentStates,
          [agent]: nextAgentState,
        },
      };
      const next = {
        ...prev,
        roster: {
          ...prev.roster,
          [entry.repoId]: updatedEntry,
        },
      };
      // Collapse per-agent rows into ONE per-repo
      // summary line that fires only when EVERY enabled agent for
      // the repo has reached a terminal state (done / error). This
      // turns 5 rows × N repos = 5N rows into N rows of real
      // signal, with the per-agent breakdown inline. Per-shard
      // events are skipped — they're internal scaffolding.
      if (shardIndex !== null) {
        return next;
      }
      const enabled = updatedEntry.enabledAgents;
      const states = updatedEntry.agentStates;
      const expected: AgentType[] = AGENT_TYPES.filter(
        (k) => enabled[k] === true,
      );
      const allTerminal = expected.every((k) => {
        const st = states[k]?.status;
        return st === "done" || st === "error";
      });
      if (!allTerminal) {
        return next;
      }
      // Build one-line summary: "repo · code high (5f) · jira 3 hits · conf err".
      const parts: string[] = [];
      for (const k of expected) {
        const s = states[k];
        if (!s) continue;
        if (s.status === "error") {
          parts.push(`${k} err`);
          continue;
        }
        const det: string[] = [];
        if (s.relevance) det.push(s.relevance);
        if (typeof s.files === "number") det.push(`${s.files}f`);
        if (typeof s.hits === "number") det.push(`${s.hits}h`);
        parts.push(`${k} ${det.join(" ") || "done"}`);
      }
      return _pushActivity(next, {
        kind: "agent",
        message: `${repoName} · ${parts.join(" · ")}`,
        repo: repoName,
      });
    }

    // Legacy flat-map events — kept for back-compat with any old-flow fallback
    case "subagent_start":
    case "subagent_done": {
      // Now consumed by the 2D roster above; we ignore these (they're
      // still emitted as back-compat shims but the roster is authoritative).
      return prev;
    }

    case "external_start": {
      const name = String(data.label ?? "External sources");
      const next = {
        ...prev,
        agents: {
          ...prev.agents,
          [name]: {
            name,
            kind: "external" as const,
            status: "running" as const,
          },
        },
      };
      return _pushActivity(next, {
        kind: "agent",
        message: `${name} — searching`,
        agent: "external",
      });
    }

    case "external_done": {
      const existingKey = Object.keys(prev.agents).find(
        (k) => prev.agents[k].kind === "external",
      ) ?? "External sources";
      const err = data.error ? String(data.error) : null;
      const searched = (data.searched_sources as string[]) ?? [];
      const findings = Number(data.findings_count ?? 0);
      const next = {
        ...prev,
        externalFinishedSearchingIn: searched,
        agents: {
          ...prev.agents,
          [existingKey]: {
            ...prev.agents[existingKey],
            name: existingKey,
            kind: "external" as const,
            status: (err ? "error" : "done") as "error" | "done",
            findingsCount: findings,
            sources: searched,
            error: err,
          },
        },
      };
      return _pushActivity(next, {
        kind: "agent",
        message: err
          ? `${existingKey} FAILED: ${err}`
          : `${existingKey} done · ${findings} finding${findings === 1 ? "" : "s"}`,
        agent: "external",
      });
    }

    case "catalog_expansion": {
      const msg = String(data.message ?? "");
      const next = {
        ...prev,
        catalogExpansion: {
          message: msg,
          repos: (data.repos as CatalogExpansion["repos"]) ?? [],
        },
      };
      return msg ? _pushActivity(next, { kind: "note", message: msg }) : next;
    }

    case "auto_registered": {
      const repoName = String(data.repo_name ?? "");
      if (!repoName) return prev;
      const next = {
        ...prev,
        autoRegistered: [
          ...prev.autoRegistered,
          {
            repoName,
            url: String(data.url ?? ""),
            reason: String(data.reason ?? ""),
          },
        ],
      };
      return _pushActivity(next, {
        kind: "note",
        message: `Auto-registered ${repoName} from org catalog`,
        repo: repoName,
      });
    }

    case "auto_extracting": {
      const repos = (data.repos as AutoExtracting["repos"]) ?? [];
      const waitStatus: NonNullable<AutoExtracting["waitStatus"]> = {};
      for (const r of repos) {
        waitStatus[r.repo_id] = { state: "waiting" };
      }
      return {
        ...prev,
        autoExtracting: {
          message: String(data.message ?? ""),
          repos,
          waitSeconds:
            typeof data.wait_seconds === "number" ? data.wait_seconds : undefined,
          waitStatus,
        },
      };
    }

    case "extraction_completed_inline": {
      const repoId = String(data.repo_id ?? "");
      const repoName = String(data.repo_name ?? "");
      if (!repoId || !prev.autoExtracting) return prev;
      const next = {
        ...prev,
        autoExtracting: {
          ...prev.autoExtracting,
          waitStatus: {
            ...(prev.autoExtracting.waitStatus ?? {}),
            [repoId]: {
              state: "completed" as const,
              entities: typeof data.entities === "number" ? data.entities : undefined,
              message: String(data.message ?? ""),
            },
          },
        },
      };
      return _pushActivity(next, {
        kind: "note",
        message: String(data.message ?? `${repoName}: extraction completed inline`),
        repo: repoName,
      });
    }

    case "extraction_no_entities": {
      const repoId = String(data.repo_id ?? "");
      const repoName = String(data.repo_name ?? "");
      if (!repoId || !prev.autoExtracting) return prev;
      const next = {
        ...prev,
        autoExtracting: {
          ...prev.autoExtracting,
          waitStatus: {
            ...(prev.autoExtracting.waitStatus ?? {}),
            [repoId]: {
              state: "no_entities" as const,
              message: String(data.message ?? ""),
            },
          },
        },
      };
      return _pushActivity(next, {
        kind: "note",
        message: String(data.message ?? `${repoName}: extraction produced no entities`),
        repo: repoName,
      });
    }

    case "extraction_failed_inline": {
      const repoId = String(data.repo_id ?? "");
      const repoName = String(data.repo_name ?? "");
      if (!repoId || !prev.autoExtracting) return prev;
      const next = {
        ...prev,
        autoExtracting: {
          ...prev.autoExtracting,
          waitStatus: {
            ...(prev.autoExtracting.waitStatus ?? {}),
            [repoId]: {
              state: "failed" as const,
              message: String(data.message ?? ""),
            },
          },
        },
      };
      return _pushActivity(next, {
        kind: "note",
        message: String(
          data.message ?? `${repoName}: extraction ${data.state ?? "failed"}`,
        ),
        repo: repoName,
      });
    }

    case "extraction_timeout": {
      const repoId = String(data.repo_id ?? "");
      const repoName = String(data.repo_name ?? "");
      if (!repoId || !prev.autoExtracting) return prev;
      const next = {
        ...prev,
        autoExtracting: {
          ...prev.autoExtracting,
          waitStatus: {
            ...(prev.autoExtracting.waitStatus ?? {}),
            [repoId]: {
              state: "timeout" as const,
              message: String(data.message ?? ""),
            },
          },
        },
      };
      return _pushActivity(next, {
        kind: "note",
        message: String(
          data.message ?? `${repoName}: extraction still running, falling back`,
        ),
        repo: repoName,
      });
    }

    case "research_upgraded": {
      const promoted =
        (data.promoted as { repo_id: string; repo_name: string }[]) ?? [];
      const names = promoted.map((p) => p.repo_name);
      const next = {
        ...prev,
        promotedRepos: Array.from(new Set([...prev.promotedRepos, ...names])),
      };
      return _pushActivity(next, {
        kind: "note",
        message: String(
          data.message ?? `Promoted ${names.length} repo(s) to deep-indexed`,
        ),
      });
    }

    case "research_plan": {
      const mode = String(data.mode ?? "new_scope");
      const reason = String(data.reason ?? "");
      return _pushActivity(prev, {
        kind: "note",
        message: `Research plan: ${mode}${reason ? ` — ${reason}` : ""}`,
      });
    }

    case "scope_narrowed": {
      const message = String(data.message ?? "");
      return message
        ? _pushActivity(prev, { kind: "note", message })
        : prev;
    }

    case "scope_fallback": {
      const message = String(data.message ?? "");
      return message
        ? _pushActivity(prev, { kind: "note", message })
        : prev;
    }

    case "scope_inherited_from_prior": {
      const message = String(data.message ?? "");
      return message
        ? _pushActivity(prev, { kind: "note", message })
        : prev;
    }

    case "scope_widened": {
      const message = String(data.message ?? "");
      return message
        ? _pushActivity(prev, { kind: "note", message })
        : prev;
    }

    case "prior_subgraph_hydrated": {
      const totalEntities = Number(data.total_entities ?? 0);
      const totalEdges = Number(data.total_edges ?? 0);
      const repoCount = Number(data.repo_count ?? 0);
      const repoNames = (data.repo_names as string[]) ?? [];
      const missingUrns = Number(data.missing_urns ?? 0);
      const hydration = {
        totalEntities,
        totalEdges,
        repoCount,
        repoNames,
        missingUrns,
      };
      const next: ResearchState = {
        ...prev,
        bundleReuse: prev.bundleReuse
          ? { ...prev.bundleReuse, hydration }
          : {
              conversationId: "",
              priorRepos: repoNames,
              message: "",
              shortcut: null,
              hydration,
            },
      };
      const note =
        `Hydrated ${totalEntities} prior entities + ${totalEdges} ` +
        `edges from ${repoCount} repo(s) for cross-turn reasoning` +
        (missingUrns ? ` (${missingUrns} URN(s) no longer exist)` : "");
      return _pushActivity(next, { kind: "note", message: note });
    }

    case "bundle_reused": {
      const message = String(data.message ?? "");
      const shortcut =
        data.shortcut === "clarify_from_bundle"
          ? "clarify_from_bundle"
          : null;
      const next = {
        ...prev,
        bundleReuse: {
          conversationId: String(data.conversation_id ?? ""),
          priorRepos: (data.prior_repos as string[]) ?? [],
          extensionRepos: (data.extension_repos as string[]) ?? undefined,
          message,
          shortcut,
        },
      };
      return message
        ? _pushActivity(next, { kind: "note", message })
        : next;
    }

    case "synthesis_delta": {
      const text = String(data.text ?? "");
      // First synthesis token = synthesis began. Push ONE milestone
      // entry, not one per token.
      if (!prev.synthesisStarted && text) {
        const withMilestone = _pushActivity(prev, {
          kind: "milestone",
          message: "Synthesising final answer…",
        });
        return {
          ...withMilestone,
          synthesisStarted: true,
          streamingText: withMilestone.streamingText + text,
        };
      }
      return {
        ...prev,
        synthesisStarted: true,
        streamingText: prev.streamingText + text,
      };
    }

    case "already_built": {
      // Advisory pre-check: existing implementations found matching a
      // build-request question. Persist alongside the answer.
      const message = String(data.message ?? "");
      const matches = (data.matches as AlreadyBuiltMatch[]) ?? [];
      return {
        ...prev,
        alreadyBuilt: { message, matches },
      };
    }

    case "adversarial_corrections": {
      // Adversarial code-claim verifier found discrepancies between the
      // synthesised answer and T1 code evidence. count=0 means the check
      // ran but found nothing (all claims verified).
      const count = typeof data.count === "number" ? data.count : 0;
      const next = { ...prev, adversarialCorrections: count };
      return count > 0
        ? _pushActivity(next, {
            kind: "note",
            message: `Adversarial check: ${count} claim(s) corrected by code evidence`,
          })
        : next;
    }

    default:
      return prev;
  }
}

/* ── Visual ───────────────────────────────────────────────────────── */

/** Derive the 3-state single-line phase status shown prominently in the
 *  header. Transitions:
 *   1. No route yet  → "Identifying relevant repos…"
 *   2. Route arrived, synthesis not started → "Reading code and Jira in repo1, repo2…"
 *   3. First synthesis_delta → "Writing answer…"
 */
function _phaseStatus(state: ResearchState): string {
  if (state.synthesisStarted) {
    return "Writing answer…";
  }
  if (state.route.length > 0) {
    const names = state.route.slice(0, 3).map((r) => r.name);
    const suffix = state.route.length > 3 ? ` +${state.route.length - 3} more` : "";
    return `Reading code and Jira in ${names.join(", ")}${suffix}…`;
  }
  return "Identifying relevant repos…";
}

/**
 * Already-built advisory banner. Shown ABOVE the research pipeline when the
 * pre-check found existing implementations that match a build request.
 * Uses amber/warn styling to match the caution/warning convention in the UI.
 * Persists alongside the answer — does not disappear when synthesis starts.
 */
function AlreadyBuiltBanner({ alreadyBuilt }: { alreadyBuilt: AlreadyBuilt }) {
  return (
    <div className="mb-2 rounded-md border border-warn/50 bg-warn-soft px-3 py-2.5">
      <div className="flex items-start gap-2">
        <AlertTriangle
          className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn"
          strokeWidth={1.75}
        />
        <div className="flex-1 min-w-0">
          <p className="text-[12px] font-semibold text-warn leading-snug">
            Already exists — {alreadyBuilt.message}
          </p>
          <ul className="mt-1.5 space-y-0.5">
            {alreadyBuilt.matches.map((m) => (
              <li
                key={m.urn}
                className="flex flex-wrap items-center gap-1.5 text-[11px] text-fg-1"
              >
                <Book className="h-3 w-3 shrink-0 text-warn" strokeWidth={1.75} />
                <span className="font-medium text-fg-0">{m.name}</span>
                <span className="font-mono text-[10px] text-fg-3">
                  {m.entity_type}
                </span>
                <span className="text-fg-3">in</span>
                <span className="font-mono text-[10.5px] text-fg-1">
                  {m.repo_name}
                </span>
                <span
                  className="font-mono text-[9.5px] text-fg-3 truncate max-w-[260px]"
                  title={m.urn}
                >
                  {m.urn}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

/**
 * Adversarial code-check badge. Shown inline when the post-synthesis claim
 * verifier found discrepancies between the answer and T1 code evidence.
 * Amber badge so it reads as a caution, not a hard error.
 */
function AdversarialBadge({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <div className="mb-2 rounded-md border border-warn/50 bg-warn-soft px-3 py-2">
      <div className="flex items-center gap-2">
        <AlertTriangle
          className="h-3.5 w-3.5 shrink-0 text-warn"
          strokeWidth={1.75}
        />
        <p className="text-[11.5px] font-semibold text-warn">
          {count} claim{count === 1 ? "" : "s"} corrected by code check
        </p>
        <span className="ml-auto text-[10.5px] text-fg-2">
          see "Adversarial corrections" in the answer below
        </span>
      </div>
    </div>
  );
}

export function ResearchProgress({
  state,
  conversationId = "",
}: {
  state: ResearchState;
  /** Passed to per-agent cancel buttons. Empty string disables
   *  the cancel affordance — keeps the component renderable in
   *  contexts (e.g. the read-only handover viewer) that don't have
   *  a live conversation. */
  conversationId?: string;
}) {
  const hasScope = state.scope.length > 0;
  const hasFreshness = state.freshness.length > 0;
  const hasRoute = state.route.length > 0;

  const freshCount = state.freshness.filter((f) => !f.stale && !f.error).length;
  const staleCount = state.staleness.length;
  const errorCount = state.freshness.filter((f) => f.error).length;

  const phaseLabel = _phaseStatus(state);
  const isActive = !!state.status && !state.synthesisStarted;

  return (
    <div className="space-y-2">
      {state.alreadyBuilt && (
        <AlreadyBuiltBanner alreadyBuilt={state.alreadyBuilt} />
      )}
      {typeof state.adversarialCorrections === "number" && state.adversarialCorrections > 0 && (
        <AdversarialBadge count={state.adversarialCorrections} />
      )}
      <div className="rounded-lg border border-border bg-bg-1">
      <header className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <Sparkles className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
        <span className="text-[13px] font-semibold text-fg-0">
          Research pipeline
        </span>
        {/* Primary single-line phase status — the only thing visible by default */}
        {state.status && (
          <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-fg-1">
            <Loader2
              className={cn(
                "h-3 w-3 text-accent",
                isActive && "animate-spin",
              )}
              strokeWidth={1.75}
            />
            {phaseLabel}
          </span>
        )}
      </header>

      {/* Everything below is in a collapsible "Details" section */}
      <details className="group">
        <summary className="flex cursor-pointer items-center gap-1.5 px-4 py-2 text-[11px] font-medium text-fg-2 hover:text-fg-0 select-none">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Details
        </summary>

        <div className="divide-y divide-border border-t border-border">
          {/* Orchestrator plan banner */}
          {state.plan && !state.plan.is_fallback && (
            <div className="px-4 py-2.5">
              <PlanBanner plan={state.plan} />
            </div>
          )}
          {state.bundleReuse && (
            <div className="px-4 py-2.5">
              <BundleReuseBanner reuse={state.bundleReuse} />
            </div>
          )}
          <PipelineRow
            label="Scope"
            done={hasScope}
            detail={
              hasScope ? `${state.scope.length} candidate repo${state.scope.length === 1 ? "" : "s"}` : "…"
            }
          >
            {state.catalogExpansion && (
              <CatalogExpansionBanner
                expansion={state.catalogExpansion}
                autoRegistered={state.autoRegistered}
              />
            )}
            {state.statusByRepo.length > 0 && (
              <RepoStatusChips
                repos={state.statusByRepo}
                summary={state.statusSummary}
              />
            )}
          </PipelineRow>
          <PipelineRow
            label="Freshness"
            done={hasFreshness}
            detail={
              hasFreshness
                ? `${freshCount} fresh · ${staleCount} stale${errorCount ? ` · ${errorCount} error` : ""}`
                : "…"
            }
          >
            {state.staleness.length > 0 && (
              <StalenessBanner repos={state.staleness} />
            )}
          </PipelineRow>
          <PipelineRow
            label="Routing"
            done={hasRoute}
            detail={hasRoute ? `${state.route.length} selected` : "…"}
          >
            {state.autoExtracting && (
              <AutoExtractingBanner autoExtracting={state.autoExtracting} />
            )}
          </PipelineRow>
          <AgentsRow state={state} conversationId={conversationId} />

          <PipelineRow
            label="Synthesis"
            done={false}
            inProgress={state.synthesisStarted}
            detail={
              state.synthesisStarted
                ? `${state.streamingText.length.toLocaleString()} chars so far`
                : "Waiting for agents…"
            }
          />
          <ActivityLog entries={state.activityLog} />
          <BlastRadiusGraphSection state={state} />
        </div>
      </details>
      </div>
    </div>
  );
}


/**
 * Collapsible "Show blast-radius graph" toggle. Defaults open
 * once we have at least one classified repo so the user sees the
 * graph as soon as it's meaningful, but the disclosure stays
 * stickable so anyone who finds it noisy can collapse it once. Sits
 * below the activity log so the answer surface stays clean —
 * matches the spec's "toggle in the chat answer footer".
 */
function BlastRadiusGraphSection({ state }: { state: ResearchState }) {
  const [open, setOpen] = useState(false);
  if (state.statusByRepo.length === 0) return null;
  return (
    <details
      className="group border-t border-border"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer items-center justify-between gap-2 px-4 py-2 text-[11px] font-medium text-fg-2 hover:text-fg-0">
        <span className="flex items-center gap-1.5">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Show blast-radius graph
          <span className="font-mono text-[10.5px] text-fg-3">
            ({state.statusByRepo.length} repos
            {state.subgraphStats
              ? ` · ${state.subgraphStats.crossRepoEdges} cross-repo edges`
              : ""}
            )
          </span>
        </span>
      </summary>
      <div className="px-4 pb-3">
        <BlastRadiusGraph state={state} />
      </div>
    </details>
  );
}


/** Agents pipeline row + graph/grid toggle.
 *
 * Pulled out of the main ResearchProgress body so the view-mode
 * state stays local and the JSX stays readable. The toggle defaults
 * to "graph" since the user explicitly asked for the agent-graph
 * viz; the choice persists in localStorage so the next turn opens
 * the same view.
 */
function AgentsRow({
  state,
  conversationId,
}: {
  state: ResearchState;
  conversationId: string;
}) {
  const rosterEntries = useMemo(
    () => Object.values(state.roster),
    [state.roster],
  );
  const externalAgent = useMemo(
    () => Object.values(state.agents).find((a) => a.kind === "external"),
    [state.agents],
  );
  const hasRoute = state.route.length > 0;

  const [view, setView] = useState<AgentView>(() => {
    if (typeof window === "undefined") return "graph";
    const saved = window.localStorage?.getItem(_AGENT_VIEW_KEY);
    return saved === "grid" ? "grid" : "graph";
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage?.setItem(_AGENT_VIEW_KEY, view);
    } catch {
      /* ignore quota errors — non-critical */
    }
  }, [view]);

  return (
    <div className="px-4 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="w-[72px] shrink-0 text-[11.5px] font-medium text-fg-2">
          Agents
        </span>
        <span className="flex flex-1 items-center justify-between gap-2 text-[11.5px] text-fg-1">
          <span>
            {rosterEntries.length > 0 ? (
              <>
                {rosterEntries.length} repo ·{" "}
                {_countAgents(rosterEntries)} specialized agents
                {externalAgent ? " · 1 org-wide" : ""}
              </>
            ) : hasRoute ? (
              <span className="text-fg-2">
                {state.route.length} selected ·{" "}
                <span className="text-warn">none deep-indexed yet</span>
                {externalAgent ? " · falling back to org-wide" : ""}
              </span>
            ) : (
              <span className="text-fg-3">…</span>
            )}
          </span>
          {(rosterEntries.length > 0 || externalAgent) && (
            <div
              className="inline-flex items-center gap-0.5 rounded-md border border-border bg-bg-1 p-0.5"
              role="tablist"
              aria-label="Agent view"
            >
              <button
                type="button"
                role="tab"
                aria-selected={view === "graph"}
                onClick={() => setView("graph")}
                title="Graph view (live fan-out)"
                aria-label="Show agents as a graph"
                className={cn(
                  "inline-flex h-5 items-center gap-1 rounded px-1.5 text-[10.5px] font-medium transition-colors",
                  view === "graph"
                    ? "bg-bg-2 text-fg-0"
                    : "text-fg-2 hover:text-fg-0",
                )}
              >
                <Share2 className="h-3 w-3" strokeWidth={1.75} />
                Graph
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={view === "grid"}
                onClick={() => setView("grid")}
                title="Grid view (compact table)"
                aria-label="Show agents as a grid"
                className={cn(
                  "inline-flex h-5 items-center gap-1 rounded px-1.5 text-[10.5px] font-medium transition-colors",
                  view === "grid"
                    ? "bg-bg-2 text-fg-0"
                    : "text-fg-2 hover:text-fg-0",
                )}
              >
                <LayoutGrid className="h-3 w-3" strokeWidth={1.75} />
                Grid
              </button>
            </div>
          )}
        </span>
      </div>
      {rosterEntries.length > 0 && view === "grid" && (
        <div className="mt-2">
          <AgentGrid roster={rosterEntries} conversationId={conversationId} />
        </div>
      )}
      {(rosterEntries.length > 0 || externalAgent) && view === "graph" && (
        <div className="mt-2">
          <AgentGraph state={state} />
        </div>
      )}
      {externalAgent && view === "grid" && (
        <div className="mt-2">
          <AgentCard agent={externalAgent} />
        </div>
      )}
      {/* Manual extract prompt — only shown when the backend's
          auto-extract path didn't kick in (e.g., the route fired
          but ``manifest_only_repos`` was empty even though no roster
          arrived). When ``autoExtracting`` is set we suppress this
          banner entirely so users see ONE coherent progress
          surface. The prompt itself auto-triggers on mount so the
          user no longer has to click "Extract N" — same UX as the
          backend-driven path. */}
      {rosterEntries.length === 0 && hasRoute && !state.autoExtracting && (
        <ExtractRoutedReposPrompt repos={state.route} />
      )}
    </div>
  );
}


/** sessionStorage key prefix used to dedupe ExtractRoutedReposPrompt's
 *  auto-fire across page refreshes. Without this, every refresh
 *  remounts the component and re-fires triggerExtraction (+ shows a
 *  toast) for the same repos — the user complaint that drove the
 *  follow-up fix. Keyed by the sorted repo-id signature so a route
 *  change that adds/removes a repo will fire fresh.
 */
const _AUTO_EXTRACT_DEDUP_KEY_PREFIX = "bf:auto-extracted:";

function _autoExtractKey(repos: RouteEntry[]): string {
  const ids = repos.map((r) => r.id).sort().join(",");
  return _AUTO_EXTRACT_DEDUP_KEY_PREFIX + ids;
}

function _alreadyAutoExtracted(repos: RouteEntry[]): boolean {
  if (typeof window === "undefined") return false;
  try {
    return Boolean(window.sessionStorage?.getItem(_autoExtractKey(repos)));
  } catch {
    return false;
  }
}

function _markAutoExtracted(repos: RouteEntry[]): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage?.setItem(_autoExtractKey(repos), String(Date.now()));
  } catch {
    /* private mode or quota — auto-extract still fires but won't
       dedupe across refreshes. Acceptable degraded behavior. */
  }
}


function ExtractRoutedReposPrompt({ repos }: { repos: RouteEntry[] }) {
  const [running, setRunning] = useState(false);
  const [queued, setQueued] = useState<string[]>([]);
  const [errors, setErrors] = useState<Array<{ name: string; err: string }>>([]);
  // Track whether we've already auto-fired so a re-render with the
  // same repos list doesn't re-queue them.
  const autoFiredRef = useRef(false);
  // Ref-based guard for extractAll so the callback stays stable (no
  // `running` state in deps) — prevents the useEffect from re-running
  // every render while still blocking concurrent extraction calls.
  const runningRef = useRef(false);

  // Stable identity string — prevents useEffect from re-running when
  // the parent re-renders and passes a new array ref with the same repos.
  const repoIds = repos.map((r) => r.id || r.name).join(",");

  const extractAll = useCallback(async () => {
    if (runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    const ok: string[] = [];
    const bad: Array<{ name: string; err: string }> = [];
    // Fire in parallel but bound to 3 at a time — queueing 500 at once
    // crushes the worker and the UI shows nothing happening.
    const batches: RouteEntry[][] = [];
    for (let i = 0; i < repos.length; i += 3) batches.push(repos.slice(i, i + 3));
    for (const batch of batches) {
      await Promise.all(
        batch.map(async (r) => {
          try {
            await triggerExtraction(r.id);
            ok.push(r.name);
            setQueued((prev) => [...prev, r.name]);
          } catch (e) {
            const msg = e instanceof Error ? e.message : "unknown";
            bad.push({ name: r.name, err: msg });
            setErrors((prev) => [...prev, { name: r.name, err: msg }]);
          }
        }),
      );
    }
    if (bad.length === 0 && ok.length > 0) {
      toast.success(`Queued extraction for ${ok.length} repo${ok.length === 1 ? "" : "s"}`);
    } else if (ok.length > 0) {
      toast.info(
        `Queued ${ok.length}, failed ${bad.length}. First failure: ${bad[0].err.slice(0, 80)}`,
      );
    } else {
      toast.error(`All ${bad.length} extractions failed: ${bad[0]?.err.slice(0, 80)}`);
    }
    runningRef.current = false;
    setRunning(false);
  // `repos` identity is stable via `repoIds` dedup in the effect below;
  // `runningRef` is a ref so it never changes identity.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repos]);

  // Auto-fire on first mount with this repo set so the user never
  // has to click "Extract N" — they just see the extractions
  // queueing immediately. Two layers of dedup:
  //   * ``autoFiredRef`` blocks re-fires within the SAME component
  //     instance (parent re-renders with same prop).
  //   * ``sessionStorage`` blocks re-fires ACROSS page refreshes for
  //     the same set of repos in the same tab. Without this, every
  //     refresh re-queued extractions and stacked toast popups.
  //
  // ``repoIds`` (stable string) is used instead of ``repos`` (fresh array
  // ref on every parent render) to prevent spurious re-runs. The key is
  // computed once here to avoid calling _autoExtractKey twice with
  // potentially different array refs inside the effect.
  useEffect(() => {
    if (autoFiredRef.current || !repoIds) return;
    const key = _AUTO_EXTRACT_DEDUP_KEY_PREFIX + repoIds.split(",").sort().join(",");
    const alreadyDone = (() => {
      if (typeof window === "undefined") return false;
      try { return Boolean(window.sessionStorage?.getItem(key)); } catch { return false; }
    })();
    if (alreadyDone) {
      autoFiredRef.current = true;          // suppress further attempts
      // Mark UI state as "already done" so the manual button hides.
      setQueued(repos.map((r) => r.name));
      return;
    }
    autoFiredRef.current = true;
    try { window.sessionStorage?.setItem(key, String(Date.now())); } catch { /* ignore */ }
    void extractAll();
  // `extractAll` is excluded: its identity changes when `repos` changes, but
  // `repoIds` is the stable semantic key — including `extractAll` would cause
  // the effect to re-run on every parent render (fresh array ref each time).
  // `autoFiredRef` guards against double-fire within an instance.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoIds]);

  const done = queued.length > 0 && !running;
  return (
    <div className="mt-2 rounded-md border border-accent/40 bg-accent-soft/30 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        {running ? (
          <Loader2
            className="h-3 w-3 shrink-0 animate-spin text-accent"
            strokeWidth={1.75}
          />
        ) : (
          <Sparkles
            className="h-3 w-3 shrink-0 text-accent"
            strokeWidth={1.75}
          />
        )}
        <p className="flex-1 text-[11.5px] text-fg-1">
          {queued.length === 0 && !running ? (
            <>
              Starting deep extraction on {repos.length} repo
              {repos.length === 1 ? "" : "s"}… (this takes a few minutes;
              answers will use whatever has finished by the time
              synthesis runs)
            </>
          ) : running ? (
            <>Queuing extractions… {queued.length}/{repos.length}</>
          ) : errors.length === 0 ? (
            <>
              Queued {queued.length} extraction
              {queued.length === 1 ? "" : "s"}. Track progress from Knowledge tab.
            </>
          ) : (
            <>
              Queued {queued.length}, {errors.length} failed.
            </>
          )}
        </p>
        {!done && (
          <button
            onClick={extractAll}
            disabled={running}
            className="inline-flex items-center gap-1 rounded-md border border-warn bg-warn-soft px-2 py-1 text-[11px] font-semibold text-warn hover:bg-warn hover:text-bg-0 disabled:opacity-50"
            title="Kick off deep extraction for the routed repos so future queries include per-repo agents"
          >
            {running ? (
              <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
            ) : (
              <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
            )}
            {running
              ? `Queuing ${queued.length}/${repos.length}…`
              : `Extract ${repos.length}`}
          </button>
        )}
      </div>
      {errors.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-[10.5px] text-err">
          {errors.slice(0, 3).map((e) => (
            <li key={e.name} className="truncate">
              <span className="font-mono">{e.name}</span>: {e.err}
            </li>
          ))}
          {errors.length > 3 && (
            <li className="text-fg-3">… {errors.length - 3} more</li>
          )}
        </ul>
      )}
    </div>
  );
}


function ActivityLog({ entries }: { entries: ActivityEntry[] }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Auto-scroll to latest on every new entry — if the user has
  // scrolled up manually, don't yank them back.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }, [entries.length]);

  if (entries.length === 0) return null;
  return (
    <details className="group">
      <summary className="flex cursor-pointer items-center justify-between gap-2 px-4 py-2 text-[11px] font-medium text-fg-2 hover:text-fg-0">
        <span className="flex items-center gap-1.5">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Activity log
          <span className="font-mono text-[10.5px] text-fg-3">
            ({entries.length})
          </span>
        </span>
        <span className="font-mono text-[10.5px] text-fg-3">
          live — newest at bottom
        </span>
      </summary>
      <div
        ref={scrollRef}
        className="max-h-[220px] overflow-y-auto border-t border-border bg-bg-0 px-4 py-2 font-mono text-[11px] leading-[1.6]"
      >
        {entries.map((e, i) => {
          const isMilestone = e.kind === "milestone";
          return (
            <div
              key={i}
              className={cn(
                "flex items-start gap-2",
                isMilestone && "border-l-2 border-accent pl-2 -ml-2 my-0.5 py-0.5",
              )}
            >
              <span className="shrink-0 text-fg-3 tabular-nums">
                {_formatTime(e.t)}
              </span>
              <span
                className={cn(
                  "shrink-0 uppercase text-[9.5px] tracking-wide",
                  e.kind === "status" && "text-fg-3",
                  e.kind === "agent" && "text-accent",
                  e.kind === "tool" && "text-info",
                  e.kind === "note" && "text-warn",
                  isMilestone && "text-accent font-semibold",
                )}
              >
                {e.kind === "milestone" ? "STAGE" : e.kind}
              </span>
              <span
                className={cn(
                  "min-w-0 flex-1 break-words",
                  isMilestone ? "text-fg-0 font-semibold" : "text-fg-1",
                )}
              >
                {e.message}
              </span>
            </div>
          );
        })}
      </div>
    </details>
  );
}


function _formatTime(t: number): string {
  const d = new Date(t);
  return (
    String(d.getHours()).padStart(2, "0") +
    ":" +
    String(d.getMinutes()).padStart(2, "0") +
    ":" +
    String(d.getSeconds()).padStart(2, "0")
  );
}

/* ── Subcomponents ─────────────────────────────────────────────────── */

/**
 * Per-repo status chips rendered under the Scope pipeline row.
 *
 * Shows the FULL discovered repo set with its index state — the
 * router's top-K is no longer the cap, so this row is where the user
 * sees breadth. Color encodes the actionable state (green = ready to
 * answer, amber = manifest_only / auto_registered will trigger
 * extraction, red = clone failed).
 *
 * Long lists collapse to ``STATUS_CHIPS_INITIAL``; click "+N more"
 * to expand.
 */
const STATUS_CHIPS_INITIAL = 24;

function RepoStatusChips({
  repos,
  summary,
}: {
  repos: RepoStatusEntry[];
  summary: RepoStatusSummary | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? repos : repos.slice(0, STATUS_CHIPS_INITIAL);
  const remaining = repos.length - visible.length;
  return (
    <div className="mt-1 ml-[80px]">
      {summary && (
        <div className="mb-1 text-[10.5px] font-mono text-fg-3">
          {summary.deep_indexed} deep-indexed
          {summary.manifest_only ? ` · ${summary.manifest_only} manifest-only` : ""}
          {summary.auto_registered ? ` · ${summary.auto_registered} auto-registered` : ""}
          {summary.not_cloned ? ` · ${summary.not_cloned} not-cloned` : ""}
        </div>
      )}
      <div className="flex flex-wrap gap-1">
        {visible.map((r) => (
          <span
            key={r.repo_id}
            title={
              r.clone_error
                ? `Clone failed: ${r.clone_error}`
                : r.status === "deep_indexed"
                  ? `${r.code_entity_count} extracted entities`
                  : r.status === "manifest_only"
                    ? "No extracted entities yet — auto-extraction will trigger."
                    : r.status === "auto_registered"
                      ? "Just discovered in the org catalog this turn — extraction will trigger."
                      : ""
            }
            className={cn(
              "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10.5px] font-medium",
              r.status === "deep_indexed" &&
                "border-ok/40 bg-ok-soft text-ok",
              r.status === "manifest_only" &&
                "border-warn/40 bg-warn-soft text-warn",
              r.status === "auto_registered" &&
                "border-accent/40 bg-accent-soft text-accent",
              r.status === "not_cloned" &&
                "border-err/40 bg-err-soft text-err",
            )}
          >
            <span className="font-mono">{r.repo_name}</span>
            {r.status === "deep_indexed" && (
              <span className="font-mono text-[9.5px] opacity-70">
                {r.code_entity_count}
              </span>
            )}
            {r.status === "auto_registered" && (
              <span className="text-[9.5px] uppercase tracking-wide">new</span>
            )}
            {r.status === "not_cloned" && (
              <XCircle className="h-3 w-3" strokeWidth={2} />
            )}
          </span>
        ))}
        {remaining > 0 && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="inline-flex items-center rounded-md border border-border bg-bg-1 px-1.5 py-0.5 text-[10.5px] text-fg-2 hover:text-fg-0"
          >
            +{remaining} more
          </button>
        )}
      </div>
    </div>
  );
}


function PipelineRow({
  label,
  done,
  inProgress,
  detail,
  children,
}: {
  label: string;
  done: boolean;
  inProgress?: boolean;
  detail?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="px-4 py-2">
      <div className="flex items-center gap-2">
        <span className="w-[72px] shrink-0 text-[11.5px] font-medium text-fg-2">
          {label}
        </span>
        {done ? (
          <CheckCircle2 className="h-3.5 w-3.5 text-ok" strokeWidth={1.75} />
        ) : inProgress ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" strokeWidth={1.75} />
        ) : (
          <Circle className="h-3.5 w-3.5 text-fg-3" strokeWidth={1.75} />
        )}
        {detail && (
          <span className="text-[11.5px] text-fg-1">{detail}</span>
        )}
      </div>
      {children && <div className="mt-2">{children}</div>}
    </div>
  );
}

function _countAgents(entries: RepoRosterEntry[]): number {
  return entries.reduce(
    (n, e) => n + AGENT_TYPES.filter((t) => e.enabledAgents[t]).length,
    0,
  );
}

const AGENT_COLUMNS: { key: AgentType; label: string; icon: "code" | "jira" | "confluence" | "github" | "slack" }[] = [
  { key: "code", label: "Code", icon: "code" },
  { key: "jira", label: "Jira", icon: "jira" },
  { key: "confluence", label: "Docs", icon: "confluence" },
  { key: "github", label: "GitHub", icon: "github" },
  { key: "slack", label: "Slack", icon: "slack" },
];

/** 2D grid: rows = repos, columns = agent types. Each cell lights up as
 * that repo's agent of that type transitions through states. */
function AgentGrid({
  roster,
  conversationId,
}: {
  roster: RepoRosterEntry[];
  conversationId: string;
}) {
  return (
    <div className="overflow-hidden rounded-md border border-border bg-bg-0">
      <table className="w-full text-[11.5px]">
        <thead className="bg-bg-2">
          <tr>
            <th className="px-3 py-1.5 text-left font-medium text-fg-2">
              Repo
            </th>
            <th className="w-[70px] px-2 py-1.5 text-left font-medium text-fg-2">
              Squad
            </th>
            <th className="w-[64px] px-2 py-1.5 text-left font-medium text-fg-2">
              Route
            </th>
            {AGENT_COLUMNS.map((c) => (
              <th
                key={c.key}
                className="w-[110px] px-2 py-1.5 text-left font-medium text-fg-2"
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {roster.map((r) => (
            <tr key={r.repoId}>
              <td className="px-3 py-1.5 font-medium text-fg-0">
                <div
                  className="flex max-w-[220px] items-center gap-1 truncate"
                  title={
                    r.isPrimary
                      ? `${r.repoName} — primary repo for ${r.squad ?? "this team"}`
                      : r.repoName
                  }
                >
                  {r.isPrimary && (
                    <Sparkles
                      className="h-3 w-3 shrink-0 text-accent"
                      strokeWidth={2}
                      aria-label="primary"
                    />
                  )}
                  <span className="truncate">{r.repoName}</span>
                </div>
              </td>
              <td className="px-2 py-1.5 font-mono text-fg-2">
                {r.squad ?? "—"}
              </td>
              <td className="px-2 py-1.5">
                <RelevanceBadge r={r.relevance} />
              </td>
              {AGENT_COLUMNS.map((c) => (
                <td key={c.key} className="px-2 py-1.5">
                  <AgentCell
                    enabled={r.enabledAgents[c.key] === true}
                    state={r.agentStates[c.key]}
                    repoId={r.repoId}
                    agentKey={c.key}
                    conversationId={conversationId}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RelevanceBadge({ r }: { r: string }) {
  const tone =
    r === "high"
      ? "text-ok"
      : r === "medium"
        ? "text-accent"
        : r === "low"
          ? "text-fg-2"
          : "text-fg-3";
  return <span className={cn("font-mono text-[10.5px]", tone)}>{r}</span>;
}

/** External agents the user can cancel mid-flight. The "code"
 *  agent is intentionally not cancellable from the chip — code is
 *  too central to lose without abandoning the answer; conversation-
 *  level cancel covers that case. */
const _CANCELLABLE_AGENTS: ReadonlyArray<AgentType> = [
  "jira", "confluence", "github", "slack",
];

function AgentCell({
  enabled,
  state,
  repoId,
  agentKey,
  conversationId,
}: {
  enabled: boolean;
  state?: RepoRosterEntry["agentStates"][AgentType];
  repoId: string;
  agentKey: AgentType;
  conversationId: string;
}) {
  const [cancelling, setCancelling] = useState(false);
  if (!enabled) {
    return (
      <span className="inline-flex items-center gap-1 text-[10.5px] text-fg-3">
        <span className="h-1 w-1 rounded-full bg-bg-3" />
        n/a
      </span>
    );
  }
  const status = state?.status ?? "pending";
  const icon =
    status === "pending" ? (
      <Circle className="h-2.5 w-2.5 text-fg-3" strokeWidth={1.75} />
    ) : status === "running" ? (
      <Loader2 className="h-2.5 w-2.5 animate-spin text-accent" strokeWidth={2} />
    ) : status === "done" ? (
      <CheckCircle2 className="h-2.5 w-2.5 text-ok" strokeWidth={2} />
    ) : (
      <XCircle className="h-2.5 w-2.5 text-err" strokeWidth={2} />
    );
  const label =
    status === "pending"
      ? "queued"
      : status === "running"
        ? "running"
        : status === "done"
          ? state?.files !== undefined
            ? `${state.files} files`
            : state?.hits !== undefined
              ? `${state.hits} hits`
              : (state?.relevance ?? "done")
          : "err";
  const canCancel =
    status === "running"
    && conversationId
    && repoId
    && _CANCELLABLE_AGENTS.includes(agentKey)
    && !cancelling;
  const onCancel = async () => {
    setCancelling(true);
    try {
      await cancelAgent(conversationId, `${repoId}:${agentKey}`);
      // The SSE stream will deliver a synthetic agent_done with
      // error="cancelled by user" — UI updates from that, not from
      // this response. Toast lets the user know the click landed.
      toast.success(`${agentKey} agent cancelled`);
    } catch (e: unknown) {
      toast.error(`Failed to cancel ${agentKey}: ${describeError(e)}`);
      setCancelling(false);
    }
  };
  return (
    <span
      className={cn(
        "group inline-flex items-center gap-1 text-[10.5px]",
        status === "error" ? "text-err" : "text-fg-1",
      )}
      title={state?.error ?? undefined}
    >
      {icon}
      {label}
      {canCancel && (
        <button
          type="button"
          onClick={onCancel}
          aria-label={`Cancel ${agentKey} agent`}
          title={`Stop the ${agentKey} agent`}
          className="ml-0.5 inline-flex h-3 w-3 shrink-0 items-center justify-center rounded text-fg-3 opacity-0 hover:bg-err-soft hover:text-err group-hover:opacity-100 focus:opacity-100"
        >
          <XCircle className="h-2.5 w-2.5" strokeWidth={2} />
        </button>
      )}
    </span>
  );
}

function AgentCard({ agent }: { agent: AgentState }) {
  const Icon = agent.kind === "external" ? Globe : GitBranch;
  const statusLabel =
    agent.status === "pending"
      ? "waiting"
      : agent.status === "running"
        ? agent.kind === "external"
          ? "searching…"
          : "researching…"
        : agent.status === "done"
          ? agent.kind === "external"
            ? `${agent.findingsCount ?? 0} findings`
            : `${agent.fileCount ?? 0} files`
          : "error";

  const border = {
    pending: "border-border",
    running: "border-accent",
    done: "border-ok/40",
    error: "border-err/40",
  }[agent.status];

  const bg = {
    pending: "bg-bg-1",
    running: "bg-accent-soft",
    done: "bg-bg-1",
    error: "bg-err-soft",
  }[agent.status];

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-md border px-2.5 py-1.5",
        border,
        bg,
      )}
      title={agent.error ?? statusLabel}
    >
      <Icon className="h-3 w-3 shrink-0 text-fg-2" strokeWidth={1.75} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[11.5px] font-medium text-fg-0">
          {agent.name}
        </div>
        <div
          className={cn(
            "flex items-center gap-1 text-[10.5px]",
            agent.status === "error" ? "text-err" : "text-fg-2",
          )}
        >
          {agent.status === "running" && (
            <Loader2 className="h-2.5 w-2.5 animate-spin" strokeWidth={2} />
          )}
          {agent.status === "done" && (
            <CheckCircle2 className="h-2.5 w-2.5 text-ok" strokeWidth={2} />
          )}
          {agent.status === "error" && (
            <XCircle className="h-2.5 w-2.5" strokeWidth={2} />
          )}
          {agent.relevance && agent.status !== "error" && (
            <span className="font-mono">{agent.relevance}</span>
          )}
          <span className="truncate">{statusLabel}</span>
        </div>
      </div>
    </div>
  );
}

function CatalogExpansionBanner({
  expansion,
  autoRegistered,
}: {
  expansion: CatalogExpansion;
  autoRegistered: AutoRegistered[];
}) {
  return (
    <div className="rounded-md border border-accent/40 bg-accent-soft px-3 py-2">
      <div className="flex items-start gap-1.5">
        <Sparkles
          className="mt-0.5 h-3 w-3 shrink-0 text-accent"
          strokeWidth={1.75}
        />
        <div className="min-w-0 flex-1">
          <div className="text-[11.5px] font-medium text-accent">
            Skim-searching the org catalog
          </div>
          <div className="mt-0.5 text-[11px] text-fg-1">{expansion.message}</div>
          {expansion.repos.length > 0 && (
            <ul className="mt-1 flex flex-wrap gap-1">
              {expansion.repos.slice(0, 12).map((r) => (
                <li
                  key={`${r.owner}/${r.name}`}
                  className="inline-flex items-center gap-1 rounded bg-bg-1 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2"
                >
                  {r.name}
                </li>
              ))}
            </ul>
          )}
          {autoRegistered.length > 0 && (
            <div className="mt-2 text-[10.5px] text-fg-2">
              Auto-registered {autoRegistered.length} for this turn — will
              shallow-clone + research on-the-fly. Expect +30-60s latency.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function AutoExtractingBanner({
  autoExtracting,
}: {
  autoExtracting: AutoExtracting;
}) {
  const waitStatus = autoExtracting.waitStatus ?? {};
  const completed = Object.values(waitStatus).filter(
    (s) => s.state === "completed",
  ).length;
  const finalized = Object.values(waitStatus).filter(
    (s) => s.state !== "waiting",
  ).length;
  const total = autoExtracting.repos.length;
  const isWaiting = autoExtracting.waitSeconds !== undefined && finalized < total;
  return (
    <div className="rounded-md border border-accent/40 bg-accent-soft px-3 py-2">
      <div className="flex items-start gap-1.5">
        {isWaiting ? (
          <Loader2
            className="mt-0.5 h-3 w-3 shrink-0 animate-spin text-accent"
            strokeWidth={1.75}
          />
        ) : (
          <Sparkles
            className="mt-0.5 h-3 w-3 shrink-0 text-accent"
            strokeWidth={1.75}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="text-[11.5px] font-medium text-accent">
            {autoExtracting.waitSeconds !== undefined
              ? `Waiting for first-touch extraction (≤${autoExtracting.waitSeconds}s)`
              : "Starting deep extraction in the background"}
          </div>
          <div className="mt-0.5 text-[11px] text-fg-1">
            {autoExtracting.message}
            {autoExtracting.waitSeconds !== undefined && total > 0 && (
              <span className="ml-1 font-mono text-fg-2">
                · {finalized}/{total} resolved
                {completed > 0 ? ` · ${completed} promoted` : ""}
              </span>
            )}
          </div>
          {autoExtracting.repos.length > 0 && (
            <ul className="mt-1.5 flex flex-wrap gap-1">
              {autoExtracting.repos.slice(0, 12).map((r) => {
                const s = waitStatus[r.repo_id]?.state ?? "waiting";
                const cls =
                  s === "completed"
                    ? "bg-ok-soft text-ok"
                    : s === "no_entities"
                      ? "bg-bg-1 text-fg-3"
                      : s === "failed"
                        ? "bg-err-soft text-err"
                        : s === "timeout"
                          ? "bg-warn-soft text-warn"
                          : "bg-bg-1 text-fg-2";
                const isWaiting = s === "waiting";
                const onStop = async () => {
                  try {
                    await cancelExtractionTask(r.task_id);
                    toast.info(`Stopped extraction for ${r.repo_name}`);
                  } catch (err) {
                    toast.error(
                      `Couldn't stop ${r.repo_name}: ${describeError(err)}`,
                    );
                  }
                };
                return (
                  <li
                    key={r.repo_id}
                    className={cn(
                      "group inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10.5px]",
                      cls,
                    )}
                    title={waitStatus[r.repo_id]?.message ?? `task ${r.task_id.slice(0, 8)}…`}
                  >
                    {s === "completed" && <CheckCircle2 className="h-2.5 w-2.5" strokeWidth={2} />}
                    {s === "failed" && <XCircle className="h-2.5 w-2.5" strokeWidth={2} />}
                    {s === "timeout" && <AlertTriangle className="h-2.5 w-2.5" strokeWidth={2} />}
                    {s === "waiting" && <Loader2 className="h-2.5 w-2.5 animate-spin" strokeWidth={2} />}
                    {r.repo_name}
                    {waitStatus[r.repo_id]?.entities !== undefined && (
                      <span className="text-[10px] opacity-75">
                        · {waitStatus[r.repo_id]?.entities} ents
                      </span>
                    )}
                    {/* Per-repo Stop button — only meaningful while
                        the extraction is still in flight. Hover-only
                        so the chip stays compact at rest. */}
                    {isWaiting && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          void onStop();
                        }}
                        className="ml-0.5 hidden h-3 w-3 items-center justify-center rounded text-fg-3 hover:bg-err-soft hover:text-err group-hover:flex"
                        aria-label={`Stop extraction for ${r.repo_name}`}
                        title="Stop this extraction"
                      >
                        <XCircle className="h-2.5 w-2.5" strokeWidth={2} />
                      </button>
                    )}
                  </li>
                );
              })}
              {autoExtracting.repos.length > 12 && (
                <li className="text-[10.5px] text-fg-3">
                  +{autoExtracting.repos.length - 12} more
                </li>
              )}
            </ul>
          )}
          <div className="mt-1.5 text-[10.5px] text-fg-3">
            {autoExtracting.waitSeconds !== undefined
              ? "Repos that finish in time are promoted to deep-indexed for this turn; the rest stay in the background."
              : "Deeper answers available on follow-up queries once extraction completes (~5 min per repo)."}
          </div>
        </div>
      </div>
    </div>
  );
}

function PlanBanner({ plan }: { plan: OrchestrationPlan }) {
  const intentLabels: Record<OrchestrationPlan["intent"], string> = {
    architectural: "Architectural map",
    impact_radius: "Impact radius",
    debug: "Debug",
    discovery: "Discovery",
    feature_estimate: "Feature estimate",
  };
  const w = plan.resolved_walk;
  return (
    <div className="rounded-md border border-accent/40 bg-accent-soft/40 px-3 py-2">
      <div className="flex items-start gap-2">
        <Sparkles
          className="mt-0.5 h-3 w-3 shrink-0 text-accent"
          strokeWidth={1.75}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-1.5">
            <span className="text-[11.5px] font-semibold text-accent">
              {intentLabels[plan.intent] ?? plan.intent}
            </span>
            <span className="font-mono text-[10.5px] text-fg-3">
              · depth {w.walk_depth} · floor {w.confidence_floor.toFixed(2)} ·{" "}
              {w.edge_directions}
            </span>
          </div>
          {plan.reasoning && (
            <p className="mt-0.5 text-[11.5px] text-fg-1">{plan.reasoning}</p>
          )}
        </div>
      </div>
    </div>
  );
}


function BundleReuseBanner({ reuse }: { reuse: BundleReuse }) {
  const hasExtension = reuse.extensionRepos && reuse.extensionRepos.length > 0;
  const isShortcut = reuse.shortcut === "clarify_from_bundle";
  // Shortcut path uses the accent palette + Sparkles to signal "this is
  // the fast path"; the regular continuation banner stays in info tone.
  const palette = isShortcut
    ? {
        border: "border-accent/40",
        bg: "bg-accent-soft",
        iconColor: "text-accent",
        title: "Clarification shortcut",
        Icon: Sparkles,
      }
    : {
        border: "border-info/40",
        bg: "bg-info-soft",
        iconColor: "text-info",
        title: "Continuing from prior research",
        Icon: RefreshCcw,
      };
  const { Icon } = palette;
  return (
    <div className={cn("rounded-md border px-3 py-2", palette.border, palette.bg)}>
      <div className="flex items-start gap-1.5">
        <Icon
          className={cn("mt-0.5 h-3 w-3 shrink-0", palette.iconColor)}
          strokeWidth={1.75}
        />
        <div className="min-w-0 flex-1">
          <div className={cn("text-[11.5px] font-medium", palette.iconColor)}>
            {palette.title}
          </div>
          <div className="mt-0.5 text-[11px] text-fg-1">{reuse.message}</div>
          {reuse.priorRepos.length > 0 && (
            <div className="mt-1 text-[10.5px] text-fg-2">
              <span className="text-fg-3">Prior repos: </span>
              <span className="font-mono">{reuse.priorRepos.slice(0, 6).join(", ")}</span>
              {reuse.priorRepos.length > 6 && (
                <span className="text-fg-3"> · +{reuse.priorRepos.length - 6}</span>
              )}
            </div>
          )}
          {hasExtension && reuse.extensionRepos && (
            <div className="mt-0.5 text-[10.5px] text-fg-2">
              <span className="text-fg-3">Extending into: </span>
              <span className="font-mono">{reuse.extensionRepos.join(", ")}</span>
            </div>
          )}
          {isShortcut && (
            <div className="mt-1 text-[10.5px] text-fg-3">
              No fresh fan-out · Haiku call only · prior sources reused.
            </div>
          )}
          {reuse.hydration && reuse.hydration.totalEntities > 0 && (
            <div className="mt-1 text-[10.5px] text-fg-3">
              <CheckCircle2
                className="mr-0.5 inline h-2.5 w-2.5 text-ok"
                strokeWidth={2}
              />
              Hydrated{" "}
              <span className="font-mono text-fg-2">
                {reuse.hydration.totalEntities} entities
              </span>
              {" + "}
              <span className="font-mono text-fg-2">
                {reuse.hydration.totalEdges} edges
              </span>
              {" from "}
              <span className="font-mono text-fg-2">
                {reuse.hydration.repoCount} repo(s)
              </span>
              {reuse.hydration.missingUrns > 0 && (
                <span className="text-fg-3">
                  {" · "}
                  <span className="text-warn">
                    {reuse.hydration.missingUrns} URN(s) gone
                  </span>
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StalenessBanner({ repos }: { repos: StalenessEntry[] }) {
  const onRefresh = async (repoId: string, repoName: string) => {
    try {
      await triggerExtraction(repoId, false);
      toast.success(`Queued refresh for ${repoName}`);
    } catch (err) {
      toast.error(
        `Failed: ${describeError(err)}`,
      );
    }
  };

  const onRefreshAll = async () => {
    try {
      await Promise.all(repos.map((r) => triggerExtraction(r.id, false)));
      toast.success(`Queued refresh for ${repos.length} repo${repos.length === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(
        `Some refreshes failed: ${describeError(err)}`,
      );
    }
  };

  return (
    <div className="rounded-md border border-warn-soft bg-warn-soft px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-start gap-1.5">
          <AlertTriangle
            className="mt-0.5 h-3 w-3 shrink-0 text-warn"
            strokeWidth={1.75}
          />
          <div>
            <div className="text-[11.5px] font-medium text-warn">
              {repos.length} repo{repos.length === 1 ? " has" : "s have"} newer commits
            </div>
            <div className="text-[11px] text-fg-1">
              Answer is based on the latest cloned code; extracted metadata
              may be behind. Refresh to re-run incremental extraction.
            </div>
          </div>
        </div>
        <button
          onClick={onRefreshAll}
          className="inline-flex items-center gap-1 rounded border border-warn/40 bg-bg-1 px-2 py-1 text-[11px] font-medium text-warn hover:bg-warn-soft"
        >
          <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
          Refresh all
        </button>
      </div>
      <ul className="mt-2 space-y-0.5">
        {repos.map((r) => (
          <li
            key={r.id}
            className="flex items-center gap-2 font-mono text-[10.5px] text-fg-2"
          >
            <Book className="h-2.5 w-2.5" strokeWidth={1.75} />
            <span className="truncate text-fg-1">{r.name}</span>
            <span className="text-fg-3">
              {r.last_extracted_sha} → {r.remote_head}
            </span>
            <button
              onClick={() => onRefresh(r.id, r.name)}
              className="ml-auto rounded border border-border px-1.5 text-[10px] hover:border-warn hover:text-warn"
              title={`Refresh ${r.name}`}
            >
              refresh
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
