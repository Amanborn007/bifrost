import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { ConfidenceBadge } from "@/components/confidence-badge";
import { FreshnessIndicator } from "@/components/freshness-indicator";
import {
  listEntities,
  getEntity,
  getEntityRelationships,
  getEntitySource,
  getBlastRadius,
} from "@/lib/api";
import type {
  EntityDetail,
  EntitySummary,
  EntityRelationships,
  EntitySource,
  RelationshipEdge,
  BlastRadiusResponse,
} from "@/lib/api";
import {
  Search,
  X,
  Database,
  ArrowRight,
  ArrowLeft,
  FileCode,
  Network,
  List,
  MessageSquare,
  Loader2,
  Radius,
} from "lucide-react";
import { cn, describeError } from "@/lib/utils";

const TYPES = [
  "all",
  "component",
  "interface",
  "flow",
  "dependency",
  "pattern",
] as const;
const DEBOUNCE_MS = 250;

export interface EntityBrowserProps {
  repoId: string | null;
  domainId: string | null;
  initialSearch?: string;
}

/**
 * Entity browser — type chips + search + list + detail panel.
 *
 * Extracted from the old Entities page so the Knowledge page can compose
 * it filtered to the currently-selected repo. Holds its own search, type,
 * and selection state; parent controls which repo/domain to show.
 */
