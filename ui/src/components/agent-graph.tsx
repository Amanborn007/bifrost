/**
 * AgentGraph — live SVG visualization of the research fan-out.
 *
 * Renders the same data as ``AgentGrid`` but as a tiered graph
 * (User question → Router → per-repo nodes → per-agent leaves → Synthesis)
 * so the engineer watching a chat see the architecture of the research,
 * not just a 2D table. Hand-rolled with SVG + Tailwind — no new deps,
 * no DAG layout library — because the topology is fixed and shallow.
 *
 * Layout (vertical tiers, top-to-bottom):
 *   tier 0:  [User question]                       — turn input
 *   tier 1:        [Router]                        — Haiku ranks repos
 *   tier 2:  [repo₁]  [repo₂]  …  [repoN]          — selected repos
 *   tier 3:  [code][jira]…  [code][jira]…          — per-repo agent leaves
 *   tier 4:  [Org-wide external]                   — separate column
 *   tier 5:        [Synthesis]                     — collects everything
 *
 * Each node carries a state — pending / running / done / error —
 * driven entirely by the SSE-derived ``ResearchState`` already in
 * the store. No new types added; no new events emitted from the
 * coordinator — this is purely a presentational alternative to
 * the existing AgentGrid.
 */

import { useMemo } from "react";
import {
  Bot,
  Brain,
  CheckCircle2,
  CircleDot,
  GitBranch,
  Globe,
  Layers,
  MessageSquare,
  Sparkles,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  AgentType,
  RepoRosterEntry,
  ResearchState,
} from "@/components/research-progress";

const AGENT_TYPES: AgentType[] = ["code", "jira", "confluence", "github", "slack"];
type Status = "pending" | "running" | "done" | "error" | "skipped";

interface NodeRenderInfo {
  id: string;
  x: number;
  y: number;
  label: string;
  sublabel?: string;
  status: Status;
  shape: "pill" | "circle";
  primary?: boolean;
  icon?: React.ComponentType<{ className?: string; strokeWidth?: number }>;
}

interface EdgeRenderInfo {
  from: string;
  to: string;
  active: boolean;
}

const NODE_W = 130;
const NODE_H = 28;
const TIER_GAP_Y = 70;

