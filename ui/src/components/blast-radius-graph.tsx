/**
 * BlastRadiusGraph — radial visualization of which repos contributed
 * to the answer + at what confidence.
 *
 * Driven by SSE-derived state already in `ResearchState`:
 *   * `statusByRepo` — per-repo classification (deep_indexed /
 *     manifest_only / not_cloned / auto_registered)
 *   * `route` — Haiku's per-repo relevance score (high/med/low/none)
 *   * `subgraphStats` — master-subgraph cross-repo edge count +
 *     repo footprint
 *
 * Layout:
 *   * Center node = the question
 *   * Ring 1 (closest) = high-relevance repos
 *   * Ring 2          = medium-relevance
 *   * Ring 3 (farthest) = low / none / manifest-only / auto-registered
 *
 * Color encodes the **status** chip (the actionable signal),
 * NOT the relevance — green = ready to answer, amber = will trigger
 * extraction, red = clone failed. The radial DISTANCE encodes
 * relevance, so users see "what was used" (color) and "how relevant
 * each was" (distance) at once.
 *
 * Hand-rolled SVG, no DAG/force layout library — radial positions
 * are deterministic from the input ordering, so the graph is stable
 * across re-renders.
 */

import { useMemo, useState } from "react";
import { MessageSquare, Sparkles } from "lucide-react";
import type {
  RepoStatusEntry,
  ResearchState,
  RouteEntry,
  SubgraphStats,
} from "@/components/research-progress";

const SVG_W = 640;
const SVG_H = 420;
const CENTER_X = SVG_W / 2;
const CENTER_Y = SVG_H / 2;
const RING_RADII = [110, 175, 235];      // high, medium, low
const NODE_R = 6;
const LABEL_OFFSET = 12;

type Bucket = "high" | "medium" | "low";

interface PlacedRepo {
  repoId: string;
  repoName: string;
  status: RepoStatusEntry["status"];
  relevance: RouteEntry["relevance"] | "unknown";
  ringBucket: Bucket;
  x: number;
  y: number;
  inFootprint: boolean;
  codeEntityCount: number;
}

function bucketForRelevance(r: string): Bucket {
  if (r === "high") return "high";
  if (r === "medium") return "medium";
  return "low";
}

const STATUS_FILL: Record<RepoStatusEntry["status"], string> = {
  deep_indexed: "var(--color-ok, #34d399)",
  manifest_only: "var(--color-warn, #f59e0b)",
  auto_registered: "var(--color-accent, #8b5cf6)",
  not_cloned: "var(--color-err, #ef4444)",
};

const STATUS_STROKE: Record<RepoStatusEntry["status"], string> = {
  deep_indexed: "rgba(52,211,153,0.6)",
  manifest_only: "rgba(245,158,11,0.6)",
  auto_registered: "rgba(139,92,246,0.6)",
  not_cloned: "rgba(239,68,68,0.6)",
};


