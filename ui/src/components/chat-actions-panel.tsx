/**
 * Chat actions panel — "What's next?" strip shown after a
 * substantive conversation. Four inline panels, no screen jumps:
 *
 *   • Create Jira task  — AI-prefilled + blast radius section
 *   • Draft Slack msg   — multi-channel, AI-drafted, editable
 *   • Generate handover — routes to handovers page with scope hint
 *   • Start session     — cross-team requirement capture
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  BookOpen,
  Check,
  ChevronDown,
  ClipboardList,
  Copy,
  ExternalLink,
  GitBranch,
  Loader2,
  MessageSquare,
  Users,
  X,
  Zap,
} from "lucide-react";
import {
  createCollabSession,
  createSlackDraft,
  getBlastRadius,
  listEntities,
  lookupOwnership,
  previewSpawnTask,
  spawnTaskFromChat,
  type BlastImpact,
  type BlastRadiusResponse,
  type ConversationMessage,
} from "@/lib/api";
import { cn, describeError } from "@/lib/utils";
import { toSlackMrkdwn } from "@/lib/slack-mrkdwn";
import { Skeleton } from "@/components/ui/skeleton";

// ── Types ────────────────────────────────────────────────────

type ActivePanel = "jira" | "slack" | "handover" | "session" | null;
type IssueType = "Task" | "Bug" | "Story" | "Sub-task";

// ── Helpers ──────────────────────────────────────────────────

function extractSeedUrn(messages: ConversationMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    for (const src of messages[i].sources ?? []) {
      const t = src.entity_type;
      if (src.urn && (t === "component" || t === "service" || t === "flow")) {
        return src.urn;
      }
    }
  }
  return null;
}

// One scannable line per hop: `A --calls--> B  (app/b.py:42)`.
function hopLine(h: BlastImpact["path"][number]): string {
  const loc =
    h.to_source_file != null
      ? ` (${h.to_source_file}${h.to_line != null ? `:${h.to_line}` : ""})`
      : "";
  return `\`${h.from_name}\` --${h.rel_type}--> \`${h.to_name}\`${loc}`;
}

export function buildBlastSection(br: BlastRadiusResponse): string {
  const impacts = br.impacts.filter((im) => im.path_confidence >= 0.7);
  if (impacts.length === 0) return "";
  const lines = impacts.slice(0, 8).map((im) => {
    const tier =
      im.distance_from_seed === 1 ? "direct" : `${im.distance_from_seed}-hop`;
    const conf = `${Math.round(im.path_confidence * 100)}%`;
    const path =
      im.path.length > 0
        ? `\n  - path: ${im.path.map(hopLine).join(" → ")}`
        : "";
    return `- **${im.name}** (${tier}, ${im.entity_type}, ${conf}) — ${im.reason}${path}`;
  });
  const tail =
    br.tail_count > 0
      ? `\n- _…and ${br.tail_count} lower-impact entit${
          br.tail_count !== 1 ? "ies" : "y"
        } (long tail)_`
      : "";
  return `\n\n## Blast Radius\nWhat breaks, where, and why (ranked by directness):\n${lines.join(
    "\n",
  )}${tail}`;
}

// ── Blast-radius impact row ───────────────────────────────────
// One impacted entity: collapsed shows name + directness + confidence;
// expanded reveals the concrete edge path (seed → … → it) with the
// rel_type chain and the file:line at each boundary — the exact call
// an engineer would open to see what breaks.
export function ImpactRow({
  impact,
  defaultOpen,
}: {
  impact: BlastImpact;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const direct = impact.distance_from_seed === 1;
  const conf = Math.round(impact.path_confidence * 100);
  const loc =
    impact.source_file != null
      ? `${impact.source_file}${impact.line != null ? `:${impact.line}` : ""}`
      : null;

  return (
    <li className="rounded border border-border/60 bg-bg-2/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-[11.5px]"
        aria-expanded={open}
      >
        <GitBranch className="h-3 w-3 shrink-0 text-fg-3" strokeWidth={1.75} />
        <span className="truncate font-medium text-fg-0">{impact.name}</span>
        <span
          className={cn(
            "shrink-0 rounded-full px-1.5 py-px text-[9.5px] font-medium",
            direct
              ? "bg-warn-soft/40 text-warn"
              : "bg-bg-3 text-fg-3",
          )}
        >
          {direct ? "direct" : `${impact.distance_from_seed}-hop`}
        </span>
        {impact.is_cross_repo && (
          <span className="shrink-0 rounded-full bg-accent-soft px-1.5 py-px text-[9.5px] font-medium text-accent">
            cross-repo
          </span>
        )}
        <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-3">
          {conf}%
        </span>
        <ChevronDown
          className={cn(
            "h-3 w-3 shrink-0 text-fg-3 transition-transform",
            open && "rotate-180",
          )}
          strokeWidth={1.75}
        />
      </button>
      {open && (
        <div className="border-t border-border/40 px-2 py-1.5">
          {/* WHY — deterministic, code-grounded reason */}
          <p className="mb-1.5 text-[11px] leading-snug text-fg-2">
            {impact.reason}
          </p>
          {/* The concrete edge path with file:line at each boundary */}
          {impact.path.length > 0 && (
            <ol className="space-y-0.5">
              {impact.path.map((h, i) => {
                const hopLoc =
                  h.to_source_file != null
                    ? `${h.to_source_file}${h.to_line != null ? `:${h.to_line}` : ""}`
                    : null;
                return (
                  <li
                    key={`${h.from_urn}-${h.to_urn}-${i}`}
                    className="flex flex-wrap items-baseline gap-1 font-mono text-[10px] text-fg-2"
                  >
                    <span className="text-fg-1">{h.from_name}</span>
                    <span className="text-accent">--{h.rel_type}--&gt;</span>
                    <span className="text-fg-1">{h.to_name}</span>
                    {hopLoc && (
                      <span className="text-fg-3">{hopLoc}</span>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
          {loc && impact.path.length === 0 && (
            <p className="font-mono text-[10px] text-fg-3">{loc}</p>
          )}
        </div>
      )}
    </li>
  );
}

// Build a Markdown draft from the last assistant answer, preserving
// structure (headings, bold, bullets, footnote markers). The raw
// Markdown is what the user edits; `toSlackMrkdwn` converts it to Slack
// mrkdwn only at copy/send time. We keep a generous excerpt rather than
// stripping to plain prose so the formatting survives the round-trip.
function buildSlackDraft(messages: ConversationMessage[]): string {
  const lastAssistant = [...messages]
    .reverse()
    .find((m) => m.role === "assistant");
  if (!lastAssistant) return "";

  const content = lastAssistant.content.trim();
  // Cap to a reasonable Slack-sized excerpt (Slack hard-limits ~4K),
  // cutting on a paragraph boundary when possible.
  if (content.length <= 1500) return content;
  const cut = content.slice(0, 1500);
  const lastBreak = cut.lastIndexOf("\n\n");
  return (lastBreak > 600 ? cut.slice(0, lastBreak) : cut).trim() + "\n\n…";
}

// ── Root bar ─────────────────────────────────────────────────

export function ChatActionsBar({
  conversationId,
  messages,
  scopeRepoIds = [],
  reposUsedNames = [],
  isStreaming,
}: {
  conversationId: string | null;
  messages: ConversationMessage[];
  scopeRepoIds?: string[];
  reposUsedNames?: string[];
  isStreaming: boolean;
}) {
  const [open, setOpen] = useState<ActivePanel>(null);
  const hasReply = messages.some((m) => m.role === "assistant");

  // Only show once the assistant has responded and is no longer streaming
  if (!conversationId || !hasReply || isStreaming) return null;

  const toggle = (p: ActivePanel) => setOpen((v) => (v === p ? null : p));

  return (
    <div className="shrink-0 border-t border-border/50 bg-bg-0 px-6 pb-1 pt-2.5">
      <div className="mx-auto w-full max-w-[1200px]">

        {/* Action strip */}
        <div className="mb-2.5 flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-[10.5px] font-medium text-fg-3">Next step:</span>
          <Pill
            icon={<ClipboardList className="h-3 w-3" strokeWidth={1.75} />}
            label="Create Jira task"
            active={open === "jira"}
            onClick={() => toggle("jira")}
          />
          <Pill
            icon={<MessageSquare className="h-3 w-3" strokeWidth={1.75} />}
            label="Draft Slack message"
            active={open === "slack"}
            onClick={() => toggle("slack")}
          />
          <Pill
            icon={<BookOpen className="h-3 w-3" strokeWidth={1.75} />}
            label="Generate handover"
            active={open === "handover"}
            onClick={() => toggle("handover")}
          />
          <Pill
            icon={<Users className="h-3 w-3" strokeWidth={1.75} />}
            label="Brief another squad"
            active={open === "session"}
            onClick={() => toggle("session")}
          />
          {open && (
            <button
              onClick={() => setOpen(null)}
              aria-label="Close panel"
              className="ml-auto rounded p-0.5 text-fg-3 hover:text-fg-0"
            >
              <X className="h-3.5 w-3.5" strokeWidth={1.75} />
            </button>
          )}
        </div>

        {/* Inline panels — only one open at a time */}
        {open === "jira" && (
          <JiraTaskPanel
            conversationId={conversationId}
            messages={messages}
            scopeRepoIds={scopeRepoIds}
            onClose={() => setOpen(null)}
          />
        )}
        {open === "slack" && (
          <SlackDraftPanel
            messages={messages}
            reposUsedNames={reposUsedNames}
            onClose={() => setOpen(null)}
          />
        )}
        {open === "handover" && (
          <HandoverTriggerPanel
            messages={messages}
            onClose={() => setOpen(null)}
          />
        )}
        {open === "session" && (
          <SessionStartPanel
            messages={messages}
            onClose={() => setOpen(null)}
          />
        )}
      </div>
    </div>
  );
}

function Pill({
  icon,
  label,
  active,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] font-medium transition-colors",
        active
          ? "border-accent bg-accent-soft text-accent"
          : "border-border bg-bg-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0",
      )}
    >
      {icon}
      {label}
      <ChevronDown
        className={cn("h-3 w-3 transition-transform", active && "rotate-180")}
        strokeWidth={1.75}
      />
    </button>
  );
}

// ── Jira task panel ──────────────────────────────────────────

function JiraTaskPanel({
  conversationId,
  messages,
  scopeRepoIds = [],
  onClose,
}: {
  conversationId: string;
  messages: ConversationMessage[];
  scopeRepoIds?: string[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [issueType, setIssueType] = useState<IssueType>("Task");
  const [title, setTitle] = useState("");
  const [projectKey, setProjectKey] = useState("");
  const [criteria, setCriteria] = useState<string[]>([]);
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<{
    id: string;
    title: string;
    jira_key?: string | null;
  } | null>(null);
  const seededRef = useRef(false);

  // AI-suggest all fields from the conversation context
  const preview = useQuery({
    queryKey: ["spawn-preview", conversationId],
    queryFn: () => previewSpawnTask(conversationId),
    staleTime: 60_000,
  });

  // Populate form once (don't overwrite user edits on re-renders)
  useEffect(() => {
    if (!preview.data || seededRef.current) return;
    seededRef.current = true;
    setTitle(preview.data.suggested_title ?? "");
    setDescription(preview.data.suggested_intent ?? "");
    setCriteria(preview.data.suggested_acceptance_criteria ?? []);
    setProjectKey(preview.data.resolved_project_key ?? "");
    const t = preview.data.suggested_issue_type;
    if (t === "Bug" || t === "Story" || t === "Sub-task") {
      setIssueType(t as IssueType);
    }
  }, [preview.data]);

  // Blast radius — try message sources first, fall back to scoped repos.
  const seedUrn = useMemo(() => extractSeedUrn(messages), [messages]);

  // Fallback: when no entity URN in message sources, find the primary
  // service/component for the first scoped repo.
  const fallbackEntityQ = useQuery({
    queryKey: ["entities-for-blast", scopeRepoIds[0]],
    queryFn: async () => {
      const services = await listEntities({
        repo_id: scopeRepoIds[0],
        entity_type: "service",
        limit: 1,
      });
      if (services.length > 0) return services[0].urn;
      const any = await listEntities({ repo_id: scopeRepoIds[0], limit: 1 });
      return any[0]?.urn ?? null;
    },
    enabled: !seedUrn && scopeRepoIds.length > 0,
    staleTime: 5 * 60_000,
  });

  const effectiveSeedUrn = seedUrn ?? fallbackEntityQ.data ?? null;

  const blastQ = useQuery({
    queryKey: ["blast-radius", effectiveSeedUrn],
    queryFn: () =>
      getBlastRadius(effectiveSeedUrn!, {
        direction: "downstream",
        depth: 3,
        confidence_floor: 0.6,
      }),
    enabled: !!effectiveSeedUrn,
    staleTime: 2 * 60_000,
  });
  // Code-grounded impacts ranked by directness (backend already sorts:
  // distance asc, then confidence desc). Keep only high-confidence ones
  // for the headline so we don't pad with faint links.
  const impacts = useMemo(
    () =>
      (blastQ.data?.impacts ?? []).filter((im) => im.path_confidence >= 0.7),
    [blastQ.data],
  );
  const directImpact = useMemo(
    () => impacts.filter((im) => im.distance_from_seed === 1),
    [impacts],
  );
  const downstreamCount = useMemo(
    () => impacts.filter((im) => im.distance_from_seed > 1).length,
    [impacts],
  );
  const tailCount = blastQ.data?.tail_count ?? 0;

  const onCreate = useCallback(async () => {
    if (!title.trim() || creating) return;
    setCreating(true);
    try {
      // Build a Markdown description that embeds blast radius + criteria
      let fullDesc = description.trim();
      if (criteria.length > 0) {
        fullDesc += `\n\n## Acceptance Criteria\n${criteria.map((c) => `- [ ] ${c}`).join("\n")}`;
      }
      if (blastQ.data && impacts.length > 0) {
        fullDesc += buildBlastSection(blastQ.data);
      }

      const task = await spawnTaskFromChat(conversationId, {
        title: title.trim(),
        override_intent: fullDesc,
        issue_type: issueType,
        project_key: projectKey || undefined,
        create_jira_now: true,
        acceptance_criteria: criteria.length > 0 ? criteria : undefined,
      });
      setCreated(task);
      toast.success(`Task created: ${task.title.slice(0, 60)}`);
    } catch (err) {
      toast.error(`Failed to create task: ${describeError(err)}`);
    } finally {
      setCreating(false);
    }
  }, [
    conversationId,
    title,
    description,
    criteria,
    issueType,
    projectKey,
    blastQ.data,
    impacts,
    creating,
  ]);

  // ── Success state ──
  if (created) {
    return (
      <div className="mb-3 flex items-center gap-3 rounded-xl border border-ok/50 bg-ok-soft/20 px-4 py-3">
        <Check className="h-4 w-4 shrink-0 text-ok" strokeWidth={2} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-fg-0">
            {created.title}
          </div>
          {created.jira_key && (
            <div className="font-mono text-[11.5px] text-fg-3">
              {created.jira_key}
            </div>
          )}
        </div>
        <button
          onClick={() => navigate(`/tasks/${created.id}`)}
          className="inline-flex shrink-0 items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover"
        >
          Open task
          <ExternalLink className="h-3 w-3" strokeWidth={2} />
        </button>
        <button
          onClick={onClose}
          className="shrink-0 text-fg-3 hover:text-fg-0"
        >
          <X className="h-4 w-4" strokeWidth={1.75} />
        </button>
      </div>
    );
  }

  return (
    <div className="mb-3 rounded-xl border border-border bg-bg-1">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          <span className="text-[13px] font-semibold text-fg-0">
            Create Jira Task
          </span>
          {preview.isLoading && (
            <Loader2
              className="h-3 w-3 animate-spin text-fg-3"
              strokeWidth={1.75}
            />
          )}
        </div>
        <button
          onClick={onClose}
          className="text-fg-3 hover:text-fg-0"
        >
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </button>
      </div>

      <div className="grid grid-cols-1 gap-4 p-4 md:grid-cols-2">
        {/* Left column: type + title + project */}
        <div className="space-y-4">
          {/* Issue type */}
          <div>
            <label className="mb-1.5 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
              Type
            </label>
            <div className="flex flex-wrap gap-1.5">
              {(["Task", "Bug", "Story", "Sub-task"] as IssueType[]).map(
                (t) => (
                  <button
                    key={t}
                    onClick={() => setIssueType(t)}
                    className={cn(
                      "rounded-full border px-2.5 py-0.5 text-[11.5px] font-medium transition-colors",
                      issueType === t
                        ? "border-accent bg-accent-soft text-accent"
                        : "border-border bg-bg-2 text-fg-2 hover:text-fg-0",
                    )}
                  >
                    {t}
                  </button>
                ),
              )}
            </div>
          </div>

          {/* Title */}
          <div>
            <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
              Title
            </label>
            {preview.isLoading && !title ? (
              <Skeleton className="h-9 rounded-md" />
            ) : (
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Task title…"
                className="w-full rounded-md border border-border bg-bg-2 px-3 py-2 text-[13px] text-fg-0 focus:border-accent focus:outline-none"
              />
            )}
          </div>

          {/* Project */}
          <div>
            <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
              Jira project
            </label>
            <div className="flex items-center gap-2">
              {preview.isLoading && !projectKey ? (
                <Skeleton className="h-9 flex-1 rounded-md" />
              ) : (
                <input
                  type="text"
                  value={projectKey}
                  onChange={(e) =>
                    setProjectKey(e.target.value.toUpperCase())
                  }
                  placeholder="PROJ"
                  className="flex-1 rounded-md border border-border bg-bg-2 px-3 py-2 font-mono text-[13px] text-fg-0 focus:border-accent focus:outline-none"
                />
              )}
              {(preview.data?.candidate_projects ?? []).length > 0 && (
                <div className="flex gap-1">
                  {(preview.data?.candidate_projects ?? [])
                    .slice(0, 3)
                    .map((p) => (
                      <button
                        key={p}
                        onClick={() => setProjectKey(p)}
                        className={cn(
                          "rounded border px-2 py-1 font-mono text-[11px]",
                          projectKey === p
                            ? "border-accent bg-accent-soft text-accent"
                            : "border-border bg-bg-2 text-fg-2 hover:text-fg-0",
                        )}
                      >
                        {p}
                      </button>
                    ))}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Right column: blast radius + criteria */}
        <div className="space-y-4">
          {/* Blast radius */}
          <div>
            <label className="mb-1.5 flex items-center gap-1 text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
              <Zap className="h-3 w-3" strokeWidth={1.75} />
              Blast radius
              {(blastQ.isLoading || fallbackEntityQ.isLoading) && (
                <Loader2
                  className="h-3 w-3 animate-spin"
                  strokeWidth={1.75}
                />
              )}
            </label>
            {fallbackEntityQ.isLoading ? (
              <Skeleton className="h-8 rounded-md" />
            ) : !effectiveSeedUrn ? (
              <p className="text-[11.5px] text-fg-3">
                No service entities found — run a research question scoped to
                a repo to enable blast radius.
              </p>
            ) : blastQ.isLoading ? (
              <Skeleton className="h-16 rounded-md" />
            ) : impacts.length === 0 ? (
              <p className="text-[11.5px] text-fg-3">
                No impact detected at high confidence.
              </p>
            ) : (
              <div className="rounded-md border border-warn/30 bg-warn-soft/10 px-3 py-2.5">
                <p className="mb-2 text-[11.5px] font-medium text-warn">
                  {directImpact.length > 0
                    ? `${directImpact.length} direct dependenc${directImpact.length !== 1 ? "ies" : "y"}`
                    : "No direct dependencies"}
                  {downstreamCount > 0 && (
                    <span className="ml-1 font-normal text-fg-3">
                      · {downstreamCount} further downstream
                    </span>
                  )}
                </p>
                <ul className="space-y-1">
                  {impacts.slice(0, 6).map((im, i) => (
                    <ImpactRow key={im.urn} impact={im} defaultOpen={i < 2} />
                  ))}
                </ul>
                {tailCount > 0 && (
                  <p className="mt-1.5 text-[10.5px] text-fg-3">
                    + {tailCount} lower-impact entit
                    {tailCount !== 1 ? "ies" : "y"} (long tail, collapsed)
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Acceptance criteria */}
          <div>
            <label className="mb-1.5 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
              Acceptance criteria
            </label>
            {preview.isLoading && criteria.length === 0 ? (
              <div className="space-y-1.5">
                {[1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-5 rounded" />
                ))}
              </div>
            ) : criteria.length === 0 ? (
              <p className="text-[11.5px] text-fg-3">
                None suggested — add manually if needed.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {criteria.map((c, i) => (
                  <li key={c.slice(0, 40)} className="flex items-start gap-2">
                    <span className="mt-1 h-3 w-3 shrink-0 rounded border border-border bg-bg-2" />
                    <span
                      contentEditable
                      suppressContentEditableWarning
                      onBlur={(e) => {
                        const updated = [...criteria];
                        updated[i] = e.currentTarget.textContent ?? "";
                        setCriteria(updated.filter(Boolean));
                      }}
                      className="flex-1 text-[12px] text-fg-1 focus:outline-none"
                    >
                      {c}
                    </span>
                    <button
                      onClick={() =>
                        setCriteria(criteria.filter((_, j) => j !== i))
                      }
                      className="mt-0.5 text-fg-3 hover:text-fg-0"
                    >
                      <X className="h-3 w-3" strokeWidth={1.75} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
        <button
          onClick={onClose}
          className="rounded-md px-3 py-1.5 text-[12.5px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
        >
          Cancel
        </button>
        <button
          onClick={onCreate}
          disabled={!title.trim() || creating}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12.5px] font-medium transition-colors",
            !title.trim() || creating
              ? "cursor-not-allowed bg-bg-3 text-fg-3"
              : "bg-accent text-accent-fg hover:bg-accent-hover",
          )}
        >
          {creating ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2} />
          ) : (
            <Zap className="h-3.5 w-3.5" strokeWidth={2} />
          )}
          {creating
            ? "Creating…"
            : `Create ${issueType}${projectKey ? ` in ${projectKey}` : ""}`}
        </button>
      </div>
    </div>
  );
}

// ── Slack draft panel ─────────────────────────────────────────

// Resolve the real Slack channel for the conversation's primary repo
// from RepoOwnership.slack_channel — NOT a guessed `<repo>-collab`
// slug. Returns the channel (no leading #) + which repo it came from,
// or null when ownership has no channel on file (UI then shows an empty
// field with a placeholder rather than a wrong guess).
export type ChannelSource = {
  channel: string;
  fromRepo: string;
} | null;

// Pure resolution used by the panel + unit-tested directly. Prefers an
// exact repo-name match that has a channel on file, then any result
// with a channel. Returns null when no result carries a channel — the
// UI then leaves the field empty (we never invent a `<repo>-collab`).
export function resolveChannelFromOwnership(
  results: { repo_name: string; slack_channel: string | null }[],
  primaryRepo: string,
): ChannelSource {
  const exact = results.find(
    (r) => r.repo_name.toLowerCase() === primaryRepo.toLowerCase(),
  );
  const hit =
    (exact?.slack_channel ? exact : null) ??
    results.find((r) => r.slack_channel);
  if (!hit?.slack_channel) return null;
  return {
    channel: hit.slack_channel.replace(/^#/, ""),
    fromRepo: hit.repo_name,
  };
}

function SlackDraftPanel({
  messages,
  reposUsedNames = [],
  onClose,
}: {
  messages: ConversationMessage[];
  reposUsedNames?: string[];
  onClose: () => void;
}) {
  const primaryRepo = reposUsedNames[0] ?? "";

  // Look up the real channel from ownership for the primary repo.
  const ownershipQ = useQuery({
    queryKey: ["ownership-channel", primaryRepo],
    queryFn: () => lookupOwnership(primaryRepo),
    enabled: !!primaryRepo,
    staleTime: 5 * 60_000,
  });

  const resolved: ChannelSource = useMemo(
    () => resolveChannelFromOwnership(ownershipQ.data?.results ?? [], primaryRepo),
    [ownershipQ.data, primaryRepo],
  );

  // The single channel the draft targets. Seeded once from ownership
  // (don't clobber the user's edit on re-render); empty until resolved.
  const [channel, setChannel] = useState("");
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current || !resolved) return;
    seededRef.current = true;
    setChannel(resolved.channel);
  }, [resolved]);

  const [draft, setDraft] = useState(() => buildSlackDraft(messages));
  const [copied, setCopied] = useState(false);
  const [sending, setSending] = useState(false);
  const [sentLink, setSentLink] = useState<string | null>(null);

  // Slack-formatted body — converted at copy/send time so the textarea
  // stays editable as plain Markdown.
  const slackBody = useMemo(() => toSlackMrkdwn(draft), [draft]);

  const onCopy = useCallback(async () => {
    await navigator.clipboard.writeText(slackBody);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    toast.success("Copied (Slack formatting) — paste into Slack");
  }, [slackBody]);

  const onSendDraft = useCallback(async () => {
    const ch = channel.trim().replace(/^#/, "");
    if (!ch || sending || !draft.trim()) return;
    setSending(true);
    setSentLink(null);
    try {
      const res = await createSlackDraft(ch, slackBody);
      if (res.ok) {
        setSentLink(res.channel_link ?? null);
        toast.success(`Draft created in #${ch} — review & send in Slack`);
      } else {
        toast.error(
          res.error
            ? `Couldn't create Slack draft: ${res.error}`
            : "Couldn't create Slack draft — copy & paste instead",
        );
      }
    } catch (err) {
      toast.error(`Slack draft failed: ${describeError(err)}`);
    } finally {
      setSending(false);
    }
  }, [channel, slackBody, sending, draft]);

  const channelKnown = !!channel.trim();
  const fromOwnership = resolved && channel === resolved.channel;

  return (
    <div className="mb-3 rounded-xl border border-border bg-bg-1">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <MessageSquare
            className="h-3.5 w-3.5 text-accent"
            strokeWidth={1.75}
          />
          <span className="text-[13px] font-semibold text-fg-0">
            Draft Slack Message
          </span>
        </div>
        <button onClick={onClose} className="text-fg-3 hover:text-fg-0">
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </button>
      </div>

      <div className="space-y-4 p-4">
        {/* Channel — single, resolved from real ownership */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
            Channel
            {ownershipQ.isLoading && (
              <Loader2 className="h-3 w-3 animate-spin" strokeWidth={1.75} />
            )}
            {fromOwnership && (
              <span className="font-normal normal-case text-fg-3">
                — from {resolved.fromRepo} ownership
              </span>
            )}
          </label>
          <div className="flex items-center gap-1.5">
            <span className="text-[12px] text-fg-3">#</span>
            <input
              type="text"
              value={channel}
              onChange={(e) => setChannel(e.target.value.replace(/^#/, ""))}
              placeholder={
                ownershipQ.isLoading
                  ? "resolving channel…"
                  : "no channel on file — type one"
              }
              className="flex-1 rounded border border-border bg-bg-2 px-2 py-1.5 font-mono text-[12px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
            />
          </div>
          {!ownershipQ.isLoading && !resolved && (
            <p className="mt-1 text-[10.5px] text-fg-3">
              No Slack channel recorded
              {primaryRepo ? ` for ${primaryRepo}` : ""} — enter one (we don't
              guess).
            </p>
          )}
        </div>

        {/* Draft */}
        <div>
          <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
            Draft
            <span className="ml-1.5 font-normal normal-case text-fg-3">
              — edit as Markdown; converted to Slack formatting on copy/send
            </span>
          </label>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={6}
            className="w-full resize-none rounded-md border border-border bg-bg-2 px-3 py-2.5 text-[12.5px] leading-relaxed text-fg-0 focus:border-accent focus:outline-none"
          />
        </div>

        {sentLink && (
          <a
            href={sentLink}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-[11.5px] text-accent hover:underline"
          >
            <ExternalLink className="h-3 w-3" strokeWidth={1.75} />
            Open the draft in Slack
          </a>
        )}

        {/* Actions */}
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-3">
          <button
            onClick={onCopy}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-2 px-3 py-1.5 text-[12px] font-medium text-fg-1 hover:bg-bg-1 hover:text-fg-0"
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-ok" strokeWidth={2} />
            ) : (
              <Copy className="h-3.5 w-3.5" strokeWidth={1.75} />
            )}
            Copy to clipboard
          </button>
          <button
            onClick={onSendDraft}
            disabled={!channelKnown || sending || !draft.trim()}
            title={
              channelKnown
                ? `Create a draft in #${channel.trim()} (you review & send in Slack)`
                : "Set a channel first"
            }
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors",
              !channelKnown || sending || !draft.trim()
                ? "cursor-not-allowed bg-bg-3 text-fg-3"
                : "bg-accent text-accent-fg hover:bg-accent-hover",
            )}
          >
            {sending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2} />
            ) : (
              <MessageSquare className="h-3.5 w-3.5" strokeWidth={1.75} />
            )}
            {sending
              ? "Drafting…"
              : channelKnown
                ? `Send draft to #${channel.trim()}`
                : "Send draft to Slack"}
          </button>
        </div>
        <p className="text-right text-[10.5px] text-fg-3">
          "Send draft" creates a Slack draft in your Drafts &amp; Sent — it is
          NOT posted until you send it in Slack.
        </p>
      </div>
    </div>
  );
}

// ── Handover trigger panel ────────────────────────────────────

function HandoverTriggerPanel({
  messages,
  onClose,
}: {
  messages: ConversationMessage[];
  onClose: () => void;
}) {
  const topSources = useMemo(() => {
    const counts = new Map<string, number>();
    for (const msg of messages) {
      for (const src of msg.sources ?? []) {
        if (src.name) counts.set(src.name, (counts.get(src.name) ?? 0) + 1);
      }
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4)
      .map(([name]) => name);
  }, [messages]);

  return (
    <div className="mb-3 rounded-xl border border-border bg-bg-1 px-4 py-3.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BookOpen className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          <span className="text-[13px] font-semibold text-fg-0">
            Generate Handover Doc
          </span>
        </div>
        <button onClick={onClose} className="text-fg-3 hover:text-fg-0">
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </button>
      </div>
      <p className="mt-2 text-[12.5px] text-fg-2">
        Create a living handover doc for a service or repo discussed in this
        conversation — the clarifier will use it to answer future questions
        without asking humans.
      </p>
      {topSources.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[10.5px] text-fg-3">Referenced here:</span>
          {topSources.map((s) => (
            <span
              key={s}
              className="rounded-full border border-border bg-bg-2 px-2 py-0.5 font-mono text-[11px] text-fg-1"
            >
              {s}
            </span>
          ))}
        </div>
      )}
      <div className="mt-3 flex justify-end">
        <Link
          to="/collaboration/handovers"
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-fg hover:bg-accent-hover"
        >
          <BookOpen className="h-3.5 w-3.5" strokeWidth={1.75} />
          Go to Handover Docs
        </Link>
      </div>
    </div>
  );
}

// ── Session start panel ───────────────────────────────────────

function SessionStartPanel({
  messages,
  onClose,
}: {
  messages: ConversationMessage[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const lastUser = useMemo(
    () => [...messages].reverse().find((m) => m.role === "user"),
    [messages],
  );
  const [requirement, setRequirement] = useState(
    lastUser?.content.slice(0, 400) ?? "",
  );

  const onCreate = useCallback(async () => {
    if (!requirement.trim() || creating) return;
    setCreating(true);
    try {
      const { id } = await createCollabSession(requirement.trim());
      navigate(`/collaboration/sessions/${id}`);
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
      setCreating(false);
    }
  }, [requirement, creating, navigate]);

  return (
    <div className="mb-3 rounded-xl border border-border bg-bg-1">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Users className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          <span className="text-[13px] font-semibold text-fg-0">
            Brief Another Squad
          </span>
        </div>
        <button onClick={onClose} className="text-fg-3 hover:text-fg-0">
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </button>
      </div>
      <div className="space-y-3 p-4">
        <p className="text-[12.5px] text-fg-2">
          Captures what you need from another squad. Bifrost asks clarifying
          questions, resolves open points, and produces a handover brief you
          can share — no back-and-forth emails needed.
        </p>
        <textarea
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
          rows={3}
          placeholder="What do you need the other team to do or decide?"
          className="w-full resize-none rounded-md border border-border bg-bg-2 px-3 py-2 text-[13px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md px-3 py-1.5 text-[12.5px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          >
            Cancel
          </button>
          <button
            onClick={onCreate}
            disabled={!requirement.trim() || creating}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12.5px] font-medium",
              !requirement.trim() || creating
                ? "cursor-not-allowed bg-bg-3 text-fg-3"
                : "bg-accent text-accent-fg hover:bg-accent-hover",
            )}
          >
            {creating && (
              <Loader2
                className="h-3.5 w-3.5 animate-spin"
                strokeWidth={2}
              />
            )}
            Create brief
          </button>
        </div>
      </div>
    </div>
  );
}