export function EntityBrowser({
  repoId,
  domainId,
  initialSearch,
}: EntityBrowserProps) {
  const [search, setSearch] = useState(initialSearch ?? "");
  const [debounced, setDebounced] = useState(search);
  const [type, setType] = useState<string>("all");
  const [selected, setSelected] = useState<EntityDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    timer.current = setTimeout(() => setDebounced(search), DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [search]);

  // Reset selection when the repo scope changes — otherwise we'd show a
  // detail panel for an entity that isn't even in the current list.
  useEffect(() => {
    setSelected(null);
  }, [repoId, domainId]);

  const entities = useQuery({
    queryKey: ["entities", type, repoId, domainId],
    queryFn: () =>
      listEntities({
        entity_type: type !== "all" ? type : undefined,
        repo_id: repoId ?? undefined,
        domain_id: domainId ?? undefined,
        limit: 200,
      }),
    // Keep previous data on-screen during filter changes — prevents the
    // "0 entities" flash when the user flips repos/types.
    placeholderData: (prev) => prev,
  });

  const filtered = (entities.data ?? []).filter((e) =>
    debounced
      ? e.name.toLowerCase().includes(debounced.toLowerCase()) ||
        e.urn.toLowerCase().includes(debounced.toLowerCase())
      : true,
  );

  const handleSelect = useCallback(async (urn: string) => {
    try {
      setLoadingDetail(true);
      const detail = await getEntity(urn);
      setSelected(detail);
    } catch {
      setSelected(null);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  const counts = (entities.data ?? []).reduce<Record<string, number>>(
    (acc, e) => {
      acc[e.entity_type] = (acc[e.entity_type] ?? 0) + 1;
      return acc;
    },
    {},
  );

  return (
    <div className="flex min-h-0 flex-col gap-3">
      {/* Filter bar */}
      <div className="space-y-2">
        <div className="flex flex-wrap gap-1.5">
          {TYPES.map((t) => {
            const active = type === t;
            const cnt =
              t === "all" ? (entities.data ?? []).length : counts[t] ?? 0;
            return (
              <button
                key={t}
                onClick={() => setType(t)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11.5px] font-medium transition-colors",
                  active
                    ? "border-accent bg-accent-soft text-accent"
                    : "border-border bg-bg-1 text-fg-1 hover:bg-bg-2",
                )}
              >
                {t === "all" ? "All" : t}
                <span
                  className={cn(
                    "tabular text-[10.5px]",
                    active ? "text-accent" : "text-fg-3",
                  )}
                >
                  {cnt}
                </span>
              </button>
            );
          })}
          {entities.isFetching && !entities.isLoading && (
            <span className="ml-1 inline-flex items-center gap-1 text-[10.5px] text-fg-3">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
              refreshing…
            </span>
          )}
          <span className="ml-auto text-[11px] text-fg-3">
            {filtered.length.toLocaleString()} shown
          </span>
        </div>
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3"
            strokeWidth={1.75}
          />
          <input
            placeholder="Search entities by name or URN"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-8 w-full rounded-md border border-border bg-bg-1 pl-8 pr-3 text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
          />
        </div>
      </div>

      <div
        className={cn(
          "grid min-h-0 flex-1 gap-3",
          selected
            ? "grid-cols-1 lg:grid-cols-[1fr_520px]"
            : "grid-cols-1",
        )}
      >
        <EntityList
          loading={entities.isLoading}
          entities={filtered}
          selectedUrn={selected?.urn ?? null}
          onSelect={handleSelect}
        />
        {(selected || loadingDetail) && (
          <aside className="sticky top-2 max-h-[calc(100vh-8rem)] self-start overflow-hidden">
            {loadingDetail ? (
              <Skeleton className="h-96" />
            ) : selected ? (
              <EntityDetailPanel
                entity={selected}
                onClose={() => setSelected(null)}
              />
            ) : null}
          </aside>
        )}
      </div>
    </div>
  );
}

/* ─── Entity list ────────────────────────────────────────── */

function EntityList({
  loading,
  entities,
  selectedUrn,
  onSelect,
}: {
  loading: boolean;
  entities: EntitySummary[];
  selectedUrn: string | null;
  onSelect: (urn: string) => void;
}) {
  if (loading) {
    return (
      <div className="space-y-1.5">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <Skeleton key={i} className="h-12 rounded-lg" />
        ))}
      </div>
    );
  }
  if (entities.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-bg-1 py-16 text-center">
        <Database className="mb-2 h-6 w-6 text-fg-3" strokeWidth={1.75} />
        <p className="text-[13.5px] font-medium text-fg-1">No entities match</p>
        <p className="mt-1 text-[12px] text-fg-3">
          Try clearing filters, or pick a different repo on the left.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-bg-1">
      <div className="grid grid-cols-[1fr_110px_100px] gap-3 border-b border-border bg-bg-0 px-4 py-2 text-[11px] font-medium text-fg-2">
        <span>Name</span>
        <span>Type</span>
        <span>Status</span>
      </div>
      <ol>
        {entities.map((e, idx) => {
          const isSelected = e.urn === selectedUrn;
          return (
            <li
              key={e.id}
              className={idx !== 0 ? "border-t border-border" : undefined}
            >
              <button
                onClick={() => onSelect(e.urn)}
                className={cn(
                  "grid w-full grid-cols-[1fr_110px_100px] items-center gap-3 px-4 py-2.5 text-left transition-colors",
                  isSelected ? "bg-accent-soft" : "hover:bg-bg-2",
                )}
              >
                <div className="min-w-0">
                  <div className="truncate text-[13px] font-medium text-fg-0">
                    {e.name}
                  </div>
                  <div className="truncate font-mono text-[10.5px] text-fg-3">
                    {e.urn}
                  </div>
                </div>
                <EntityTypeTag type={e.entity_type} />
                <FreshnessIndicator state={e.freshness_state} />
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/* ─── Detail panel with tabs ─────────────────────────────── */

type DetailTab = "summary" | "relationships" | "source";

function EntityDetailPanel({
  entity,
  onClose,
}: {
  entity: EntityDetail;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<DetailTab>("summary");

  const meta = (entity.metadata ?? {}) as Record<string, unknown>;
  const hasSourceFile =
    typeof meta.source_file === "string" && meta.source_file;

  const askInChat = `/query?prefill=${encodeURIComponent(
    `Tell me everything about the component ${entity.name} (${entity.urn}). ` +
      `Read its source file and summarise its responsibilities, methods, and callers.`,
  )}`;

  return (
    <article className="flex h-full flex-col overflow-hidden rounded-lg border border-border bg-bg-1">
      <header className="flex items-start justify-between gap-2 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <EntityTypeTag type={entity.entity_type} />
            <ConfidenceBadge level={entity.confidence_level} />
            <FreshnessIndicator state={entity.freshness_state} />
          </div>
          <h3 className="mt-1.5 text-[16px] font-semibold text-fg-0">
            {entity.name}
          </h3>
          <code className="mt-1 block break-all font-mono text-[10.5px] text-fg-2">
            {entity.urn}
          </code>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          className="h-7 w-7 shrink-0 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
        >
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </Button>
      </header>

      <nav className="flex border-b border-border bg-bg-0">
        <TabBtn
          active={tab === "summary"}
          onClick={() => setTab("summary")}
          icon={List}
        >
          Summary
        </TabBtn>
        <TabBtn
          active={tab === "relationships"}
          onClick={() => setTab("relationships")}
          icon={Network}
        >
          Relationships
        </TabBtn>
        <TabBtn
          active={tab === "source"}
          onClick={() => setTab("source")}
          icon={FileCode}
          disabled={!hasSourceFile}
        >
          Source
        </TabBtn>
        <div className="ml-auto flex items-center pr-3">
          <Link
            to={askInChat}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-2.5 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
            title="Ask Bifrost about this entity"
          >
            <MessageSquare className="h-3 w-3" strokeWidth={1.75} />
            Ask in chat
          </Link>
        </div>
      </nav>

      <div className="flex-1 overflow-y-auto">
        {tab === "summary" && <SummaryTab entity={entity} />}
        {tab === "relationships" && <RelationshipsTab entity={entity} />}
        {tab === "source" && <SourceTab entity={entity} />}
      </div>
    </article>
  );
}

function TabBtn({
  active,
  onClick,
  icon: Icon,
  children,
  disabled,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>;
  children: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "flex items-center gap-1.5 border-b-2 px-3 py-2 text-[12.5px] font-medium transition-colors disabled:opacity-40",
        active
          ? "border-accent text-fg-0"
          : "border-transparent text-fg-2 hover:text-fg-0",
      )}
    >
      <Icon className="h-3.5 w-3.5" strokeWidth={1.75} />
      {children}
    </button>
  );
}

/* ─── Summary tab ─────────────────────────── */

function SummaryTab({ entity }: { entity: EntityDetail }) {
  const meta = (entity.metadata ?? {}) as Record<string, unknown>;

  return (
    <div className="space-y-5 px-5 py-5">
      {typeof meta.purpose === "string" && meta.purpose && (
        <Section label="Purpose">
          <p className="text-[13.5px] leading-relaxed text-fg-1">
            {String(meta.purpose)}
          </p>
        </Section>
      )}

      {typeof meta.component_type === "string" && (
        <div className="flex gap-5">
          <KV label="Component type">{String(meta.component_type)}</KV>
          {typeof meta.source_file === "string" && (
            <KV label="Source file">
              <code className="font-mono text-[11.5px] text-fg-1">
                {String(meta.source_file)}
              </code>
            </KV>
          )}
        </div>
      )}

      {typeof meta.flow_type === "string" && (
        <KV label="Flow type">{String(meta.flow_type)}</KV>
      )}

      {Array.isArray(meta.exported_interfaces) &&
        (meta.exported_interfaces as unknown[]).length > 0 && (
          <Section label="Exported interfaces">
            <ul className="space-y-1 text-[13px] text-fg-1">
              {(meta.exported_interfaces as string[]).map((s, i) => (
                <li key={i}>
                  <code className="rounded bg-bg-2 px-1.5 py-0.5 font-mono text-[12px]">
                    {s}
                  </code>
                </li>
              ))}
            </ul>
          </Section>
        )}

      {Array.isArray(meta.internal_dependencies) &&
        (meta.internal_dependencies as unknown[]).length > 0 && (
          <Section label="Internal dependencies">
            <ul className="flex flex-wrap gap-1.5">
              {(meta.internal_dependencies as string[]).map((s, i) => (
                <li
                  key={i}
                  className="rounded-md border border-border bg-bg-0 px-2 py-0.5 font-mono text-[11.5px] text-fg-1"
                >
                  {s}
                </li>
              ))}
            </ul>
          </Section>
        )}

      {Array.isArray(meta.segments) &&
        (meta.segments as unknown[]).length > 0 && (
          <Section label="Flow segments">
            <ol className="space-y-1.5 text-[13px]">
              {(meta.segments as Array<Record<string, unknown>>).map((s, i) => (
                <li key={i} className="flex gap-2">
                  <span className="tabular shrink-0 text-fg-3">{i + 1}.</span>
                  <div className="min-w-0">
                    <span className="font-medium text-fg-0">
                      {String(s.component_name ?? "—")}
                    </span>
                    <span className="mx-1.5 rounded bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
                      {String(s.segment_type ?? "")}
                    </span>
                    <span className="text-fg-2">{String(s.description ?? "")}</span>
                  </div>
                </li>
              ))}
            </ol>
          </Section>
        )}

      {Array.isArray(meta.known_gaps) &&
        (meta.known_gaps as unknown[]).length > 0 && (
          <Section label="Known gaps">
            <ul className="space-y-1 text-[13px] text-warn">
              {(meta.known_gaps as string[]).map((s, i) => (
                <li key={i} className="flex gap-2">
                  <span aria-hidden>▸</span>
                  <span>{s}</span>
                </li>
              ))}
            </ul>
          </Section>
        )}

      {entity.qualified_name && (
        <Section label="Qualified name">
          <code className="block break-all rounded-md border border-border bg-bg-0 px-3 py-2 font-mono text-[12px] text-fg-1">
            {entity.qualified_name}
          </code>
        </Section>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-[11px] text-fg-3">
        <span>
          Created{" "}
          <span className="text-fg-1">
            {new Date(entity.created_at).toLocaleString()}
          </span>
        </span>
        <span>
          Updated{" "}
          <span className="text-fg-1">
            {new Date(entity.updated_at).toLocaleString()}
          </span>
        </span>
      </footer>
    </div>
  );
}

/* ─── Relationships tab ───────────────────── */

function RelationshipsTab({ entity }: { entity: EntityDetail }) {
  const rels = useQuery<EntityRelationships>({
    queryKey: ["entity-relationships", entity.id],
    queryFn: () => getEntityRelationships(entity.id),
  });

  if (rels.isLoading)
    return (
      <div className="p-5">
        <Skeleton className="h-32" />
      </div>
    );
  if (rels.isError)
    return (
      <p className="p-5 text-[13px] text-err">
        Couldn't load relationships.
      </p>
    );

  const data = rels.data;
  if (!data || (data.outbound.length === 0 && data.inbound.length === 0)) {
    return (
      <div className="px-5 py-5">
        <BlastRadiusControl entity={entity} />
        <div className="mt-6 flex flex-col items-center justify-center py-8 text-center">
          <Network className="mb-2 h-6 w-6 text-fg-3" strokeWidth={1.75} />
          <p className="text-[13px] font-medium text-fg-1">No relationships indexed</p>
          <p className="mt-1 text-[12px] text-fg-3">
            Run a <span className="font-medium text-fg-1">Full re-index</span> on
            this repo to extract relationships between components.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 px-5 py-5">
      <BlastRadiusControl entity={entity} />
      {data.outbound.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-2">
            <ArrowRight
              className="h-3.5 w-3.5 text-fg-2"
              strokeWidth={1.75}
            />
            <h4 className="text-[12.5px] font-semibold text-fg-0">
              Outbound ({data.outbound.length})
            </h4>
          </div>
          <EdgeList edges={data.outbound} direction="outbound" entityUrn={entity.urn} />
        </div>
      )}
      {data.inbound.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-2">
            <ArrowLeft className="h-3.5 w-3.5 text-fg-2" strokeWidth={1.75} />
            <h4 className="text-[12.5px] font-semibold text-fg-0">
              Inbound ({data.inbound.length})
            </h4>
          </div>
          <EdgeList edges={data.inbound} direction="inbound" entityUrn={entity.urn} />
        </div>
      )}
    </div>
  );
}

/* ─── Blast radius (S6 dependency-impact) ─────────────────── */

const BLAST_BUCKET_PILL: Record<string, string> = {
  high: "bg-ok-soft text-ok",
  medium: "bg-warn-soft text-warn",
  low: "bg-err-soft text-err",
};

function bucketForNode(
  data: BlastRadiusResponse,
  nodeId: string,
): string | null {
  for (const [bucket, ids] of Object.entries(data.confidence_buckets)) {
    if (ids.includes(nodeId)) return bucket;
  }
  return null;
}

/**
 * "Blast radius" affordance — walks the dependency graph downstream
 * from this entity (POST /dependencies/blast-radius) and lists the
 * impacted entities, ranked by distance, with weakest-path confidence
 * shown per node. Kept minimal: a single button + inline result panel,
 * no graph drawing.
 */
function BlastRadiusControl({ entity }: { entity: EntityDetail }) {
  const blast = useMutation({
    mutationFn: () =>
      getBlastRadius(entity.urn, { direction: "downstream", depth: 2 }),
  });

  const data = blast.data;
  const impacted = data
    ? [...data.nodes]
        .filter((n) => !n.is_seed)
        .sort(
          (a, b) =>
            a.distance_from_seed - b.distance_from_seed ||
            b.min_path_confidence - a.min_path_confidence,
        )
    : [];

  return (
    <div className="rounded-md border border-border bg-bg-0 p-3">
      <div className="flex items-center gap-2">
        <Radius className="h-3.5 w-3.5 text-fg-2" strokeWidth={1.75} />
        <h4 className="text-[12.5px] font-semibold text-fg-0">
          Blast radius
        </h4>
        <button
          type="button"
          onClick={() => blast.mutate()}
          disabled={blast.isPending}
          className={cn(
            "ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11.5px] font-medium",
            blast.isPending
              ? "bg-bg-2 text-fg-3"
              : "bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
          )}
          title="What breaks downstream if this entity changes (depth 2)"
        >
          {blast.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
          ) : (
            <Radius className="h-3 w-3" strokeWidth={1.75} />
          )}
          {blast.isPending ? "Walking…" : "Compute"}
        </button>
      </div>

      {!data && !blast.isPending && !blast.isError && (
        <p className="mt-1.5 text-[11.5px] text-fg-3">
          Walk the dependency graph downstream (depth 2) to see what
          would be affected if this entity changes.
        </p>
      )}

      {blast.isError && (
        <p className="mt-2 text-[12px] text-err">
          Couldn't compute blast radius: {describeError(blast.error)}
        </p>
      )}

      {data && (
        <div className="mt-2">
          <p className="text-[11.5px] text-fg-2">
            {data.impacted_count} impacted entit
            {data.impacted_count === 1 ? "y" : "ies"} via downstream walk
            (depth {data.depth})
            {data.soft_cap_exceeded ? " · soft cap hit (truncated)" : ""}
          </p>
          {impacted.length === 0 ? (
            <p className="mt-2 text-[12px] text-fg-3">
              Nothing downstream — no entities depend on this one.
            </p>
          ) : (
            <ul className="mt-2 overflow-hidden rounded-md border border-border bg-bg-1">
              {impacted.map((n, idx) => {
                const bucket = bucketForNode(data, n.id);
                return (
                  <li
                    key={n.id}
                    className={cn(
                      "flex items-center gap-3 px-3 py-2",
                      idx !== 0 && "border-t border-border",
                    )}
                  >
                    <span className="inline-flex shrink-0 items-center rounded-full border border-border px-2 py-0.5 font-mono text-[10.5px] text-fg-2">
                      d{n.distance_from_seed}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-medium text-fg-0">
                        {n.name}
                      </div>
                      <div className="truncate font-mono text-[11px] text-fg-3">
                        {n.urn}
                      </div>
                    </div>
                    {bucket && (
                      <span
                        className={cn(
                          "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                          BLAST_BUCKET_PILL[bucket] ?? "bg-bg-3 text-fg-2",
                        )}
                        title={`Weakest-path confidence ${n.min_path_confidence.toFixed(2)}`}
                      >
                        {bucket}
                      </span>
                    )}
                    <EntityTypeTag type={n.entity_type} />
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function repoFromUrn(urn: string): string | null {
  const m = urn.match(/^urn:bifrost:repo:([^/]+)/);
  return m ? m[1] : null;
}

function EdgeList({
  edges,
  direction,
  entityUrn,
}: {
  edges: RelationshipEdge[];
  direction: "outbound" | "inbound";
  entityUrn: string;
}) {
  const thisRepo = repoFromUrn(entityUrn);
  return (
    <ul className="overflow-hidden rounded-md border border-border bg-bg-0">
      {edges.map((e, idx) => {
        const partnerUrn =
          direction === "outbound" ? e.target_urn : e.source_urn;
        const partnerName =
          direction === "outbound" ? e.target_name : e.source_name;
        const partnerType =
          direction === "outbound"
            ? e.target_entity_type
            : e.source_entity_type;
        const partnerRepo = repoFromUrn(partnerUrn);
        const isCrossRepo = thisRepo && partnerRepo && thisRepo !== partnerRepo;
        return (
          <li
            key={e.id}
            className={cn(
              "flex items-center gap-3 px-3 py-2",
              idx !== 0 && "border-t border-border",
              isCrossRepo && "bg-info-soft/30",
            )}
          >
            <span className="inline-flex shrink-0 items-center rounded-full border border-border px-2 py-0.5 font-mono text-[10.5px] text-fg-2">
              {e.rel_type}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="truncate text-[13px] font-medium text-fg-0">
                  {partnerName}
                </span>
                {isCrossRepo && (
                  <span
                    className="shrink-0 rounded border border-info bg-info-soft px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide text-info"
                    title={`Cross-repo: ${partnerRepo}`}
                  >
                    {partnerRepo}
                  </span>
                )}
              </div>
              <div className="truncate font-mono text-[10.5px] text-fg-3">
                {partnerUrn}
              </div>
            </div>
            {partnerType && <EntityTypeTag type={partnerType} />}
          </li>
        );
      })}
    </ul>
  );
}

/* ─── Source tab ──────────────────────────── */

function SourceTab({ entity }: { entity: EntityDetail }) {
  const src = useQuery<EntitySource>({
    queryKey: ["entity-source", entity.id],
    queryFn: () => getEntitySource(entity.id),
  });

  if (src.isLoading)
    return (
      <div className="p-5">
        <Skeleton className="h-64" />
      </div>
    );
  if (src.isError)
    return (
      <div className="px-5 py-5 text-[13px] text-fg-2">
        <p>Source not available on server.</p>
        <p className="mt-1 text-[12px] text-fg-3">
          The repository clone isn't on disk. Run an extraction to populate it.
        </p>
      </div>
    );
  const data = src.data!;
  const language = inferLanguage(data.file_path);

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-bg-0 px-4 py-2">
        <code className="truncate font-mono text-[11.5px] text-fg-1">
          {data.file_path}
        </code>
        <span className="ml-2 shrink-0 text-[10.5px] text-fg-3">
          {data.truncated
            ? "truncated · first 200 KB"
            : `${data.content.length.toLocaleString()} bytes`}
        </span>
      </div>
      <div className="flex-1 overflow-auto">
        <SyntaxHighlighter
          language={language}
          style={oneDark}
          showLineNumbers
          wrapLongLines={false}
          customStyle={{
            margin: 0,
            padding: "12px 16px",
            fontSize: "12px",
            background: "var(--bg-0)",
          }}
          codeTagProps={{ style: { fontFamily: "var(--font-mono)" } }}
        >
          {data.content}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}

function inferLanguage(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return (
    {
      java: "java",
      kt: "kotlin",
      py: "python",
      go: "go",
      ts: "typescript",
      tsx: "tsx",
      js: "javascript",
      jsx: "jsx",
      rs: "rust",
      rb: "ruby",
      php: "php",
      cs: "csharp",
      swift: "swift",
      cpp: "cpp",
      cc: "cpp",
      c: "c",
      h: "c",
      hpp: "cpp",
      proto: "protobuf",
      yaml: "yaml",
      yml: "yaml",
      json: "json",
      toml: "toml",
      md: "markdown",
      sh: "bash",
      sql: "sql",
      xml: "xml",
    }[ext] ?? "text"
  );
}

/* ─── Small helpers ──────────────────────── */

function Section({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h4 className="mb-1.5 text-[11px] font-medium text-fg-2">{label}</h4>
      {children}
    </section>
  );
}

function KV({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-[11px] font-medium text-fg-2">{label}</div>
      <div className="mt-0.5 text-[13px] text-fg-1">{children}</div>
    </div>
  );
}

function EntityTypeTag({ type }: { type?: string | null }) {
  const t = type ?? "entity";
  const tone =
    {
      component: "border-info bg-info-soft text-info",
      flow: "border-accent bg-accent-soft text-accent",
      dependency: "border-warn bg-warn-soft text-warn",
      interface: "border-ok bg-ok-soft text-ok",
      pattern: "border-border bg-bg-2 text-fg-2",
      file: "border-border bg-bg-2 text-fg-2",
    }[t] ?? "border-border bg-bg-2 text-fg-2";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
        tone,
      )}
    >
      {t}
    </span>
  );
}