export function BlastRadiusGraph({ state }: { state: ResearchState }) {
  const [hovered, setHovered] = useState<string | null>(null);

  const placed = useMemo(
    () => layoutRepos(state.statusByRepo, state.route, state.subgraphStats),
    [state.statusByRepo, state.route, state.subgraphStats],
  );

  const summary = state.statusSummary;
  const stats = state.subgraphStats;

  if (placed.length === 0) {
    return (
      <div className="rounded-md border border-border bg-bg-1 p-4 text-[11.5px] text-fg-3">
        Blast-radius graph not available — no repos in scope yet.
      </div>
    );
  }

  return (
    <div className="rounded-md border border-border bg-bg-0">
      <header className="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5">
        <span className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-fg-1">
          <Sparkles className="h-3 w-3 text-accent" strokeWidth={1.75} />
          Blast-radius graph
        </span>
        {stats && (
          <span className="font-mono text-[10.5px] text-fg-3">
            {stats.totalNodes} entities · {stats.totalEdges} edges ·{" "}
            <span className="text-accent">
              {stats.crossRepoEdges} cross-repo
            </span>
          </span>
        )}
      </header>

      <div className="px-3 py-2">
        <svg
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          className="block w-full h-auto"
          role="img"
          aria-label="Blast-radius graph: repos arranged radially around the question, distance = relevance, color = index status"
        >
          {/* Concentric ring guides */}
          {RING_RADII.map((r, i) => (
            <circle
              key={i}
              cx={CENTER_X}
              cy={CENTER_Y}
              r={r}
              fill="none"
              stroke="var(--color-bg-3, rgba(255,255,255,0.06))"
              strokeWidth={1}
              strokeDasharray="2,3"
            />
          ))}

          {/* Edges from question center to each repo */}
          {placed.map((p) => (
            <line
              key={`e-${p.repoId}`}
              x1={CENTER_X}
              y1={CENTER_Y}
              x2={p.x}
              y2={p.y}
              stroke={STATUS_STROKE[p.status]}
              strokeWidth={p.inFootprint ? 1.5 : 0.75}
              strokeOpacity={p.inFootprint ? 0.8 : 0.4}
              strokeDasharray={p.inFootprint ? undefined : "3,2"}
            />
          ))}

          {/* Center node — the question */}
          <g>
            <circle
              cx={CENTER_X}
              cy={CENTER_Y}
              r={18}
              fill="var(--color-accent-soft, rgba(139,92,246,0.18))"
              stroke="var(--color-accent, #8b5cf6)"
              strokeWidth={1.5}
            />
            <foreignObject
              x={CENTER_X - 12}
              y={CENTER_Y - 12}
              width={24}
              height={24}
              style={{ overflow: "visible" }}
            >
              <div className="flex h-full w-full items-center justify-center text-accent">
                <MessageSquare className="h-3 w-3" strokeWidth={1.75} />
              </div>
            </foreignObject>
          </g>

          {/* Repo nodes */}
          {placed.map((p) => {
            const isHovered = hovered === p.repoId;
            const r = isHovered ? NODE_R + 2 : NODE_R;
            return (
              <g
                key={p.repoId}
                onMouseEnter={() => setHovered(p.repoId)}
                onMouseLeave={() => setHovered((cur) => (cur === p.repoId ? null : cur))}
                style={{ cursor: "pointer" }}
              >
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={r}
                  fill={STATUS_FILL[p.status]}
                  stroke={STATUS_STROKE[p.status]}
                  strokeWidth={isHovered ? 2 : 1}
                />
                <text
                  x={p.x}
                  y={p.y + LABEL_OFFSET + 4}
                  textAnchor="middle"
                  className="fill-fg-1 font-mono"
                  style={{ fontSize: "9.5px" }}
                  pointerEvents="none"
                >
                  {p.repoName.length > 18 ? p.repoName.slice(0, 17) + "…" : p.repoName}
                </text>
                {isHovered && (
                  <title>
                    {p.repoName}
                    {"\n"}status: {p.status} ({p.codeEntityCount} entities)
                    {"\n"}relevance: {p.relevance}
                    {p.inFootprint ? "\nin master-subgraph footprint" : ""}
                  </title>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      {/* Legend + summary */}
      <footer className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border bg-bg-1 px-3 py-1.5 text-[10.5px] text-fg-2">
        <LegendDot color={STATUS_FILL.deep_indexed} label="deep-indexed" />
        <LegendDot color={STATUS_FILL.manifest_only} label="manifest-only" />
        <LegendDot color={STATUS_FILL.auto_registered} label="auto-registered" />
        <LegendDot color={STATUS_FILL.not_cloned} label="not-cloned" />
        <span className="ml-auto font-mono text-[10px] text-fg-3">
          {summary
            ? `${summary.deep_indexed + summary.manifest_only + summary.auto_registered + summary.not_cloned} repos · distance = relevance`
            : `${placed.length} repos · distance = relevance`}
        </span>
      </footer>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        aria-hidden
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: color }}
      />
      <span className="font-mono">{label}</span>
    </span>
  );
}

function layoutRepos(
  statusByRepo: RepoStatusEntry[],
  route: RouteEntry[],
  subgraph: SubgraphStats | null,
): PlacedRepo[] {
  if (!statusByRepo.length) return [];

  const relevanceById = new Map<string, RouteEntry["relevance"]>();
  for (const r of route) relevanceById.set(r.id, r.relevance);

  const footprint = new Set(subgraph?.repoFootprint ?? []);

  const buckets: Record<Bucket, PlacedRepo[]> = {
    high: [], medium: [], low: [],
  };
  for (const s of statusByRepo) {
    const rel = relevanceById.get(s.repo_id) ?? "unknown";
    const ring = bucketForRelevance(rel as string);
    buckets[ring].push({
      repoId: s.repo_id,
      repoName: s.repo_name,
      status: s.status,
      relevance: rel,
      ringBucket: ring,
      x: 0, y: 0,                                  // placeholder
      inFootprint: footprint.has(s.repo_id),
      codeEntityCount: s.code_entity_count,
    });
  }

  // Distribute each ring's nodes evenly around the circle. Stagger
  // adjacent rings by half a wedge so labels don't overlap.
  const ringStarts: Record<Bucket, number> = {
    high: -Math.PI / 2,                            // top
    medium: -Math.PI / 2 + Math.PI / 18,
    low: -Math.PI / 2 + Math.PI / 9,
  };
  const ringRadius: Record<Bucket, number> = {
    high: RING_RADII[0],
    medium: RING_RADII[1],
    low: RING_RADII[2],
  };

  const out: PlacedRepo[] = [];
  for (const bucket of ["high", "medium", "low"] as Bucket[]) {
    const repos = buckets[bucket];
    const radius = ringRadius[bucket];
    const start = ringStarts[bucket];
    const step = repos.length > 0 ? (Math.PI * 2) / repos.length : 0;
    repos.forEach((p, i) => {
      const angle = start + i * step;
      out.push({
        ...p,
        x: CENTER_X + Math.cos(angle) * radius,
        y: CENTER_Y + Math.sin(angle) * radius,
      });
    });
  }
  return out;
}