export function AgentGraph({ state }: { state: ResearchState }) {
  const rosterEntries = useMemo(
    () => Object.values(state.roster),
    [state.roster],
  );
  const externalAgent = state.agents
    ? Object.values(state.agents).find((a) => a.kind === "external")
    : undefined;

  // Compute layout. We derive node positions from the data so the
  // graph reflows naturally when more repos arrive mid-stream.
  const { nodes, edges, width, height } = useMemo(() => {
    const _nodes: NodeRenderInfo[] = [];
    const _edges: EdgeRenderInfo[] = [];

    const repoCount = Math.max(rosterEntries.length, 1);
    // Width = max(repo-tier width, agent-tier width). Each repo expands
    // to AGENT_TYPES.length leaves below it; total leaf width = sum
    // across all repos. Use the larger so the SVG canvas fits.
    const leafGap = 80;            // horizontal stride per agent leaf
    const leafWidth = leafGap;
    const totalLeafWidth = repoCount * AGENT_TYPES.length * leafWidth;
    const repoTierWidth = repoCount * (NODE_W + 24);
    const _width = Math.max(720, totalLeafWidth, repoTierWidth) + 80;

    const cx = _width / 2;

    // tier 0: question
    _nodes.push({
      id: "question",
      x: cx,
      y: 30,
      label: "User question",
      status: rosterEntries.length || state.scope.length > 0 ? "done" : "pending",
      shape: "pill",
      icon: MessageSquare,
    });

    // tier 1: router
    _nodes.push({
      id: "router",
      x: cx,
      y: 30 + TIER_GAP_Y,
      label: "Router",
      sublabel: "Haiku ranks repos",
      status:
        state.route.length > 0
          ? "done"
          : state.scope.length > 0
            ? "running"
            : "pending",
      shape: "pill",
      icon: Brain,
    });
    _edges.push({
      from: "question",
      to: "router",
      active: state.scope.length > 0,
    });

    // tier 2: repos
    const repoY = 30 + 2 * TIER_GAP_Y;
    const repoStride = _width / (repoCount + 1);
    rosterEntries.forEach((r, i) => {
      const x = repoStride * (i + 1);
      const status = repoStatusFromAgents(r);
      _nodes.push({
        id: `repo:${r.repoId}`,
        x,
        y: repoY,
        label: r.repoName,
        sublabel: r.squad ?? undefined,
        status,
        shape: "pill",
        primary: r.isPrimary,
        icon: GitBranch,
      });
      _edges.push({
        from: "router",
        to: `repo:${r.repoId}`,
        active: status !== "pending",
      });
    });

    // tier 3: per-repo agents
    const agentY = repoY + TIER_GAP_Y;
    rosterEntries.forEach((r, ri) => {
      const repoX = repoStride * (ri + 1);
      const enabled = AGENT_TYPES.filter((t) => r.enabledAgents[t]);
      enabled.forEach((agent, ai) => {
        const span = (enabled.length - 1) * 32;
        const x = repoX - span / 2 + ai * 32;
        const cell = r.agentStates[agent];
        const status: Status = cell?.status ?? "pending";
        _nodes.push({
          id: `agent:${r.repoId}:${agent}`,
          x,
          y: agentY,
          label: agent,
          status,
          shape: "circle",
          icon: agentIcon(agent),
        });
        _edges.push({
          from: `repo:${r.repoId}`,
          to: `agent:${r.repoId}:${agent}`,
          active: status !== "pending",
        });
      });
    });

    // tier 4 (offset right): external agent — runs in parallel with the
    // per-repo fan-out, attached to the router rather than to a repo.
    if (externalAgent) {
      _nodes.push({
        id: "external",
        x: _width - 80,
        y: repoY,
        label: "Org-wide",
        sublabel: "Confluence + Jira",
        status: externalAgent.status as Status,
        shape: "pill",
        icon: Globe,
      });
      _edges.push({
        from: "router",
        to: "external",
        active: externalAgent.status !== "pending",
      });
    }

    // tier 5: synthesis
    const synthY = agentY + TIER_GAP_Y;
    _nodes.push({
      id: "synthesis",
      x: cx,
      y: synthY,
      label: "Synthesis",
      sublabel: state.synthesisStarted ? "writing answer…" : "waiting for agents",
      status: state.synthesisStarted ? "running" : "pending",
      shape: "pill",
      icon: Sparkles,
    });
    rosterEntries.forEach((r) => {
      _edges.push({
        from: `repo:${r.repoId}`,
        to: "synthesis",
        active: state.synthesisStarted,
      });
    });
    if (externalAgent) {
      _edges.push({
        from: "external",
        to: "synthesis",
        active: state.synthesisStarted,
      });
    }

    return {
      nodes: _nodes,
      edges: _edges,
      width: _width,
      height: synthY + 50,
    };
  }, [rosterEntries, externalAgent, state.scope.length, state.route.length, state.synthesisStarted]);

  const nodeById = useMemo(
    () => Object.fromEntries(nodes.map((n) => [n.id, n])),
    [nodes],
  );

  if (rosterEntries.length === 0 && !externalAgent) {
    return (
      <div className="rounded-md border border-border bg-bg-0 p-6 text-center text-[12px] text-fg-3">
        Graph appears once the router picks repos for this turn.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-md border border-border bg-bg-0">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="block min-w-full"
        style={{ height: `${height}px` }}
        role="img"
        aria-label="Live agent fan-out graph"
      >
        {/* edges first so they render under the nodes */}
        <g>
          {edges.map((e, i) => {
            const a = nodeById[e.from];
            const b = nodeById[e.to];
            if (!a || !b) return null;
            return (
              <EdgePath
                key={i}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                active={e.active}
              />
            );
          })}
        </g>
        <g>
          {nodes.map((n) => (
            <GraphNode key={n.id} node={n} />
          ))}
        </g>
      </svg>
    </div>
  );
}

function repoStatusFromAgents(r: RepoRosterEntry): Status {
  const states = AGENT_TYPES
    .filter((t) => r.enabledAgents[t])
    .map((t) => r.agentStates[t]?.status);
  if (states.length === 0) return "skipped";
  if (states.every((s) => s === "done")) return "done";
  if (states.some((s) => s === "error")) return "error";
  if (states.some((s) => s === "running")) return "running";
  if (states.every((s) => s === "pending" || s == null)) return "pending";
  return "running";
}

function agentIcon(t: AgentType) {
  switch (t) {
    case "code":
      return Layers;
    case "jira":
    case "github":
      return Bot;
    case "confluence":
    case "slack":
      return MessageSquare;
    default:
      return Bot;
  }
}

function statusColors(status: Status): {
  fill: string;
  stroke: string;
  text: string;
} {
  switch (status) {
    case "done":
      return { fill: "fill-ok-soft", stroke: "stroke-ok", text: "text-ok" };
    case "running":
      return { fill: "fill-accent-soft", stroke: "stroke-accent", text: "text-accent" };
    case "error":
      return { fill: "fill-err-soft", stroke: "stroke-err", text: "text-err" };
    case "skipped":
      return { fill: "fill-bg-1", stroke: "stroke-border", text: "text-fg-3" };
    default:
      return { fill: "fill-bg-1", stroke: "stroke-border", text: "text-fg-2" };
  }
}

function GraphNode({ node }: { node: NodeRenderInfo }) {
  const colors = statusColors(node.status);
  const Icon = node.icon;
  if (node.shape === "circle") {
    return (
      <g>
        <circle
          cx={node.x}
          cy={node.y}
          r={14}
          className={cn(
            colors.fill,
            colors.stroke,
            "stroke-[1.5]",
            node.status === "running" && "animate-pulse",
          )}
        />
        {Icon ? (
          <foreignObject
            x={node.x - 7}
            y={node.y - 7}
            width={14}
            height={14}
            className={cn(colors.text)}
          >
            <Icon className="h-3.5 w-3.5" strokeWidth={1.75} />
          </foreignObject>
        ) : null}
        <text
          x={node.x}
          y={node.y + 28}
          textAnchor="middle"
          className={cn("font-mono text-[10px]", colors.text)}
        >
          {node.label}
        </text>
        {/* status overlay */}
        {node.status === "done" && (
          <CheckCircle2
            className="text-ok"
            x={node.x + 8}
            y={node.y - 18}
            width={10}
            height={10}
            strokeWidth={2.5}
          />
        )}
        {node.status === "error" && (
          <XCircle
            className="text-err"
            x={node.x + 8}
            y={node.y - 18}
            width={10}
            height={10}
            strokeWidth={2.5}
          />
        )}
      </g>
    );
  }
  // pill
  const w = NODE_W;
  const h = NODE_H;
  return (
    <g>
      <rect
        x={node.x - w / 2}
        y={node.y - h / 2}
        width={w}
        height={h}
        rx={h / 2}
        className={cn(
          colors.fill,
          colors.stroke,
          "stroke-[1.5]",
          node.status === "running" && "animate-pulse",
        )}
      />
      <foreignObject
        x={node.x - w / 2 + 8}
        y={node.y - h / 2}
        width={w - 16}
        height={h}
      >
        <div
          className={cn(
            "flex h-full items-center gap-1 truncate text-[11px] font-medium",
            colors.text,
          )}
        >
          {node.primary ? (
            <Sparkles className="h-3 w-3 shrink-0 text-accent" strokeWidth={2} />
          ) : Icon ? (
            <Icon className="h-3 w-3 shrink-0" strokeWidth={1.75} />
          ) : (
            <CircleDot className="h-3 w-3 shrink-0" strokeWidth={1.75} />
          )}
          <span className="truncate">{node.label}</span>
        </div>
      </foreignObject>
      {node.sublabel && (
        <text
          x={node.x}
          y={node.y + h / 2 + 12}
          textAnchor="middle"
          className="font-mono text-[9.5px] fill-fg-3"
        >
          {node.sublabel}
        </text>
      )}
    </g>
  );
}

function EdgePath({
  x1,
  y1,
  x2,
  y2,
  active,
}: {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  active: boolean;
}) {
  // Smooth cubic-bezier path between (x1,y1) and (x2,y2). Control
  // points pull vertically so the curves arc downward through the
  // tier gap rather than zig-zagging.
  const midY = (y1 + y2) / 2;
  const d = `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
  return (
    <path
      d={d}
      fill="none"
      className={cn(
        "transition-all duration-300",
        active ? "stroke-accent/60" : "stroke-border",
      )}
      strokeWidth={active ? 1.5 : 1}
    />
  );
}
