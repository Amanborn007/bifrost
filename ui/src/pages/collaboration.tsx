import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  Activity,
  AlertTriangle,
  Archive,
  ArchiveRestore,
  ArrowUp,
  BookOpen,
  Check,
  ChevronRight,
  CircleDot,
  ClipboardCheck,
  ClipboardList,
  ExternalLink,
  FileText,
  GitBranch,
  GitCompareArrows,
  GitPullRequest,
  Globe,
  HelpCircle,
  History,
  Loader2,
  Pencil,
  Plus,
  RefreshCcw,
  RotateCcw,
  Save,
  Search,
  Sparkles,
  User,
  Users,
  X,
  Zap,
} from "lucide-react";
import {
  ApiError,
  appendCollabMessage,
  archiveHandoverDoc,
  closeCollabSession,
  createCollabSession,
  createTask,
  estimateDomainHandoverCost,
  estimateTeamHandoverCost,
  estimateRegionHandoverCost,
  estimateRepoHandoverCost,
  generateDomainHandover,
  generateTeamHandover,
  generateRegionHandover,
  generateRepoHandover,
  HANDOVER_SECTION_LABELS,
  getCollabSession,
  getHandoverAudits,
  getHandoverBodyHistory,
  getHandoverDoc,
  getHandoverDoubtCandidates,
  getKnowledgeOverview,
  lookupOwnership,
  listCollabSessions,
  listDomains,
  listHandoverDocs,
  listRepos,
  patchHandoverDocBody,
  promoteDoubtCandidate,
  regenerateHandoverSection,
  reopenCollabSession,
  linkTaskToCollabSession,
  resolveCollabDoubt,
  restoreHandoverDoc,
  searchHandoverDocs,
  type CollabDoubt,
  type CollabDoubtStatus,
  type CollabMessage,
  type CollabSessionDetail,
  type CollabSessionState,
  type CollabSessionSummary,
  type DomainSummary,
  type DoubtCandidate,
  type HandoverAuditEntry,
  type HandoverAuditEventType,
  type HandoverBodyHistoryEntry,
  type HandoverBodyHistoryEventType,
  type HandoverCostEstimate,
  type HandoverDocSummary,
  type HandoverSearchHit,
  type HandoverSectionKey,
} from "@/lib/api";
import { subscribeToCollabStream } from "@/lib/sse";
import { Skeleton } from "@/components/ui/skeleton";
import { Prose } from "@/components/prose";
import { cn, describeError, formatRelativeDate } from "@/lib/utils";
import { HandoverOwnerInbox } from "@/components/handover-owner-inbox";
import { HandoverReferencingSessions } from "@/components/handover-referencing-sessions";
import {
  HandoverFeedbackList,
  HandoverSectionFeedback,
} from "@/components/handover-section-feedback";
import { HandoverRestoreVersion } from "@/components/handover-restore-version";
import { IntegrationStatus } from "@/components/integration-status";

/**
 * Collaboration UI shell.
 *
 * Three pages share this file because they're all thin and share
 * helpers. Split into separate files when one outgrows ~300 lines.
 *
 *   /collaboration           → landing (sessions list + new CTA)
 *   /collaboration/new       → requirement form
 *   /collaboration/sessions/:id → chat + doubt panel + close
 *
 * The clarifier_agent and synthesizer_agent that produce real
 * conversation content land in a follow-up commit. For now this round-trips
 * the human side: requester messages, manual doubt resolution,
 * close-with-risks-accepted.
 */

const POLL_INTERVAL = 15_000;

/* ─── Landing page ───────────────────────────────────────── */

export function CollaborationLandingPage() {
  const sessionsQ = useQuery({
    queryKey: ["collab-sessions"],
    queryFn: () => listCollabSessions({ limit: 100 }),
    refetchInterval: POLL_INTERVAL,
  });
  const recentHandoversQ = useQuery({
    queryKey: ["handover-docs-recent"],
    queryFn: () => listHandoverDocs({ limit: 5 }),
    staleTime: 60_000,
  });
  const [ownershipSearch, setOwnershipSearch] = useState("");
  const [debouncedOwnershipQ, setDebouncedOwnershipQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedOwnershipQ(ownershipSearch.trim()), 350);
    return () => clearTimeout(t);
  }, [ownershipSearch]);
  const ownershipQ = useQuery({
    queryKey: ["ownership", debouncedOwnershipQ],
    queryFn: () => lookupOwnership(debouncedOwnershipQ),
    enabled: debouncedOwnershipQ.length >= 2,
    staleTime: 60_000,
  });

  const list = useMemo(() => sessionsQ.data ?? [], [sessionsQ.data]);
  const counts = useMemo(() => bucketCounts(list), [list]);
  const activeSessionCount = counts.open + counts.clarifying;

  return (
    <div className="mx-auto max-w-[900px] space-y-6 px-8 py-8">

      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="flex items-start justify-between gap-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-fg-0">
            Collaboration
          </h1>
          <p className="mt-1 text-[13.5px] text-fg-2">
            Clarify cross-team requirements and produce handover briefs. Each
            session captures open questions and resolves them before work starts.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2 pt-1">
          <Link
            to="/collaboration/handovers"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-1 px-3 py-1.5 text-[12.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
          >
            <BookOpen className="h-3.5 w-3.5" strokeWidth={1.75} />
            Handover docs
          </Link>
          <Link
            to="/collaboration/new"
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-fg hover:bg-accent-hover"
          >
            <Plus className="h-3.5 w-3.5" strokeWidth={2} />
            New session
          </Link>
        </div>
      </header>

      {/* ── Summary strip — single tile ───────────────────────── */}
      <LandingStat
        icon={<Users className="h-4 w-4" strokeWidth={1.75} />}
        label="Active sessions"
        value={activeSessionCount}
        note={
          list.length > 0
            ? `${list.length} total · ${counts.resolved + counts.briefed} resolved`
            : "No sessions yet"
        }
        tone={activeSessionCount > 0 ? "info" : "default"}
        loading={sessionsQ.isLoading}
      />

      {/* ── Sessions ───────────────────────────────────────────── */}
      <DashSection
        title="Sessions"
        icon={<Users className="h-4 w-4" strokeWidth={1.75} />}
        cta={
          <Link
            to="/collaboration/new"
            className="inline-flex items-center gap-1 text-[12px] font-medium text-accent hover:underline"
          >
            <Plus className="h-3.5 w-3.5" strokeWidth={2} />
            New session
          </Link>
        }
      >
        {sessionsQ.isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-14 rounded-lg" />
            ))}
          </div>
        ) : list.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-6 py-12 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-bg-2">
              <Users className="h-6 w-6 text-fg-3" strokeWidth={1.25} />
            </div>
            <p className="text-[14px] font-medium text-fg-1">No sessions yet</p>
            <p className="mt-2 mx-auto max-w-sm text-[12.5px] leading-relaxed text-fg-3">
              Start a session when you need to clarify a cross-team requirement.
              An AI walks through open questions with you and generates a
              handover brief for the receiving squad.
            </p>
            <Link
              to="/collaboration/new"
              className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-accent px-4 py-2 text-[13px] font-medium text-accent-fg hover:bg-accent-hover"
            >
              <Plus className="h-3.5 w-3.5" strokeWidth={2} />
              Start a session
            </Link>
          </div>
        ) : (
          <>
            {activeSessionCount > 0 && (
              <div className="mb-3 flex items-center gap-2">
                <span className="badge badge-info">
                  {activeSessionCount} active
                </span>
                {counts.resolved + counts.briefed > 0 && (
                  <span className="badge badge-ok">
                    {counts.resolved + counts.briefed} resolved
                  </span>
                )}
              </div>
            )}
            <ul className="max-h-[320px] overflow-y-auto rounded-lg border border-border bg-bg-1">
              {list.map((s, i) => (
                <SessionRow key={s.id} session={s} first={i === 0} />
              ))}
            </ul>
            {list.length > 5 && (
              <p className="mt-1.5 text-right text-[11.5px] text-fg-3">
                Showing {list.length} sessions — scroll to see all
              </p>
            )}
          </>
        )}
      </DashSection>

      {/* ── Who owns this? (repo ownership DB) ────────────────── */}
      <DashSection
        title="Who owns this?"
        icon={<Search className="h-4 w-4" strokeWidth={1.75} />}
      >
        <div className="space-y-3">
          <input
            type="text"
            value={ownershipSearch}
            onChange={(e) => setOwnershipSearch(e.target.value)}
            placeholder="Repo, service or squad name…"
            className="w-full rounded-md border border-border bg-bg-2 px-3 py-2 text-[13px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
          />
          {ownershipQ.isLoading && (
            <div className="space-y-2">
              <Skeleton className="h-5 rounded" />
              <Skeleton className="h-12 rounded" />
            </div>
          )}
          {debouncedOwnershipQ.length >= 2 &&
            !ownershipQ.isLoading &&
            (ownershipQ.data?.results.length ?? 0) === 0 && (
              <p className="text-[12px] text-fg-3">No results found.</p>
          )}
          {ownershipQ.data && ownershipQ.data.results.length > 0 && (
            <ul className="space-y-2">
              {ownershipQ.data.results.slice(0, 5).map((r) => (
                <li
                  key={r.repo_name}
                  className="rounded-md border border-border bg-bg-2 px-4 py-3"
                >
                  <div className="flex items-center gap-2 text-[12.5px] text-fg-0">
                    <span className="font-mono font-medium">{r.repo_name}</span>
                    {r.squad && (
                      <span className="rounded-full bg-bg-3 px-2 py-0.5 text-[10.5px] text-fg-1">
                        {r.squad}
                      </span>
                    )}
                  </div>
                  {(r.slack_channel || r.primary_contact) && (
                    <p className="mt-1 text-[11.5px] text-fg-2">
                      {[r.slack_channel, r.primary_contact].filter(Boolean).join(" · ")}
                    </p>
                  )}
                  {r.description && (
                    <p className="mt-1 text-[11.5px] leading-snug text-fg-3 line-clamp-2">
                      {r.description}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </DashSection>

      {/* ── Recent handover docs ───────────────────────────────── */}
      <DashSection
        title="Recent handover docs"
        icon={<BookOpen className="h-4 w-4" strokeWidth={1.75} />}
        cta={
          <Link
            to="/collaboration/handovers"
            className="text-[12px] font-medium text-accent hover:underline"
          >
            All docs →
          </Link>
        }
        headerRight={<IntegrationStatus source="extraction" className="mr-1" />}
      >
        {recentHandoversQ.isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-10 rounded-md" />
            ))}
          </div>
        ) : (recentHandoversQ.data?.length ?? 0) === 0 ? (
          <div className="rounded-md bg-bg-2 px-4 py-3">
            <p className="text-[12.5px] text-fg-2">
              No handover docs generated yet.
            </p>
            <Link
              to="/collaboration/handovers"
              className="mt-1 block text-[11.5px] font-medium text-accent hover:underline"
            >
              Generate your first handover →
            </Link>
          </div>
        ) : (
          <ul className="overflow-hidden rounded-lg border border-border bg-bg-1 divide-y divide-border">
            {(recentHandoversQ.data ?? []).map((d) => (
              <li
                key={d.id}
                className="flex items-center gap-3 px-3 py-2.5 transition-colors hover:bg-bg-2"
              >
                <FreshnessDot state={d.freshness_state} />
                <div className="min-w-0 flex-1">
                  <Link
                    to={`/collaboration/handovers/${d.id}`}
                    className="truncate text-[12.5px] font-medium text-fg-0 hover:text-accent"
                  >
                    {d.scope_value}
                  </Link>
                  <div className="text-[11px] text-fg-3">
                    {d.scope_kind === "team" ? "Team" :
                     d.scope_kind === "domain" ? "Domain" :
                     d.scope_kind === "region" ? "Region" : "Repo"} handover
                    {" · "}
                    <span className={d.freshness_state === "fresh" ? "text-ok" : "text-warn"}>
                      {d.freshness_state}
                    </span>
                    {d.last_generated_at &&
                      ` · ${formatRelativeDate(d.last_generated_at)}`}
                  </div>
                </div>
                <Link
                  to={`/collaboration/handovers/${d.id}`}
                  className="shrink-0 text-[11.5px] font-medium text-accent hover:underline"
                >
                  View →
                </Link>
              </li>
            ))}
          </ul>
        )}
      </DashSection>
    </div>
  );
}

/* ── Landing helpers ─────────────────────────────────────── */

function LandingStat({
  icon,
  label,
  value,
  note,
  tone = "default",
  loading,
}: {
  icon: ReactNode;
  label: string;
  value: number;
  note?: string;
  tone?: "default" | "ok" | "warn" | "info";
  loading?: boolean;
}) {
  const valueColor = {
    default: "text-fg-0",
    ok: "text-ok",
    warn: "text-warn",
    info: "text-info",
  }[tone];
  return (
    <div className="rounded-xl border border-border bg-bg-1 px-4 py-3.5">
      <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-fg-2">
        {icon}
        {label}
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-12" />
      ) : (
        <div className={cn("mt-1.5 text-[26px] font-semibold leading-none tabular", valueColor)}>
          {value}
        </div>
      )}
      {note && !loading && (
        <div className="mt-1 text-[11px] text-fg-3">{note}</div>
      )}
    </div>
  );
}

function DashSection({
  title,
  icon,
  subtitle,
  cta,
  headerRight,
  compact = false,
  children,
}: {
  title: string;
  icon: ReactNode;
  subtitle?: string;
  cta?: ReactNode;
  headerRight?: ReactNode;
  compact?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-bg-1">
      <div className={cn(
        "flex items-center gap-2 border-b border-border",
        compact ? "px-3 py-2.5" : "px-4 py-3",
      )}>
        <span className="text-fg-2">{icon}</span>
        <div className="flex-1 min-w-0">
          <span className={cn(
            "font-semibold text-fg-0",
            compact ? "text-[12.5px]" : "text-[13.5px]",
          )}>
            {title}
          </span>
          {subtitle && (
            <span className="ml-2 text-[11.5px] text-fg-3">{subtitle}</span>
          )}
        </div>
        {cta && <div className="shrink-0">{cta}</div>}
        {headerRight && <div className="shrink-0">{headerRight}</div>}
      </div>
      <div className={cn(compact ? "p-3" : "p-4")}>
        {children}
      </div>
    </section>
  );
}

const SessionRow = memo(function SessionRow({
  session,
  first,
}: {
  session: CollabSessionSummary;
  first: boolean;
}) {
  const cfg = stateConfig(session.state);
  const hasBrief = session.state === "briefed";
  return (
    <li className={cn(!first && "border-t border-border")}>
      <Link
        to={`/collaboration/sessions/${session.id}`}
        className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-bg-2"
      >
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-medium text-fg-0">
            {session.requirement_text}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {/* State badge */}
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] font-semibold",
                cfg.pill,
              )}
            >
              <span className={cn("h-1.5 w-1.5 rounded-full", cfg.dot)} />
              {cfg.label}
            </span>
            {/* Brief ready */}
            {hasBrief && (
              <span className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-2 py-0.5 text-[10.5px] font-medium text-accent">
                <FileText className="h-2.5 w-2.5" strokeWidth={2} />
                Brief ready
              </span>
            )}
            {/* Requester — only show if it's a readable name (email prefix),
                not a raw UUID/hex ID */}
            {session.requester.includes("@") && (
              <span className="font-mono text-[10.5px] text-fg-3">
                {session.requester.split("@")[0]}
              </span>
            )}
            <span className="text-[10.5px] text-fg-3">
              {formatRelativeDate(session.created_at)}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="font-mono text-[11px] text-fg-3">
            {session.message_count}msg
          </span>
          {session.open_doubt_count > 0 && (
            <span
              title={`${session.open_doubt_count} open doubts`}
              className="inline-flex items-center gap-1 rounded-full bg-warn-soft px-2 py-0.5 text-[11px] text-warn"
            >
              <HelpCircle className="h-2.5 w-2.5" strokeWidth={1.75} />
              {session.open_doubt_count}
            </span>
          )}
          {(session.linked_task_count ?? 0) > 0 && (
            <span
              title={`${session.linked_task_count} linked task${session.linked_task_count !== 1 ? "s" : ""}: ${(session.linked_task_keys ?? []).join(", ")}`}
              className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-2 py-0.5 text-[11px] text-accent"
            >
              <ClipboardList className="h-2.5 w-2.5" strokeWidth={2} />
              {session.linked_task_count}
            </span>
          )}
          <ChevronRight className="h-3.5 w-3.5 text-fg-3" strokeWidth={1.75} />
        </div>
      </Link>
    </li>
  );
});

/* ─── New session page ───────────────────────────────────── */

export function CollaborationNewPage() {
  const [requirement, setRequirement] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();
  const ref = useRef<HTMLTextAreaElement | null>(null);

  // P5: support deep-link pre-scoping from a handover detail page.
  // When the user clicks "Ask about this handover" (handover-ask-button
  // component), they land here with ``?from_handover=<id>&section=<key>``.
  // We fetch the handover context to:
  //   1. Pre-fill the requirement textarea with a templated lead-in
  //      that references the handover so the clarifier knows the
  //      caller is mid-read.
  //   2. Show a "context" pill above the form so the user can see the
  //      session is going to be linked to a specific doc.
  const [searchParams] = useSearchParams();
  const fromHandover = searchParams.get("from_handover");
  const fromSection = searchParams.get("section");
  const handoverContext = useQuery({
    queryKey: ["handover-doc", fromHandover],
    queryFn: () => getHandoverDoc(fromHandover!),
    enabled: !!fromHandover,
  });

  useEffect(() => {
    ref.current?.focus();
  }, []);

  // Once the handover context loads, seed the textarea ONCE with a
  // templated prompt — but only if the user hasn't already typed
  // anything. We track this via a ref so a subsequent re-render
  // doesn't blow away their edits.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    if (!handoverContext.data) return;
    if (requirement.trim().length > 0) return;
    seededRef.current = true;
    const data = handoverContext.data;
    const sectionPart = fromSection
      ? ` (section: **${fromSection.replace(/_/g, " ")}**)`
      : "";
    // One-time seed from async-loaded handover data (ref-guarded), paired
    // with the imperative caret/focus below — an external-sync effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setRequirement(
      `I'm reading the handover for **${data.scope_value}** (${data.scope_kind})${sectionPart} and need clarity on:\n\n`,
    );
    // Move caret to the end so the user can type immediately.
    requestAnimationFrame(() => {
      const el = ref.current;
      if (el) {
        el.setSelectionRange(el.value.length, el.value.length);
        el.focus();
      }
    });
  }, [handoverContext.data, fromSection, requirement]);

  const onSubmit = useCallback(async () => {
    const trimmed = requirement.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      const { id } = await createCollabSession(trimmed);
      navigate(`/collaboration/sessions/${id}`);
    } catch (err) {
      toast.error(`Couldn't start session: ${describeError(err)}`);
      setSubmitting(false);
    }
  }, [requirement, submitting, navigate]);

  return (
    <div className="mx-auto max-w-[720px] space-y-6 px-8 py-10">
      <header>
        <Link
          to={fromHandover ? `/collaboration/handovers/${fromHandover}` : "/collaboration"}
          className="text-[12px] font-medium text-fg-2 hover:text-fg-0"
        >
          ← {fromHandover ? "Back to handover" : "Back to sessions"}
        </Link>
        <h1 className="mt-3 text-[22px] font-semibold tracking-tight text-fg-0">
          Start a collaboration session
        </h1>
        <p className="mt-1 text-[13.5px] text-fg-2">
          Describe the feature or change you need from another team. Bifrost
          will clarify the ask, surface open doubts, and produce a brief you
          can take to the owning team.
        </p>
        {handoverContext.data && (
          <div className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-accent/40 bg-accent-soft px-2.5 py-1 text-[11.5px] font-medium text-accent">
            <Sparkles className="h-3 w-3" strokeWidth={1.75} />
            Linked to {handoverContext.data.scope_kind}/
            <span className="font-mono">{handoverContext.data.scope_value}</span>
            {fromSection && <span> · {fromSection.replace(/_/g, " ")}</span>}
          </div>
        )}
      </header>

      <div className="rounded-lg border border-border bg-bg-1 p-1">
        <textarea
          ref={ref}
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              onSubmit();
            }
          }}
          placeholder="e.g. We need to add a new carrier to shipping — what would it take from the fulfillment team?"
          className="h-40 w-full resize-none bg-transparent px-4 py-3 text-[14px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
        />
        <div className="flex items-center justify-between border-t border-border px-3 py-2">
          <span className="text-[11px] text-fg-3">
            <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
              ⌘
            </kbd>{" "}
            +{" "}
            <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
              Enter
            </kbd>{" "}
            to submit
          </span>
          <button
            onClick={onSubmit}
            disabled={!requirement.trim() || submitting}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12.5px] font-medium",
              requirement.trim() && !submitting
                ? "bg-accent text-accent-fg hover:bg-accent-hover"
                : "bg-bg-3 text-fg-3",
            )}
          >
            {submitting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2} />
            ) : (
              <ArrowUp className="h-3.5 w-3.5" strokeWidth={2} />
            )}
            {submitting ? "Starting…" : "Start session"}
          </button>
        </div>
      </div>

      <aside className="rounded-md border border-dashed border-border bg-bg-1/40 px-4 py-3 text-[12px] text-fg-2">
        <p>
          <span className="font-semibold text-fg-1">Tip.</span> Be concrete
          about the outcome you want, not the implementation. The clarifier
          will ask follow-ups for the parts it can't resolve from the
          handover docs.
        </p>
      </aside>
    </div>
  );
}

/* ─── Session detail page ────────────────────────────────── */

export function CollaborationSessionPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();

  const session = useQuery({
    queryKey: ["collab-session", id],
    queryFn: () => getCollabSession(id!),
    enabled: !!id,
    // No polling — live updates flow through the SSE subscription
    // below. We keep a long-stale window so a tab focus + manual
    // refetch (via react-query devtools or a Refresh button) still
    // works if the SSE connection is stuck.
    staleTime: 60_000,
  });

  const [composer, setComposer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [closing, setClosing] = useState(false);
  const [reopening, setReopening] = useState(false);
  const [showTaskPanel, setShowTaskPanel] = useState(false);
  // Tracks which agent (if any) is currently running an LLM call so
  // the UI can show a "Bifrost is thinking…" pill. Cleared on
  // ``agent_done`` event. Not a ref because the JSX needs to re-render.
  const [agentRunning, setAgentRunning] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  // Live SSE subscription — replaces the 15s polling. Each event
  // either invalidates the session query (forcing a refetch) or
  // toggles the agent-running indicator. Disposed on unmount or id
  // change; reconnect-with-replay handles network blips.
  useEffect(() => {
    if (!id) return;
    return subscribeToCollabStream(id, (e) => {
      if (e.event_type === "session_updated") {
        qc.invalidateQueries({ queryKey: ["collab-session", id] });
        // Also nudge the sidebar list if we ever expose it from this page.
        qc.invalidateQueries({ queryKey: ["collab-sessions"] });
      } else if (e.event_type === "agent_running") {
        const agent = (e.data?.agent as string | undefined) ?? "agent";
        setAgentRunning(agent);
      } else if (e.event_type === "agent_done") {
        setAgentRunning(null);
        // ``ok=false`` means the agent fell back; surface a soft
        // toast so the user knows the turn that just landed is a
        // graceful fallback rather than a real LLM response.
        if (e.data?.ok === false) {
          const agent = (e.data?.agent as string | undefined) ?? "Agent";
          toast.warning(`${agent} fell back — see system message in chat.`);
        }
      }
    });
  }, [id, qc]);

  // Auto-scroll the message column to the bottom on new messages.
  // Same rAF-coalesced pattern as the chat page so we don't shake.
  useEffect(() => {
    const sentinel = endRef.current;
    if (!sentinel) return;
    const scroller = sentinel.parentElement;
    if (!scroller) return;
    const handle = requestAnimationFrame(() => {
      scroller.scrollTop = scroller.scrollHeight;
    });
    return () => cancelAnimationFrame(handle);
  }, [session.data?.messages.length]);

  const onSend = useCallback(async () => {
    const trimmed = composer.trim();
    if (!trimmed || submitting || !id) return;
    setSubmitting(true);
    try {
      await appendCollabMessage(id, trimmed);
      setComposer("");
      qc.invalidateQueries({ queryKey: ["collab-session", id] });
    } catch (err) {
      toast.error(`Send failed: ${describeError(err)}`);
    } finally {
      setSubmitting(false);
    }
  }, [composer, id, submitting, qc]);

  const onClose = useCallback(async () => {
    if (!id) return;
    const data = session.data;
    if (!data) return;
    const openDoubts = data.doubts.filter((d) => d.status === "open").length;
    let acceptRisks = false;
    if (openDoubts > 0) {
      acceptRisks = window.confirm(
        `${openDoubts} open doubt${openDoubts === 1 ? "" : "s"} remain. ` +
          `Close anyway and brief them as open risks?`,
      );
      if (!acceptRisks) return;
    } else if (
      !window.confirm("Close this session and produce the brief?")
    ) {
      return;
    }
    setClosing(true);
    try {
      await closeCollabSession(id, acceptRisks);
      qc.invalidateQueries({ queryKey: ["collab-session", id] });
      qc.invalidateQueries({ queryKey: ["collab-sessions"] });
      toast.success("Session closed");
    } catch (err) {
      toast.error(`Close failed: ${describeError(err)}`);
    } finally {
      setClosing(false);
    }
  }, [id, session.data, qc]);

  const onReopen = useCallback(async () => {
    if (!id || reopening) return;
    if (!window.confirm("Reopen this session for more clarifications?")) return;
    setReopening(true);
    try {
      await reopenCollabSession(id);
      qc.invalidateQueries({ queryKey: ["collab-session", id] });
      qc.invalidateQueries({ queryKey: ["collab-sessions"] });
      toast.success("Session reopened");
    } catch (err) {
      toast.error(`Reopen failed: ${describeError(err)}`);
    } finally {
      setReopening(false);
    }
  }, [id, reopening, qc]);

  if (session.isLoading) {
    return (
      <div className="mx-auto max-w-[1200px] space-y-3 px-8 py-8">
        <Skeleton className="h-12 w-2/3" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  if (session.isError || !session.data) {
    return (
      <div className="mx-auto max-w-[720px] px-8 py-12">
        <EmptyHero
          title="Session not found"
          body={describeError(session.error) || "We couldn't load this session."}
        />
        <div className="mt-4 text-center">
          <Link
            to="/collaboration"
            className="text-[13px] font-medium text-accent hover:underline"
          >
            ← Back to sessions
          </Link>
        </div>
      </div>
    );
  }

  const data = session.data;
  const isClosed = data.state === "resolved" || data.state === "briefed" || data.state === "closed";
  const openDoubts = data.doubts.filter((d) => d.status === "open");
  const resolvedDoubts = data.doubts.filter((d) => d.status !== "open");

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Top bar — state + meta + close */}
      <header className="border-b border-border bg-bg-1 px-6 py-3">
        <div className="mx-auto flex max-w-[1200px] items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Link
                to="/collaboration"
                className="text-[12px] font-medium text-fg-2 hover:text-fg-0"
              >
                Sessions
              </Link>
              <span className="text-fg-3">/</span>
              <StateBadge state={data.state} />
            </div>
            <h1
              className="mt-1 truncate text-[15px] font-semibold text-fg-0"
              title={data.requirement_text}
            >
              {data.requirement_text}
            </h1>
            <p className="mt-0.5 text-[11px] text-fg-3">
              <span className="font-mono">{data.requester.slice(0, 12)}</span>
              {" · "}
              opened {formatRelativeDate(data.created_at)}
              {data.resolved_at && (
                <>
                  {" · "}
                  resolved {formatRelativeDate(data.resolved_at)}
                </>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowTaskPanel((v) => !v)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-medium transition-colors",
                showTaskPanel
                  ? "border-accent bg-accent-soft text-accent"
                  : "border-border bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
              )}
            >
              <ClipboardList className="h-3 w-3" strokeWidth={1.75} />
              Create Jira task
            </button>
            {!isClosed && (
              <button
                onClick={onClose}
                disabled={closing}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium",
                  closing
                    ? "bg-bg-3 text-fg-3"
                    : "border border-border bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                )}
              >
                {closing ? (
                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                ) : (
                  <ClipboardCheck className="h-3 w-3" strokeWidth={1.75} />
                )}
                {closing ? "Closing…" : "Close & brief"}
              </button>
            )}
            {isClosed && (
              <button
                onClick={onReopen}
                disabled={reopening}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium",
                  reopening
                    ? "bg-bg-3 text-fg-3"
                    : "border border-border bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                )}
                title="Reopen for more clarifications."
              >
                {reopening ? (
                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                ) : (
                  <RotateCcw className="h-3 w-3" strokeWidth={1.75} />
                )}
                {reopening ? "Reopening…" : "Reopen"}
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Inline Jira task creation panel */}
      {showTaskPanel && (
        <div className="border-b border-border bg-bg-0 px-6 py-3">
          <div className="mx-auto max-w-[1200px]">
            <SessionJiraPanel
              session={data}
              onClose={() => setShowTaskPanel(false)}
            />
          </div>
        </div>
      )}

      {/* Three-column body: recent sessions | messages | doubts */}
      <div className="flex flex-1 min-h-0 gap-0">
        <SessionsRail activeId={id ?? null} />

        {/* Messages column (scrolls independently) */}
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-[760px] space-y-6 px-6 py-6">
              {data.messages.map((m) => (
                <MessageBubble key={m.id} msg={m} />
              ))}
              {agentRunning && <AgentThinking agent={agentRunning} />}
              {data.brief_md && <BriefPanel md={data.brief_md} sessionId={data.id} />}
              <div ref={endRef} className="h-2" />
            </div>
          </div>

          {/* Composer */}
          {!isClosed && (
            <div className="border-t border-border bg-bg-1">
              <div className="mx-auto max-w-[760px] px-6 py-3">
                <div className="rounded-lg border border-border bg-bg-0 p-1">
                  <textarea
                    ref={composerRef}
                    value={composer}
                    onChange={(e) => setComposer(e.target.value)}
                    onKeyDown={(e) => {
                      // Enter sends; Shift+Enter inserts a newline.
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        onSend();
                      }
                    }}
                    placeholder="Add a follow-up or answer a doubt…"
                    className="min-h-[64px] w-full resize-none bg-transparent px-3 py-2 text-[13.5px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
                  />
                  <div className="flex items-center justify-between px-2 pb-1">
                    <span className="text-[10.5px] text-fg-3">
                      <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                        Enter
                      </kbd>{" "}
                      to send,{" "}
                      <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                        Shift + Enter
                      </kbd>{" "}
                      newline
                    </span>
                    <button
                      onClick={onSend}
                      disabled={!composer.trim() || submitting}
                      className={cn(
                        "inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-medium",
                        composer.trim() && !submitting
                          ? "bg-accent text-accent-fg hover:bg-accent-hover"
                          : "bg-bg-3 text-fg-3",
                      )}
                    >
                      {submitting ? (
                        <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                      ) : (
                        <ArrowUp className="h-3 w-3" strokeWidth={2} />
                      )}
                      Send
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

        </main>

        {/* Right rail — doubts + artifacts */}
        <aside className="flex w-[320px] shrink-0 flex-col border-l border-border bg-bg-1/60">
          <div className="flex-1 overflow-y-auto px-4 py-5 space-y-6">
            <DoubtsSection
              title="Open doubts"
              doubts={openDoubts}
              sessionId={data.id}
              onChanged={() =>
                qc.invalidateQueries({ queryKey: ["collab-session", id] })
              }
            />
            {resolvedDoubts.length > 0 && (
              <DoubtsSection
                title="Resolved"
                doubts={resolvedDoubts}
                sessionId={data.id}
                onChanged={() =>
                  qc.invalidateQueries({ queryKey: ["collab-session", id] })
                }
                resolved
              />
            )}

            <ArtifactsSection artifacts={data.artifacts} />
          </div>
        </aside>
      </div>
    </div>
  );
}

/* ─── Message bubble ─────────────────────────────────────── */

const MessageBubble = memo(function MessageBubble({ msg }: { msg: CollabMessage }) {
  const isRequester = msg.role === "requester";
  const isSystem = msg.role === "system";
  const isAgent = msg.role === "clarifier_agent" || msg.role === "synthesizer_agent";

  return (
    <div className="flex items-start gap-3">
      <div
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
          isRequester && "bg-bg-3 text-fg-1",
          isSystem && "bg-bg-2 text-fg-2",
          isAgent && "bg-accent text-accent-fg",
        )}
        title={roleLabel(msg.role)}
      >
        {isRequester ? (
          <User className="h-3.5 w-3.5" strokeWidth={1.75} />
        ) : isAgent ? (
          <Sparkles className="h-3.5 w-3.5" strokeWidth={1.75} />
        ) : (
          <Users className="h-3.5 w-3.5" strokeWidth={1.75} />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-[11px] font-medium">
          <span className="text-fg-2">{roleLabel(msg.role)}</span>
          <span className="text-fg-3">
            {formatRelativeDate(msg.created_at)}
          </span>
        </div>
        {/* Requester messages render as plain text (whitespace preserved) so
            literal user typing isn't mangled by markdown parsing. Agents +
            system content are rendered as Prose (md). */}
        {isRequester ? (
          <div className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg-0">
            {msg.content_md}
          </div>
        ) : (
          <div className="max-w-[72ch]">
            <Prose>{msg.content_md}</Prose>
          </div>
        )}
        {msg.source_citations.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {msg.source_citations.map((urn) => (
              <span
                key={urn}
                className="inline-flex items-center rounded-full border border-border bg-bg-1 px-2 py-0.5 font-mono text-[10px] text-fg-2"
                title={urn}
              >
                {urn.slice(0, 48)}
                {urn.length > 48 ? "…" : ""}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

/* ─── Session → Jira task panel ────────────────────────── */

function SessionJiraPanel({
  session,
  onClose,
}: {
  session: import("@/lib/api").CollabSessionDetail;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [issueType, setIssueType] = useState<"Task" | "Bug" | "Story" | "Sub-task">("Task");
  const [title, setTitle] = useState(
    session.requirement_text.slice(0, 120),
  );
  const [projectKey, setProjectKey] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<{ id: string; title: string; jira_key?: string | null } | null>(null);

  const description = useMemo(() => {
    const parts: string[] = [session.requirement_text];
    if (session.brief_md) {
      parts.push("\n\n## Session Brief\n" + session.brief_md.slice(0, 1500));
    }
    if (session.doubts.filter((d) => d.status === "open").length > 0) {
      const openQ = session.doubts
        .filter((d) => d.status === "open")
        .map((d) => `- ${d.question}`)
        .join("\n");
      parts.push("\n\n## Open Questions\n" + openQ);
    }
    return parts.join("");
  }, [session]);

  const onCreate = useCallback(async () => {
    if (!title.trim() || creating) return;
    setCreating(true);
    try {
      const task = await createTask({
        title: title.trim(),
        intent_text: description,
        created_from: "direct",
      });
      setCreated(task);
      toast.success(`Task created: ${task.title.slice(0, 60)}`);
      // Link the new task back to this session
      try {
        await linkTaskToCollabSession(session.id.toString(), task.id);
      } catch {
        // non-fatal — linking failed but task was created
      }
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    } finally {
      setCreating(false);
    }
  }, [title, description, creating]);

  if (created) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-ok/40 bg-ok-soft/20 px-4 py-3">
        <Check className="h-4 w-4 shrink-0 text-ok" strokeWidth={2} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-fg-0">{created.title}</div>
          {created.jira_key && (
            <div className="font-mono text-[11.5px] text-fg-3">{created.jira_key}</div>
          )}
        </div>
        <button
          onClick={() => navigate(`/tasks/${created.id}`)}
          className="inline-flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover"
        >
          Open task <ExternalLink className="h-3 w-3" strokeWidth={2} />
        </button>
        <button onClick={onClose} className="text-fg-3 hover:text-fg-0">
          <X className="h-4 w-4" strokeWidth={1.75} />
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border bg-bg-1">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
          <span className="text-[13px] font-semibold text-fg-0">Create Jira Task from Session</span>
        </div>
        <button onClick={onClose} className="text-fg-3 hover:text-fg-0">
          <X className="h-3.5 w-3.5" strokeWidth={1.75} />
        </button>
      </div>
      <div className="grid grid-cols-1 gap-4 p-4 md:grid-cols-2">
        <div className="space-y-3">
          {/* Type */}
          <div>
            <label className="mb-1.5 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">Type</label>
            <div className="flex gap-1.5">
              {(["Task", "Bug", "Story", "Sub-task"] as const).map((t) => (
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
              ))}
            </div>
          </div>
          {/* Title */}
          <div>
            <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">Title</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded-md border border-border bg-bg-2 px-3 py-2 text-[13px] text-fg-0 focus:border-accent focus:outline-none"
            />
          </div>
          {/* Project */}
          <div>
            <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">Jira project (optional)</label>
            <input
              type="text"
              value={projectKey}
              onChange={(e) => setProjectKey(e.target.value.toUpperCase())}
              placeholder="PROJ"
              className="w-full rounded-md border border-border bg-bg-2 px-3 py-2 font-mono text-[13px] text-fg-0 focus:border-accent focus:outline-none"
            />
          </div>
        </div>
        <div>
          <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-wider text-fg-3">
            Description preview
            <span className="ml-1.5 font-normal normal-case text-fg-3">— from session content</span>
          </label>
          <pre className="max-h-44 overflow-y-auto whitespace-pre-wrap rounded-md bg-bg-2 px-3 py-2.5 font-mono text-[10.5px] leading-relaxed text-fg-1">
            {description.slice(0, 600)}{description.length > 600 ? "\n…" : ""}
          </pre>
          {session.brief_md && (
            <p className="mt-1.5 flex items-center gap-1 text-[11px] text-ok">
              <Check className="h-3 w-3" strokeWidth={2} />
              Session brief included in description
            </p>
          )}
        </div>
      </div>
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
            "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12.5px] font-medium",
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
          {creating ? "Creating…" : `Create ${issueType}`}
        </button>
      </div>
    </div>
  );
}

/* ─── Sessions rail (left sidebar on the detail page) ─── */

function SessionsRail({ activeId }: { activeId: string | null }) {
  const { data, isLoading } = useQuery({
    queryKey: ["collab-sessions"],
    queryFn: () => listCollabSessions({ limit: 30 }),
    refetchInterval: 30_000,
  });
  const list = data ?? [];

  return (
    <aside className="flex w-[260px] shrink-0 flex-col border-r border-border bg-bg-1/50">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="text-[11.5px] font-semibold uppercase tracking-wide text-fg-2">
          Recent sessions
        </span>
        <Link
          to="/collaboration/new"
          className="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-[10.5px] font-medium text-accent-fg hover:bg-accent-hover"
          title="Start a new session"
        >
          <Plus className="h-2.5 w-2.5" strokeWidth={2} />
          New
        </Link>
      </div>
      <nav className="flex-1 overflow-y-auto p-2">
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12 rounded-md" />
            ))}
          </div>
        ) : list.length === 0 ? (
          <p className="px-2 py-3 text-[11.5px] text-fg-3">
            No sessions yet.
          </p>
        ) : (
          <ul className="space-y-0.5">
            {list.map((s) => (
              <SessionRailItem
                key={s.id}
                session={s}
                active={s.id === activeId}
              />
            ))}
          </ul>
        )}
      </nav>
    </aside>
  );
}

const SessionRailItem = memo(function SessionRailItem({
  session,
  active,
}: {
  session: CollabSessionSummary;
  active: boolean;
}) {
  return (
    <li>
      <Link
        to={`/collaboration/sessions/${session.id}`}
        className={cn(
          "block rounded-md px-2.5 py-2 transition-colors",
          active
            ? "bg-accent-soft text-fg-0"
            : "text-fg-1 hover:bg-bg-2 hover:text-fg-0",
        )}
      >
        <div className="flex items-center gap-1.5">
          <StateDot state={session.state} />
          <span
            className="truncate text-[12.5px] font-medium"
            title={session.requirement_text}
          >
            {session.requirement_text}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 pl-3 text-[10.5px] text-fg-3">
          <span>{humanState(session.state)}</span>
          {session.open_doubt_count > 0 && (
            <>
              <span aria-hidden>·</span>
              <span className="inline-flex items-center gap-0.5 text-warn">
                <HelpCircle className="h-2 w-2" strokeWidth={2} />
                {session.open_doubt_count}
              </span>
            </>
          )}
          <span aria-hidden className="ml-auto">
            {formatRelativeDate(session.updated_at)}
          </span>
        </div>
      </Link>
    </li>
  );
});

/* ─── Agent typing indicator ─────────────────────────────── */

function AgentThinking({ agent }: { agent: string }) {
  const label =
    agent === "clarifier"
      ? "Bifrost · Clarifier"
      : agent === "synthesizer"
        ? "Bifrost · Synthesizer"
        : "Bifrost";
  const verb = agent === "synthesizer" ? "writing the brief" : "thinking";
  return (
    <div className="flex items-start gap-3" aria-live="polite" aria-busy="true">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg">
        <Sparkles className="h-3.5 w-3.5 animate-pulse" strokeWidth={1.75} />
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-center gap-2 text-[11px] font-medium">
          <span className="text-fg-2">{label}</span>
        </div>
        <div className="inline-flex items-center gap-2 rounded-full bg-accent-soft px-3 py-1 text-[12px] text-accent">
          <span className="flex gap-0.5">
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent [animation-delay:0ms]" />
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent [animation-delay:150ms]" />
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent [animation-delay:300ms]" />
          </span>
          <span>{verb}…</span>
        </div>
      </div>
    </div>
  );
}

/* ─── Final brief panel ─────────────────────────────────── */

const BriefPanel = memo(function BriefPanel({
  md,
  sessionId,
}: {
  md: string;
  sessionId: string;
}) {
  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(md);
      toast.success("Brief copied to clipboard");
    } catch {
      toast.error("Couldn't copy");
    }
  }, [md]);

  return (
    <section className="mt-4 overflow-hidden rounded-lg border border-accent/40 bg-accent-soft/10">
      <header className="flex items-center gap-2 border-b border-accent/30 bg-accent-soft/20 px-4 py-2">
        <FileText className="h-3.5 w-3.5 text-accent" strokeWidth={1.75} />
        <span className="text-[11.5px] font-semibold uppercase tracking-wide text-accent">
          Final brief
        </span>
        <a
          href={`/api/v1/collab-sessions/${sessionId}/brief.md`}
          className="ml-auto inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          title="Download as Markdown (use ⌘+P → Save as PDF in your browser for a PDF)"
        >
          <FileText className="h-3 w-3" strokeWidth={1.75} />
          Download
        </a>
        <button
          onClick={onCopy}
          className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          title="Copy the brief Markdown to your clipboard"
        >
          <ClipboardCheck className="h-3 w-3" strokeWidth={1.75} />
          Copy
        </button>
      </header>
      <div className="px-5 py-4">
        <Prose>{md}</Prose>
      </div>
    </section>
  );
});

/* ─── Doubts ─────────────────────────────────────────────── */

function DoubtsSection({
  title,
  doubts,
  sessionId,
  onChanged,
  resolved = false,
}: {
  title: string;
  doubts: CollabDoubt[];
  sessionId: string;
  onChanged: () => void;
  resolved?: boolean;
}) {
  return (
    <section>
      <header className="mb-2 flex items-center gap-1.5">
        <HelpCircle className="h-3.5 w-3.5 text-fg-2" strokeWidth={1.75} />
        <h3 className="text-[11.5px] font-semibold uppercase tracking-wide text-fg-2">
          {title}
        </h3>
        <span className="rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[9.5px] text-fg-2">
          {doubts.length}
        </span>
      </header>
      {doubts.length === 0 ? (
        <p className="text-[11.5px] text-fg-3">
          {resolved ? "—" : "No open doubts."}
        </p>
      ) : (
        <ul className="space-y-2">
          {doubts.map((d) => (
            <DoubtCard
              key={d.id}
              doubt={d}
              sessionId={sessionId}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function DoubtCard({
  doubt,
  sessionId,
  onChanged,
}: {
  doubt: CollabDoubt;
  sessionId: string;
  onChanged: () => void;
}) {
  const [resolving, setResolving] = useState<CollabDoubtStatus | null>(null);

  const resolve = async (
    status: Exclude<CollabDoubtStatus, "open">,
    answer?: string,
  ) => {
    setResolving(status);
    try {
      await resolveCollabDoubt(sessionId, doubt.id, {
        status,
        resolved_md: answer,
      });
      onChanged();
    } catch (err) {
      toast.error(`Couldn't resolve: ${describeError(err)}`);
    } finally {
      setResolving(null);
    }
  };

  const isOpen = doubt.status === "open";

  return (
    <li className="rounded-md border border-border bg-bg-0 p-2.5">
      <div className="flex items-start gap-2">
        <CircleDot
          className={cn(
            "mt-0.5 h-3 w-3 shrink-0",
            isOpen ? "text-warn" : "text-fg-3",
          )}
          strokeWidth={1.75}
        />
        <div className="min-w-0 flex-1 space-y-1.5">
          <p className="text-[12.5px] leading-snug text-fg-1">
            {doubt.question}
          </p>
          {doubt.resolved_md && (
            <p className="text-[11.5px] leading-snug text-fg-2">
              <span className="font-semibold text-fg-1">
                {doubt.status === "answered_by_doc" ? "From handover:" : "Resolved:"}
              </span>{" "}
              {doubt.resolved_md}
            </p>
          )}
          {doubt.status === "answered_by_doc" && doubt.source_ref && (
            <DoubtCitationChip sourceRef={doubt.source_ref} />
          )}
          <div className="flex items-center gap-1.5">
            <DoubtStatusPill status={doubt.status} />
            {isOpen && (
              <>
                <button
                  onClick={() => {
                    const a = window.prompt("Answer this doubt:");
                    if (a && a.trim()) resolve("answered_by_human", a.trim());
                  }}
                  disabled={!!resolving}
                  className="text-[10.5px] font-medium text-accent hover:underline disabled:text-fg-3"
                >
                  Answer
                </button>
                <button
                  onClick={() => resolve("unresolved")}
                  disabled={!!resolving}
                  className="text-[10.5px] font-medium text-fg-2 hover:text-fg-0 disabled:text-fg-3"
                >
                  Mark risk
                </button>
                <button
                  onClick={() => resolve("escalated")}
                  disabled={!!resolving}
                  className="text-[10.5px] font-medium text-fg-2 hover:text-fg-0 disabled:text-fg-3"
                >
                  Escalate
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </li>
  );
}

/**
 * Click-through chip rendered next to ``answered_by_doc`` doubts.
 *
 * Source-ref format is ``handover:<scope_kind>/<scope_value>#<doc_id>``
 * (set by the clarifier via ``handover_urn()`` in matching.py). We
 * parse out the scope_value for the visible label and the doc_id for
 * the clickable destination — the rest is metadata for tooltip/debug.
 *
 * Falls back to a plain text chip if the format doesn't parse, so a
 * malformed citation degrades gracefully instead of crashing the
 * doubt panel.
 */
function DoubtCitationChip({ sourceRef }: { sourceRef: string }) {
  const m = sourceRef.match(/^handover:([^/]+)\/([^#]+)#(.+)$/);
  if (!m) {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-1.5 py-0.5 font-mono text-[10px] text-fg-3"
        title={sourceRef}
      >
        <BookOpen className="h-2.5 w-2.5" strokeWidth={1.75} />
        {sourceRef.slice(0, 40)}
      </span>
    );
  }
  const [, scopeKind, scopeValue, docId] = m;
  return (
    <Link
      to={`/collaboration/handovers/${docId}`}
      className="inline-flex items-center gap-1 rounded-md border border-accent/40 bg-accent-soft px-1.5 py-0.5 text-[10.5px] font-medium text-accent hover:bg-accent hover:text-bg-0"
      title={`${scopeKind} handover: ${scopeValue}`}
    >
      <BookOpen className="h-2.5 w-2.5" strokeWidth={1.75} />
      {scopeValue}
    </Link>
  );
}

function DoubtStatusPill({ status }: { status: CollabDoubtStatus }) {
  const cfg = {
    open: { cls: "bg-warn-soft text-warn", label: "Open" },
    answered_by_doc: { cls: "bg-ok-soft text-ok", label: "From docs" },
    answered_by_human: { cls: "bg-accent-soft text-accent", label: "Answered" },
    unresolved: { cls: "bg-bg-3 text-fg-2", label: "Risk" },
    escalated: { cls: "bg-info-soft text-info", label: "Escalated" },
  }[status];
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide",
        cfg.cls,
      )}
    >
      {cfg.label}
    </span>
  );
}

/* ─── Artifacts ──────────────────────────────────────────── */

function ArtifactsSection({
  artifacts,
}: {
  artifacts: CollabSessionDetail["artifacts"];
}) {
  return (
    <section>
      <header className="mb-2 flex items-center gap-1.5">
        <FileText className="h-3.5 w-3.5 text-fg-2" strokeWidth={1.75} />
        <h3 className="text-[11.5px] font-semibold uppercase tracking-wide text-fg-2">
          Artifacts
        </h3>
        <span className="rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[9.5px] text-fg-2">
          {artifacts.length}
        </span>
      </header>
      {artifacts.length === 0 ? (
        <p className="text-[11.5px] text-fg-3">
          The synthesizer hasn't run yet — close the session to produce the brief.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {artifacts.map((a) => (
            <li
              key={a.id}
              className="flex items-center gap-2 rounded-md border border-border bg-bg-0 px-2.5 py-1.5"
            >
              <FileText className="h-3 w-3 shrink-0 text-fg-2" strokeWidth={1.75} />
              <div className="min-w-0 flex-1">
                <div className="text-[12px] font-medium text-fg-1">
                  {artifactLabel(a.kind)}
                </div>
                <div
                  className="truncate font-mono text-[10.5px] text-fg-3"
                  title={a.fs_path}
                >
                  {a.fs_path}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ─── Misc helpers ───────────────────────────────────────── */

function StateBadge({ state }: { state: CollabSessionState }) {
  const cfg = stateConfig(state);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide",
        cfg.pill,
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", cfg.dot)} />
      {cfg.label}
    </span>
  );
}

function StateDot({ state }: { state: CollabSessionState }) {
  return (
    <span
      aria-hidden
      className={cn(
        "h-1.5 w-1.5 shrink-0 rounded-full",
        stateConfig(state).dot,
      )}
    />
  );
}

function stateConfig(state: CollabSessionState) {
  return {
    open: { dot: "bg-info", pill: "bg-info-soft text-info", label: "Open" },
    clarifying: { dot: "bg-warn", pill: "bg-warn-soft text-warn", label: "Discussing" },
    resolved: { dot: "bg-ok", pill: "bg-ok-soft text-ok", label: "Ready to brief" },
    briefed: { dot: "bg-accent", pill: "bg-accent-soft text-accent", label: "Brief ready" },
    closed: { dot: "bg-fg-3", pill: "bg-bg-3 text-fg-2", label: "Closed" },
  }[state];
}

function humanState(state: CollabSessionState): string {
  return stateConfig(state).label;
}

function roleLabel(role: CollabMessage["role"]): string {
  return {
    requester: "You",
    system: "System",
    clarifier_agent: "Bifrost · Clarifier",
    synthesizer_agent: "Bifrost · Synthesizer",
  }[role];
}

function artifactLabel(
  kind: CollabSessionDetail["artifacts"][number]["kind"],
): string {
  return {
    final_brief: "Final brief",
    estimate: "Estimate",
    risk_log: "Risk log",
    draft_ask_message: "Draft ask message",
  }[kind];
}

function bucketCounts(list: CollabSessionSummary[]) {
  const out = { open: 0, clarifying: 0, resolved: 0, briefed: 0, closed: 0 };
  for (const s of list) out[s.state]++;
  return out;
}

function EmptyHero({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-bg-1 px-6 py-12 text-center">
      <Users className="mb-2 h-6 w-6 text-fg-3" strokeWidth={1.75} />
      <p className="text-[14px] font-medium text-fg-1">{title}</p>
      <p className="mt-1 max-w-md text-[12.5px] text-fg-3">{body}</p>
    </div>
  );
}


/* ─── Handover docs list page ─────────────────── */

type HandoverScopeTab = "repos" | "domains" | "teams" | "regions";

/**
 * Pre-flight cost gate. Wraps the user's "Generate" click
 * with two confirmation tiers:
 *
 *   1. **Cheap (estimate ≤ $1)** — fire-and-forget, no friction.
 *   2. **Mid (≤ ceiling)** — single ``confirm()`` showing the
 *      reasoning so the user can bail before burning credits.
 *   3. **Over ceiling** — first confirm to attempt, then if the
 *      backend 409s ``handover_cost_ceiling_exceeded`` we surface a
 *      second confirm spelling out the override and retry with
 *      ``force=true``.
 *
 * Returning ``null`` means the user cancelled at some confirm step;
 * callers treat that as a no-op (no toast, no error).
 */
const _CHEAP_USD_THRESHOLD = 1.0;

type GenerateFn = (force?: boolean) => Promise<{
  doc_id: string;
  fs_path: string;
  title: string;
  used_fallback: boolean;
}>;

async function runWithCostGate(
  scopeLabel: string,
  fetchEstimate: () => Promise<HandoverCostEstimate>,
  doGenerate: GenerateFn,
): Promise<Awaited<ReturnType<GenerateFn>> | null> {
  let estimate: HandoverCostEstimate | null = null;
  try {
    estimate = await fetchEstimate();
  } catch (err) {
    // If the estimate itself fails (e.g. 404 — bad scope id) the
    // generate call will fail the same way; let it surface there.
    // For other errors we just skip the cost gate and proceed.
    console.warn(`cost estimate failed for ${scopeLabel}:`, err);
  }

  if (estimate && estimate.estimated_usd > _CHEAP_USD_THRESHOLD) {
    const msg =
      `Generate handover for ${scopeLabel}?\n\n` +
      `Estimated cost: $${estimate.estimated_usd.toFixed(2)}\n` +
      `${estimate.reasoning}\n\n` +
      (estimate.over_ceiling
        ? `This exceeds the configured ceiling of $${estimate.ceiling_usd.toFixed(2)} — you'll be asked to confirm an override.`
        : "Proceed?");
    if (!window.confirm(msg)) return null;
  }

  try {
    return await doGenerate(false);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const detail = err.detail as
        | { code?: string; message?: string; estimate?: HandoverCostEstimate }
        | undefined;
      const errorBody = (detail as { error?: typeof detail })?.error ?? detail;
      if (errorBody?.code === "handover_cost_ceiling_exceeded") {
        const est = errorBody.estimate ?? estimate;
        const overrideMsg =
          `Cost ceiling exceeded for ${scopeLabel}.\n\n` +
          (est
            ? `Estimated: $${est.estimated_usd.toFixed(2)}\n` +
              `Ceiling: $${est.ceiling_usd.toFixed(2)}\n` +
              `${est.reasoning}\n\n`
            : "") +
          "Override and run anyway?";
        if (!window.confirm(overrideMsg)) return null;
        return await doGenerate(true);
      }
    }
    throw err;
  }
}


export function HandoverDocsListPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();

  // "Show archived" toggle. When on, the list query passes
  // ``include_archived=true`` so soft-archived rows reappear with a
  // Restore button (instead of View). Off by default — archive is a
  // one-click hide-from-view, not a delete.
  const [showArchived, setShowArchived] = useState(false);

  // free-text search across handover docs. The raw input is
  // tracked in ``searchInput``; ``debouncedQ`` lags by 300 ms so we
  // don't fire a new request on every keystroke. When ``debouncedQ``
  // is non-empty the results section renders ABOVE the existing tab
  // strip and the tabs/list still render below (so the user can pivot
  // back to browsing without clearing the box).
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const search = useQuery({
    queryKey: ["handover-docs-search", debouncedQ],
    queryFn: () => searchHandoverDocs(debouncedQ, 20),
    enabled: debouncedQ.length > 0,
    staleTime: 10_000,
  });

  const docs = useQuery({
    queryKey: ["handover-docs", { include_archived: showArchived }],
    queryFn: () =>
      listHandoverDocs({
        limit: 100,
        include_archived: showArchived || undefined,
      }),
    refetchInterval: 30_000,
  });
  const repos = useQuery({ queryKey: ["repos"], queryFn: listRepos });
  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: listDomains,
  });
  // Knowledge overview gives us distinct squads (teams) without
  // needing a new endpoint — every repo carries the squad it's owned
  // by, and we just dedupe.
  const knowledge = useQuery({
    queryKey: ["knowledge-overview"],
    queryFn: getKnowledgeOverview,
  });

  const list = useMemo(() => docs.data ?? [], [docs.data]);
  const repoList = repos.data ?? [];
  const domainList = domains.data ?? [];

  // Map by scope_value → existing doc (per scope_kind) for quick lookup.
  const byRepoName = useMemo(() => {
    const m = new Map<string, HandoverDocSummary>();
    for (const d of list) {
      if (d.scope_kind === "repo") m.set(d.scope_value, d);
    }
    return m;
  }, [list]);

  const byDomainName = useMemo(() => {
    const m = new Map<string, HandoverDocSummary>();
    for (const d of list) {
      if (d.scope_kind === "domain") m.set(d.scope_value, d);
    }
    return m;
  }, [list]);

  const byTeamSquad = useMemo(() => {
    const m = new Map<string, HandoverDocSummary>();
    for (const d of list) {
      if (d.scope_kind === "team") m.set(d.scope_value, d);
    }
    return m;
  }, [list]);

  const byRegion = useMemo(() => {
    const m = new Map<string, HandoverDocSummary>();
    for (const d of list) {
      if (d.scope_kind === "region") m.set(d.scope_value, d);
    }
    return m;
  }, [list]);

  // Distinct squads from registered repos. We keep both the (alpha-
  // sorted) name AND the count of repos per squad — the count is
  // shown on each row so the user knows what "team" actually
  // covers. This is the cheapest path: no new backend endpoint, and
  // it already excludes squads with no registered repos (which would
  // be unrenderable anyway since the generator joins through
  // OrgRepo.registered_repo_id).
  const squadList = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of knowledge.data?.repos ?? []) {
      if (!r.squad) continue;
      counts.set(r.squad, (counts.get(r.squad) ?? 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([squad, repo_count]) => ({ squad, repo_count }));
  }, [knowledge.data]);

  // Distinct regions — same shape + derivation as squads, just keyed
  // off ``region``. Repos with a NULL region are skipped: the catalog
  // hasn't surfaced a region for them yet, so they wouldn't be in
  // scope for any region handover the generator could produce.
  const regionList = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of knowledge.data?.repos ?? []) {
      if (!r.region) continue;
      counts.set(r.region, (counts.get(r.region) ?? 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([region, repo_count]) => ({ region, repo_count }));
  }, [knowledge.data]);

  const [tab, setTab] = useState<HandoverScopeTab>("repos");
  const [generatingKey, setGeneratingKey] = useState<string | null>(null);

  const onGenerateRepo = useCallback(
    async (repoId: string) => {
      const key = `repo:${repoId}`;
      setGeneratingKey(key);
      try {
        const r = await runWithCostGate(
          `repo \`${repoId}\``,
          () => estimateRepoHandoverCost(repoId),
          (force) => generateRepoHandover(repoId, force),
        );
        if (r === null) return; // user cancelled
        if (r.used_fallback) {
          toast.warning(
            "Handover fell back — repo may have no extracted entities yet",
          );
        } else {
          toast.success(`Handover ready: ${r.title}`);
        }
        qc.invalidateQueries({ queryKey: ["handover-docs"] });
        if (r.doc_id && r.doc_id !== "00000000-0000-0000-0000-000000000000") {
          navigate(`/collaboration/handovers/${r.doc_id}`);
        }
      } catch (err) {
        toast.error(`Generation failed: ${describeError(err)}`, {
          action: { label: "Retry", onClick: () => void onGenerateRepo(repoId) },
        });
      } finally {
        setGeneratingKey(null);
      }
    },
    [qc, navigate],
  );

  const onGenerateDomain = useCallback(
    async (domainId: string) => {
      const key = `domain:${domainId}`;
      setGeneratingKey(key);
      try {
        const r = await runWithCostGate(
          `domain \`${domainId}\``,
          () => estimateDomainHandoverCost(domainId),
          (force) => generateDomainHandover(domainId, force),
        );
        if (r === null) return;
        if (r.used_fallback) {
          toast.warning(
            "Domain handover fell back — domain may have no linked repos or extracted entities yet",
          );
        } else {
          toast.success(`Domain handover ready: ${r.title}`);
        }
        qc.invalidateQueries({ queryKey: ["handover-docs"] });
        if (r.doc_id && r.doc_id !== "00000000-0000-0000-0000-000000000000") {
          navigate(`/collaboration/handovers/${r.doc_id}`);
        }
      } catch (err) {
        toast.error(`Generation failed: ${describeError(err)}`, {
          action: { label: "Retry", onClick: () => void onGenerateDomain(domainId) },
        });
      } finally {
        setGeneratingKey(null);
      }
    },
    [qc, navigate],
  );

  const onGenerateTeam = useCallback(
    async (squad: string) => {
      const key = `team:${squad}`;
      setGeneratingKey(key);
      try {
        const r = await runWithCostGate(
          `squad \`${squad}\``,
          () => estimateTeamHandoverCost(squad),
          (force) => generateTeamHandover(squad, force),
        );
        if (r === null) return;
        if (r.used_fallback) {
          toast.warning(
            "Team handover fell back — squad may have no registered repos or extracted entities yet",
          );
        } else {
          toast.success(`Team handover ready: ${r.title}`);
        }
        qc.invalidateQueries({ queryKey: ["handover-docs"] });
        if (r.doc_id && r.doc_id !== "00000000-0000-0000-0000-000000000000") {
          navigate(`/collaboration/handovers/${r.doc_id}`);
        }
      } catch (err) {
        toast.error(`Generation failed: ${describeError(err)}`, {
          action: { label: "Retry", onClick: () => void onGenerateTeam(squad) },
        });
      } finally {
        setGeneratingKey(null);
      }
    },
    [qc, navigate],
  );

  const onGenerateRegion = useCallback(
    async (region: string) => {
      const key = `region:${region}`;
      setGeneratingKey(key);
      try {
        const r = await runWithCostGate(
          `region \`${region}\``,
          () => estimateRegionHandoverCost(region),
          (force) => generateRegionHandover(region, force),
        );
        if (r === null) return;
        if (r.used_fallback) {
          toast.warning(
            "Region handover fell back — region may have no registered repos or extracted entities yet",
          );
        } else {
          toast.success(`Region handover ready: ${r.title}`);
        }
        qc.invalidateQueries({ queryKey: ["handover-docs"] });
        if (r.doc_id && r.doc_id !== "00000000-0000-0000-0000-000000000000") {
          navigate(`/collaboration/handovers/${r.doc_id}`);
        }
      } catch (err) {
        toast.error(`Generation failed: ${describeError(err)}`, {
          action: { label: "Retry", onClick: () => void onGenerateRegion(region) },
        });
      } finally {
        setGeneratingKey(null);
      }
    },
    [qc, navigate],
  );

  const tabs: { id: HandoverScopeTab; label: string; count: number }[] = [
    { id: "repos", label: "Repos", count: repoList.length },
    { id: "domains", label: "Domains", count: domainList.length },
    { id: "teams", label: "Teams", count: squadList.length },
    { id: "regions", label: "Regions", count: regionList.length },
  ];

  // restore an archived handover doc — clears ``archived_at``
  // and refetches the list so the row reverts to its non-archived
  // appearance (or disappears if the toggle has since been turned off).
  const onRestore = useCallback(
    async (docId: string, scopeValue: string) => {
      try {
        await restoreHandoverDoc(docId);
        toast.success(`Restored: ${scopeValue}`);
        qc.invalidateQueries({ queryKey: ["handover-docs"] });
      } catch (err) {
        toast.error(`Restore failed: ${describeError(err)}`);
      }
    },
    [qc],
  );

  return (
    <div className="mx-auto max-w-[1100px] space-y-7 px-8 py-8">
      <header className="flex items-end justify-between gap-4">
        <div>
          <Link
            to="/collaboration"
            className="text-[12px] font-medium text-fg-2 hover:text-fg-0"
          >
            ← Back to collaboration
          </Link>
          <h1 className="mt-2 text-[22px] font-semibold tracking-tight text-fg-0">
            Handover docs
          </h1>
          <p className="mt-1 text-[13.5px] text-fg-2">
            Living documents about what each team owns — reused by the
            collaboration clarifier so it can answer doubts with citations
            instead of always punting back to the human.
          </p>
        </div>
        {/* "Show archived" toggle. Off by default; flipping on
            re-issues the list query with ``include_archived=true`` so
            soft-archived rows reappear (greyed out) with a Restore button.*/}
        <label className="inline-flex shrink-0 cursor-pointer items-center gap-2 rounded-md border border-border bg-bg-1 px-2.5 py-1.5 text-[12px] font-medium text-fg-1 hover:bg-bg-2">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
            className="h-3 w-3 rounded border-border accent-accent"
          />
          <Archive className="h-3 w-3" strokeWidth={1.75} />
          Show archived
        </label>
      </header>

      {/* "Needs your attention" digest — owner inbox (self-fetching).
          Surfaces handovers with stale source data, open flags, active
          clarifier sessions, or recurring doubts. The component renders
          its own muted empty/loading state. */}
      <HandoverOwnerInbox />

      {/* free-text search bar. Debounced 300 ms; results land
          BELOW the input and ABOVE the tab strip when the query is
          non-empty. The tabs/list still render so the user can pivot
          back to browsing without clearing the input. */}
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-3"
          strokeWidth={1.75}
        />
        <input
          type="search"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Search handover docs by name or content…"
          className="w-full rounded-md border border-border bg-bg-1 py-2 pl-9 pr-9 text-[13px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
          aria-label="Search handover docs"
        />
        {searchInput && (
          <button
            type="button"
            onClick={() => setSearchInput("")}
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-fg-3 hover:bg-bg-2 hover:text-fg-0"
            aria-label="Clear search"
          >
            <X className="h-3.5 w-3.5" strokeWidth={1.75} />
          </button>
        )}
      </div>

      {debouncedQ && (
        <SearchResultsSection
          query={debouncedQ}
          isLoading={search.isLoading || search.isFetching}
          isError={search.isError}
          hits={search.data ?? []}
        />
      )}

      {/* Tab strip: Repos | Domains | Teams | Regions */}
      <nav
        className="-mb-2 flex items-center gap-1 border-b border-border"
        aria-label="Handover scope"
      >
        {tabs.map((t) => {
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={cn(
                "relative px-3 py-2 text-[13px] font-medium transition-colors",
                active
                  ? "text-fg-0"
                  : "text-fg-2 hover:text-fg-0",
              )}
              aria-current={active ? "page" : undefined}
            >
              <span className="inline-flex items-center gap-1.5">
                {t.label}
                <span
                  className={cn(
                    "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
                    active
                      ? "bg-accent-soft text-accent"
                      : "bg-bg-3 text-fg-2",
                  )}
                >
                  {t.count}
                </span>
              </span>
              {active && (
                <span
                  aria-hidden
                  className="absolute inset-x-0 -bottom-px h-0.5 bg-accent"
                />
              )}
            </button>
          );
        })}
      </nav>

      {tab === "repos" && (
        <RepoHandoverSection
          isLoading={repos.isLoading}
          repos={repoList}
          byRepoName={byRepoName}
          generatingKey={generatingKey}
          onGenerate={onGenerateRepo}
        />
      )}

      {tab === "domains" && (
        <DomainHandoverSection
          isLoading={domains.isLoading}
          domains={domainList}
          byDomainName={byDomainName}
          generatingKey={generatingKey}
          onGenerate={onGenerateDomain}
        />
      )}

      {tab === "teams" && (
        <TeamHandoverSection
          isLoading={knowledge.isLoading}
          squads={squadList}
          byTeamSquad={byTeamSquad}
          generatingKey={generatingKey}
          onGenerate={onGenerateTeam}
        />
      )}

      {tab === "regions" && (
        <RegionHandoverSection
          isLoading={knowledge.isLoading}
          regions={regionList}
          byRegion={byRegion}
          generatingKey={generatingKey}
          onGenerate={onGenerateRegion}
        />
      )}

      {/* All handover docs (any scope) — kept intact below the tabs. */}
      {list.length > 0 && (
        <section>
          <h2 className="text-[15px] font-semibold text-fg-0">
            All handover docs{" "}
            <span className="ml-1 rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
              {list.length}
            </span>
          </h2>
          <ul className="mt-3 overflow-hidden rounded-lg border border-border bg-bg-1">
            {list.map((d, i) => {
              const isArchived = d.archived_at != null;
              return (
                <li
                  key={d.id}
                  className={cn(
                    "flex items-center gap-3 px-4 py-3",
                    i > 0 && "border-t border-border",
                    // dim archived rows so the "Show archived"
                    // toggle visibly distinguishes them from live docs.
                    isArchived && "opacity-50",
                  )}
                >
                  <FreshnessDot state={d.freshness_state} />
                  <div className="min-w-0 flex-1">
                    <Link
                      to={`/collaboration/handovers/${d.id}`}
                      className="truncate text-[13.5px] font-medium text-fg-0 hover:text-accent"
                    >
                      {d.scope_value}
                    </Link>
                    <div className="mt-0.5 text-[11px] text-fg-3">
                      {d.scope_kind} · {d.freshness_state} ·{" "}
                      {d.last_generated_at
                        ? `generated ${formatRelativeDate(d.last_generated_at)}`
                        : "never generated"}
                      {isArchived && (
                        <>
                          {" · "}
                          <span className="font-medium text-warn">
                            archived {formatRelativeDate(d.archived_at!)}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                  {isArchived ? (
                    <button
                      type="button"
                      onClick={() => onRestore(d.id, d.scope_value)}
                      className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
                      title="Restore this handover doc — clears archived_at, returns it to the live list"
                    >
                      <ArchiveRestore
                        className="h-3 w-3"
                        strokeWidth={1.75}
                      />
                      Restore
                    </button>
                  ) : (
                    <Link
                      to={`/collaboration/handovers/${d.id}`}
                      className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
                    >
                      <BookOpen className="h-3 w-3" strokeWidth={1.75} />
                      Read
                    </Link>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}

/* ─── Search results section ──────────────── */

/**
 * Highlight ``query`` (case-insensitive) inside ``text`` by wrapping
 * each occurrence in a ``<mark>`` element. Splits on the literal
 * string — no regex — so the user can search for chars like ``.``
 * or ``$`` without escape pain.
 */
function highlightMatches(text: string, query: string): ReactNode[] {
  if (!query) return [text];
  const out: ReactNode[] = [];
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  let i = 0;
  let n = 0;
  while (i < text.length) {
    const next = lower.indexOf(q, i);
    if (next < 0) {
      out.push(text.slice(i));
      break;
    }
    if (next > i) out.push(text.slice(i, next));
    out.push(
      <mark
        key={`m-${n++}`}
        className="rounded bg-accent-soft px-0.5 text-fg-0"
      >
        {text.slice(next, next + query.length)}
      </mark>,
    );
    i = next + query.length;
  }
  return out;
}

function SearchResultsSection({
  query,
  isLoading,
  isError,
  hits,
}: {
  query: string;
  isLoading: boolean;
  isError: boolean;
  hits: HandoverSearchHit[];
}) {
  return (
    <section aria-label="Search results">
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-[15px] font-semibold text-fg-0">
          Search results{" "}
          <span className="ml-1 rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
            {hits.length}
          </span>
        </h2>
        <span className="text-[11px] text-fg-3">
          for <span className="font-mono text-fg-1">{query}</span>
        </span>
      </div>
      {isLoading ? (
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-6 text-center text-[12.5px] text-fg-3">
          Searching…
        </div>
      ) : isError ? (
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-6 text-center text-[12.5px] text-warn">
          Search failed — try again.
        </div>
      ) : hits.length === 0 ? (
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-6 text-center text-[12.5px] text-fg-3">
          No matches.
        </div>
      ) : (
        <ul className="overflow-hidden rounded-lg border border-border bg-bg-1">
          {hits.map((h, i) => (
            <li
              key={h.id}
              className={cn(
                "px-4 py-3",
                i > 0 && "border-t border-border",
              )}
            >
              <div className="flex items-start gap-3">
                <FreshnessDot state={h.freshness_state} />
                <div className="min-w-0 flex-1">
                  <Link
                    to={`/collaboration/handovers/${h.id}`}
                    className="block truncate text-[13.5px] font-medium text-fg-0 hover:text-accent"
                  >
                    {highlightMatches(h.scope_value, query)}
                  </Link>
                  <div className="mt-0.5 text-[11px] text-fg-3">
                    {h.scope_kind} · {h.freshness_state} ·{" "}
                    {h.last_generated_at
                      ? `generated ${formatRelativeDate(h.last_generated_at)}`
                      : "never generated"}
                    {" · "}
                    score{" "}
                    <span className="font-mono text-fg-2">
                      {h.score.toFixed(1)}
                    </span>
                  </div>
                  {h.snippet && (
                    <div className="mt-1.5 text-[12px] leading-snug text-fg-2">
                      {highlightMatches(h.snippet, query)}
                    </div>
                  )}
                </div>
                <Link
                  to={`/collaboration/handovers/${h.id}`}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
                >
                  <BookOpen className="h-3 w-3" strokeWidth={1.75} />
                  Read
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ─── Reusable scope-row layout ─────────────────────────── */

function ScopeRow({
  icon,
  label,
  sublabel,
  existing,
  isGenerating,
  busy,
  onGenerate,
  generateLabel = "Generate",
  regenerateTitle = "Regenerate",
  generateTitle = "Generate handover doc",
  rowKey,
  isFirst = false,
}: {
  icon: ReactNode;
  label: string;
  sublabel: string;
  existing: HandoverDocSummary | undefined;
  isGenerating: boolean;
  busy: boolean;
  onGenerate: () => void;
  generateLabel?: string;
  regenerateTitle?: string;
  generateTitle?: string;
  rowKey: string;
  isFirst?: boolean;
}) {
  return (
    <li
      key={rowKey}
      className={cn(
        "flex items-center gap-3 px-4 py-3",
        !isFirst && "border-t border-border",
      )}
    >
      {icon}
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13.5px] font-medium text-fg-0">
          {label}
        </div>
        <div className="mt-0.5 truncate text-[11px] text-fg-3">{sublabel}</div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {existing && (
          <Link
            to={`/collaboration/handovers/${existing.id}`}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
          >
            <BookOpen className="h-3 w-3" strokeWidth={1.75} />
            View
          </Link>
        )}
        <button
          onClick={onGenerate}
          disabled={busy}
          title={
            existing
              ? `${regenerateTitle} from latest extraction`
              : generateTitle
          }
          className={cn(
            "inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-medium",
            isGenerating
              ? "bg-bg-3 text-fg-3"
              : existing
                ? "border border-border bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0"
                : "bg-accent text-accent-fg hover:bg-accent-hover",
          )}
        >
          {isGenerating ? (
            <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
          ) : existing ? (
            <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
          ) : (
            <Zap className="h-3 w-3" strokeWidth={2} />
          )}
          {isGenerating
            ? "Generating…"
            : existing
              ? "Regenerate"
              : generateLabel}
        </button>
      </div>
    </li>
  );
}

/* ─── Repo tab ─────────────────────────────────────────── */

function RepoHandoverSection({
  isLoading,
  repos,
  byRepoName,
  generatingKey,
  onGenerate,
}: {
  isLoading: boolean;
  repos: { id: string; name: string }[];
  byRepoName: Map<string, HandoverDocSummary>;
  generatingKey: string | null;
  onGenerate: (repoId: string) => void;
}) {
  const [showBrowse, setShowBrowse] = useState(false);
  const [filter, setFilter] = useState("");

  const withDoc = useMemo(
    () => repos.filter((r) => byRepoName.has(r.name)),
    [repos, byRepoName],
  );
  const withoutDoc = useMemo(
    () => repos.filter((r) => !byRepoName.has(r.name)),
    [repos, byRepoName],
  );
  const filteredWithout = useMemo(() => {
    if (!filter.trim()) return withoutDoc;
    const q = filter.toLowerCase();
    return withoutDoc.filter((r) => r.name.toLowerCase().includes(q));
  }, [withoutDoc, filter]);

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2, 3].map((i) => <Skeleton key={i} className="h-16 rounded-lg" />)}
      </div>
    );
  }
  if (repos.length === 0) {
    return (
      <EmptyHero
        title="No repositories"
        body="Register a repo on the Overview tab and run an extraction first."
      />
    );
  }

  return (
    <div className="space-y-5">
      {/* Summary strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-3">
          <div className="text-[11.5px] text-fg-2">Generated</div>
          <div className="mt-1 text-[22px] font-semibold tabular text-ok">
            {withDoc.length}
          </div>
        </div>
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-3">
          <div className="text-[11.5px] text-fg-2">Not yet</div>
          <div className="mt-1 text-[22px] font-semibold tabular text-fg-3">
            {withoutDoc.length}
          </div>
        </div>
        <div className="rounded-lg border border-border bg-bg-1 px-4 py-3">
          <div className="text-[11.5px] text-fg-2">Total repos</div>
          <div className="mt-1 text-[22px] font-semibold tabular text-fg-0">
            {repos.length}
          </div>
        </div>
      </div>

      {/* Generated docs — show prominently */}
      {withDoc.length > 0 && (
        <section>
          <h2 className="mb-2 text-[13.5px] font-semibold text-fg-0">
            Generated handovers
            <span className="ml-1.5 rounded-full bg-ok-soft px-1.5 py-0.5 font-mono text-[10px] text-ok">
              {withDoc.length}
            </span>
          </h2>
          <ul className="overflow-hidden rounded-lg border border-border bg-bg-1">
            {withDoc.map((r, i) => {
              const existing = byRepoName.get(r.name)!;
              const key = `repo:${r.id}`;
              const isGenerating = generatingKey === key;
              return (
                <ScopeRow
                  key={r.id}
                  rowKey={r.id}
                  isFirst={i === 0}
                  icon={<GitBranch className="h-3.5 w-3.5 shrink-0 text-fg-2" strokeWidth={1.75} />}
                  label={r.name}
                  sublabel={`Generated ${formatRelativeDate(existing.last_generated_at ?? existing.created_at)} · ${existing.freshness_state}`}
                  existing={existing}
                  isGenerating={isGenerating}
                  busy={isGenerating || !!generatingKey}
                  onGenerate={() => onGenerate(r.id)}
                />
              );
            })}
          </ul>
        </section>
      )}

      {/* Browse / generate for repos without docs */}
      {withoutDoc.length > 0 && (
        <section>
          <button
            type="button"
            onClick={() => setShowBrowse((v) => !v)}
            className="flex w-full items-center justify-between rounded-lg border border-border bg-bg-1 px-4 py-3 text-left hover:bg-bg-2"
          >
            <div>
              <span className="text-[13px] font-semibold text-fg-0">
                Generate for a new repo
              </span>
              <span className="ml-2 text-[12px] text-fg-3">
                {withoutDoc.length} repos without a handover
              </span>
            </div>
            <ChevronRight
              className={cn(
                "h-4 w-4 text-fg-2 transition-transform",
                showBrowse && "rotate-90",
              )}
              strokeWidth={1.75}
            />
          </button>

          {showBrowse && (
            <div className="mt-2 space-y-2">
              <div className="relative">
                <Search
                  className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3"
                  strokeWidth={1.75}
                />
                <input
                  type="text"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="Filter repos…"
                  className="w-full rounded-md border border-border bg-bg-1 py-1.5 pl-8 pr-3 text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
                />
                {filter && (
                  <button
                    type="button"
                    onClick={() => setFilter("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-fg-3 hover:text-fg-0"
                  >
                    <X className="h-3.5 w-3.5" strokeWidth={1.75} />
                  </button>
                )}
              </div>
              {filteredWithout.length === 0 ? (
                <p className="py-3 text-center text-[12.5px] text-fg-3">
                  No repos match "{filter}"
                </p>
              ) : (
                <ul className="max-h-80 overflow-y-auto overflow-hidden rounded-lg border border-border bg-bg-1">
                  {filteredWithout.slice(0, 100).map((r, i) => {
                    const key = `repo:${r.id}`;
                    const isGenerating = generatingKey === key;
                    return (
                      <ScopeRow
                        key={r.id}
                        rowKey={r.id}
                        isFirst={i === 0}
                        icon={<GitBranch className="h-3.5 w-3.5 shrink-0 text-fg-2" strokeWidth={1.75} />}
                        label={r.name}
                        sublabel="No handover yet"
                        existing={undefined}
                        isGenerating={isGenerating}
                        busy={isGenerating || !!generatingKey}
                        onGenerate={() => onGenerate(r.id)}
                      />
                    );
                  })}
                  {filteredWithout.length > 100 && (
                    <li className="border-t border-border px-4 py-2 text-center text-[11.5px] text-fg-3">
                      Showing 100 of {filteredWithout.length} — filter to narrow down
                    </li>
                  )}
                </ul>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

/* ─── Domain tab ───────────────────────────────────────── */

function DomainHandoverSection({
  isLoading,
  domains,
  byDomainName,
  generatingKey,
  onGenerate,
}: {
  isLoading: boolean;
  domains: DomainSummary[];
  byDomainName: Map<string, HandoverDocSummary>;
  generatingKey: string | null;
  onGenerate: (domainId: string) => void;
}) {
  return (
    <section>
      <h2 className="text-[15px] font-semibold text-fg-0">
        By domain{" "}
        <span className="ml-1 rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
          {domains.length}
        </span>
      </h2>
      <p className="mt-1 text-[12px] text-fg-3">
        Multi-repo scope — pools entities across every repo linked via{" "}
        <code className="font-mono text-[11px]">repos.domain_id</code>.
      </p>
      <div className="mt-3 [contain:layout]">
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-16 rounded-lg" />
            ))}
          </div>
        ) : domains.length === 0 ? (
          <EmptyHero
            title="No domains"
            body="Register a domain (Domains tab) and link repos to it before generating a domain handover."
          />
        ) : (
          <ul className="overflow-hidden rounded-lg border border-border bg-bg-1">
            {domains.map((d, i) => {
              const existing = byDomainName.get(d.name);
              const key = `domain:${d.id}`;
              const isGenerating = generatingKey === key;
              return (
                <ScopeRow
                  key={d.id}
                  rowKey={d.id}
                  isFirst={i === 0}
                  icon={
                    <Sparkles
                      className="h-3.5 w-3.5 shrink-0 text-fg-2"
                      strokeWidth={1.75}
                    />
                  }
                  label={d.name}
                  sublabel={
                    existing
                      ? `Generated ${formatRelativeDate(existing.last_generated_at ?? existing.created_at)} · ${existing.freshness_state}`
                      : d.description ?? "No handover yet"
                  }
                  existing={existing}
                  isGenerating={isGenerating}
                  busy={isGenerating || !!generatingKey}
                  onGenerate={() => onGenerate(d.id)}
                  generateTitle="Generate a multi-repo handover for this domain"
                />
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

/* ─── Team tab ─────────────────────────────────────────── */

function TeamHandoverSection({
  isLoading,
  squads,
  byTeamSquad,
  generatingKey,
  onGenerate,
}: {
  isLoading: boolean;
  squads: { squad: string; repo_count: number }[];
  byTeamSquad: Map<string, HandoverDocSummary>;
  generatingKey: string | null;
  onGenerate: (squad: string) => void;
}) {
  return (
    <section>
      <h2 className="text-[15px] font-semibold text-fg-0">
        By team{" "}
        <span className="ml-1 rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
          {squads.length}
        </span>
      </h2>
      <p className="mt-1 text-[12px] text-fg-3">
        Multi-repo scope — pools entities across every registered repo whose{" "}
        <code className="font-mono text-[11px]">ownership manifest</code> squad matches.
        Distinct squads come from the registered-repo set.
      </p>
      <div className="mt-3 [contain:layout]">
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-16 rounded-lg" />
            ))}
          </div>
        ) : squads.length === 0 ? (
          <EmptyHero
            title="No teams"
            body="Register repos and sync their ownership manifests before generating a team handover."
          />
        ) : (
          <ul className="overflow-hidden rounded-lg border border-border bg-bg-1">
            {squads.map((s, i) => {
              const existing = byTeamSquad.get(s.squad);
              const key = `team:${s.squad}`;
              const isGenerating = generatingKey === key;
              return (
                <ScopeRow
                  key={s.squad}
                  rowKey={s.squad}
                  isFirst={i === 0}
                  icon={
                    <Users
                      className="h-3.5 w-3.5 shrink-0 text-fg-2"
                      strokeWidth={1.75}
                    />
                  }
                  label={s.squad}
                  sublabel={
                    existing
                      ? `Generated ${formatRelativeDate(existing.last_generated_at ?? existing.created_at)} · ${existing.freshness_state} · ${s.repo_count} repo${s.repo_count === 1 ? "" : "s"}`
                      : `${s.repo_count} repo${s.repo_count === 1 ? "" : "s"} · No handover yet`
                  }
                  existing={existing}
                  isGenerating={isGenerating}
                  busy={isGenerating || !!generatingKey}
                  onGenerate={() => onGenerate(s.squad)}
                  generateTitle="Generate a multi-repo handover for this squad"
                />
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

/* ─── Region tab ─────────────────────────────────────────── */

function RegionHandoverSection({
  isLoading,
  regions,
  byRegion,
  generatingKey,
  onGenerate,
}: {
  isLoading: boolean;
  regions: { region: string; repo_count: number }[];
  byRegion: Map<string, HandoverDocSummary>;
  generatingKey: string | null;
  onGenerate: (region: string) => void;
}) {
  return (
    <section>
      <h2 className="text-[15px] font-semibold text-fg-0">
        By region{" "}
        <span className="ml-1 rounded-full bg-bg-3 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
          {regions.length}
        </span>
      </h2>
      <p className="mt-1 text-[12px] text-fg-3">
        Multi-repo scope — pools entities across every registered repo whose{" "}
        <code className="font-mono text-[11px]">org_repos.region</code> matches
        (a geographic cut). Repos with no region in the catalog
        are skipped.
      </p>
      <div className="mt-3 [contain:layout]">
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-16 rounded-lg" />
            ))}
          </div>
        ) : regions.length === 0 ? (
          <EmptyHero
            title="No regions"
            body="Register repos and sync the catalog so regions surface from ownership manifests before generating a region handover."
          />
        ) : (
          <ul className="overflow-hidden rounded-lg border border-border bg-bg-1">
            {regions.map((s, i) => {
              const existing = byRegion.get(s.region);
              const key = `region:${s.region}`;
              const isGenerating = generatingKey === key;
              return (
                <ScopeRow
                  key={s.region}
                  rowKey={s.region}
                  isFirst={i === 0}
                  icon={
                    <Globe
                      className="h-3.5 w-3.5 shrink-0 text-fg-2"
                      strokeWidth={1.75}
                    />
                  }
                  label={s.region}
                  sublabel={
                    existing
                      ? `Generated ${formatRelativeDate(existing.last_generated_at ?? existing.created_at)} · ${existing.freshness_state} · ${s.repo_count} repo${s.repo_count === 1 ? "" : "s"}`
                      : `${s.repo_count} repo${s.repo_count === 1 ? "" : "s"} · No handover yet`
                  }
                  existing={existing}
                  isGenerating={isGenerating}
                  busy={isGenerating || !!generatingKey}
                  onGenerate={() => onGenerate(s.region)}
                  generateTitle="Generate a multi-repo handover for this region"
                />
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

function FreshnessDot({ state }: { state: HandoverDocSummary["freshness_state"] }) {
  const cls = {
    fresh: "bg-ok",
    stale: "bg-warn",
    very_stale: "bg-err",
    enriched_by_children: "bg-accent",
    stale_due_to_children: "bg-warn",
  }[state];
  return <span aria-hidden className={cn("h-1.5 w-1.5 shrink-0 rounded-full", cls)} />;
}


/* ─── Onboarding lens transformer ─────────────────────
 *
 * The handover generator emits a fixed section order tuned for hand-off
 * recipients who already know the system. New joiners want a reverse
 * orientation — terminology first, who-owns-what next, then shape, then
 * flows, then the messy bits. This transformer re-shuffles the same
 * Markdown without touching the API.
 *
 * Pure / side-effect-free: same input + same lens → same output. Wrap
 * call sites in `useMemo([body_md, lens])`.
 */

export type HandoverLens = "default" | "onboarding";

// Order joiners want to read sections in. Anything not in this list is
// appended after, except hidden sections (below) and the executive
// summary (which becomes the closing wrap-up).
const ONBOARDING_SECTION_ORDER = [
  "Glossary",
  "Ownership",
  "Architecture",
  "Critical flows",
  "KTLO surface",
];

// Sections that exist for cross-team requesters / ops, not joiners.
// Cleaner to hide than to confuse.
const ONBOARDING_HIDDEN_SECTIONS = new Set([
  "Collaboration playbook",
  "Common questions",
]);

// Always-pinned-at-top preamble, regardless of lens. New joiners need
// to see the repos they're inheriting before anything else.
const PINNED_PREAMBLE_TITLE = "Repos in scope";

interface MarkdownSection {
  title: string; // "Glossary" — h2 text without the leading "## "
  content: string; // full text, INCLUDING the "## Title" line, trailing blank line trimmed
}

/** Split a Markdown body on h2 (`## `) boundaries. Pre-content (before
 * the first h2) is returned separately. h3+ stay nested inside their
 * parent h2 because we never split on more than `^## `.
 */
function splitMarkdownByH2(md: string): {
  preamble: string;
  sections: MarkdownSection[];
} {
  const lines = md.split("\n");
  let preambleEnd = lines.length;
  // Find the first h2.
  for (let i = 0; i < lines.length; i++) {
    if (/^## (?!#)/.test(lines[i])) {
      preambleEnd = i;
      break;
    }
  }
  const preamble = lines.slice(0, preambleEnd).join("\n");
  const sections: MarkdownSection[] = [];
  let cursor = preambleEnd;
  while (cursor < lines.length) {
    const headerLine = lines[cursor];
    const m = headerLine.match(/^## (?!#)(.*)$/);
    if (!m) {
      // Shouldn't happen — splitter always lands on an h2 — but bail
      // safely and treat as no more sections.
      break;
    }
    const title = m[1].trim();
    let next = cursor + 1;
    while (next < lines.length && !/^## (?!#)/.test(lines[next])) {
      next++;
    }
    const content = lines.slice(cursor, next).join("\n").replace(/\n+$/, "");
    sections.push({ title, content });
    cursor = next;
  }
  return { preamble, sections };
}

/**
 * Collapse adjacent duplicate ``## <Title>`` headers down to one.
 *
 * The section-splice bug emitted bodies like:
 *   ## Architecture
 *   ## Architecture
 *
 *   ...content...
 *
 * because ``_splice_section`` inserts the section header AND the LLM
 * also emits one. New generations fix this at write time, but old handover bodies on disk still have the
 * duplicates. Cleaning them up at READ time means the UI renders
 * cleanly for ALL existing docs without a backfill migration.
 *
 * Pure transform — runs in O(n) over body lines.
 */
function _collapseDuplicateH2Headers(md: string): string {
  if (!md) return md;
  const lines = md.split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const m = lines[i].match(/^## (?!#)(.*)$/);
    if (m) {
      const title = m[1].trim().toLowerCase();
      // Skip blank lines, then check whether the next header is a
      // duplicate (case-insensitive). If so, drop it. Repeat for any
      // chain of duplicates.
      out.push(lines[i]);
      i++;
      while (i < lines.length) {
        // Tolerate an optional blank line between the doubled
        // headers (common LLM emission pattern).
        let j = i;
        while (j < lines.length && lines[j].trim() === "") j++;
        if (j >= lines.length) break;
        const nm = lines[j].match(/^## (?!#)(.*)$/);
        if (!nm) break;
        if (nm[1].trim().toLowerCase() !== title) break;
        // Drop the duplicate header (and any blank lines we
        // skipped over leading up to it).
        i = j + 1;
      }
    } else {
      out.push(lines[i]);
      i++;
    }
  }
  return out.join("\n");
}

/** Transform body Markdown for the onboarding lens. Default lens is
 * pass-through. Pure — safe to memoize.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper exported for unit tests; HMR-only, no runtime impact
export function applyHandoverLens(bodyMd: string, lens: HandoverLens): string {
  if (lens === "default") return bodyMd;
  const { preamble, sections } = splitMarkdownByH2(bodyMd);
  if (sections.length === 0) return bodyMd;

  // Bucket sections by role: pinned preamble, ordered, executive summary
  // (closing), and "rest" (everything not otherwise classified — kept
  // in original order so we don't lose un-anticipated sections).
  let pinned: MarkdownSection | null = null;
  let executiveSummary: MarkdownSection | null = null;
  const byTitle = new Map<string, MarkdownSection>();
  const rest: MarkdownSection[] = [];

  for (const s of sections) {
    if (ONBOARDING_HIDDEN_SECTIONS.has(s.title)) continue;
    if (s.title === PINNED_PREAMBLE_TITLE) {
      pinned = s;
      continue;
    }
    if (s.title === "Executive summary") {
      executiveSummary = s;
      continue;
    }
    if (ONBOARDING_SECTION_ORDER.includes(s.title)) {
      byTitle.set(s.title, s);
      continue;
    }
    rest.push(s);
  }

  const orderedHead: MarkdownSection[] = [];
  for (const title of ONBOARDING_SECTION_ORDER) {
    const s = byTitle.get(title);
    if (s) orderedHead.push(s);
  }

  const out: string[] = [];
  if (preamble.trim().length > 0) out.push(preamble.replace(/\n+$/, ""));
  if (pinned) out.push(pinned.content);
  for (const s of orderedHead) out.push(s.content);
  for (const s of rest) out.push(s.content);
  if (executiveSummary) out.push(executiveSummary.content);
  return out.join("\n\n") + "\n";
}


/* ─── Handover doc detail page ────────────────── */

export function HandoverDocDetailPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const doc = useQuery({
    queryKey: ["handover-doc", id],
    queryFn: () => getHandoverDoc(id!),
    enabled: !!id,
  });
  const repos = useQuery({ queryKey: ["repos"], queryFn: listRepos });
  const domains = useQuery({ queryKey: ["domains"], queryFn: listDomains });
  const [refreshing, setRefreshing] = useState(false);
  const [archiving, setArchiving] = useState(false);

  // in-UI Markdown editor state. ``editing`` flips the panel
  // from <Prose> read mode into a textarea; ``draft`` holds the
  // working copy (so we can detect unsaved changes vs the loaded body).
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  // body-history diff modal. ``showHistory`` flips the
  // modal in/out; the modal manages its own snapshot-fetch + selected
  // version state so the parent re-renders aren't expensive.
  const [showHistory, setShowHistory] = useState(false);

  // restore-a-prior-version modal. Reuses the SAME body-history
  // snapshots the diff modal fetches (``getHandoverBodyHistory``); the
  // history query is only enabled while the modal is open so we don't
  // pay for it on every detail view.
  const [showRestore, setShowRestore] = useState(false);
  const restoreHistory = useQuery({
    queryKey: ["handover-body-history", id, 20],
    queryFn: () => getHandoverBodyHistory(id ?? "", 20),
    enabled: showRestore && !!id,
    staleTime: 10_000,
  });

  // per-section regeneration. The user picks one of the 8
  // canonical sections from a dropdown and fires a single Sonnet
  // call that splices a fresh body into just that ``## <Header>``
  // block. ``selectedSection`` defaults to ``executive_summary`` so
  // the button is meaningful before the user touches the picker;
  // ``regeneratingSection`` is the section currently in-flight (or
  // null when idle), used to disable the button + show a spinner.
  const [selectedSection, setSelectedSection] =
    useState<HandoverSectionKey>("executive_summary");
  const [regeneratingSection, setRegeneratingSection] =
    useState<HandoverSectionKey | null>(null);
  // Inline confirm state for the "Regenerate section" button — replaces
  // window.confirm() so the action feels responsive.
  const [confirmRegen, setConfirmRegen] = useState(false);

  // Onboarding lens: purely client-side re-shuffle of the
  // already-fetched Markdown. The query param is the source of truth so
  // a joiner can deep-link to "?lens=onboarding" from a welcome doc.
  const [searchParams, setSearchParams] = useSearchParams();
  const lens: HandoverLens =
    searchParams.get("lens") === "onboarding" ? "onboarding" : "default";
  const setLens = useCallback(
    (next: HandoverLens) => {
      const params = new URLSearchParams(searchParams);
      if (next === "onboarding") {
        params.set("lens", "onboarding");
      } else {
        params.delete("lens");
      }
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const onRefresh = useCallback(async () => {
    if (!doc.data || refreshing) return;
    const data = doc.data;
    setRefreshing(true);
    try {
      let r: { used_fallback: boolean; title: string };

      if (data.scope_kind === "repo") {
        const repo = (repos.data ?? []).find((r) => r.name === data.scope_value);
        if (!repo) {
          toast.error(`Repo "${data.scope_value}" not registered — re-index from the Knowledge page.`);
          return;
        }
        r = await generateRepoHandover(repo.id);
      } else if (data.scope_kind === "team") {
        r = await generateTeamHandover(data.scope_value);
      } else if (data.scope_kind === "region") {
        r = await generateRegionHandover(data.scope_value);
      } else if (data.scope_kind === "domain") {
        const domain = (domains.data ?? []).find(
          (d) => d.name === data.scope_value,
        );
        if (!domain) {
          toast.error(`Domain "${data.scope_value}" not found.`);
          return;
        }
        r = await generateDomainHandover(domain.id);
      } else {
        toast.info("Regeneration for this scope type is not supported here.");
        return;
      }

      if (r.used_fallback) {
        toast.warning(
          "Regenerated but content is minimal — repos in this scope may not have been extracted yet. Run an extraction from the Knowledge page first.",
        );
      } else {
        toast.success("Handover regenerated");
      }
      await qc.invalidateQueries({ queryKey: ["handover-doc", id] });
      qc.invalidateQueries({ queryKey: ["handover-docs"] });
    } catch (err) {
      toast.error(`Regeneration failed: ${describeError(err)}`);
    } finally {
      setRefreshing(false);
    }
  }, [doc.data, repos.data, domains.data, refreshing, qc, id]);

  // soft-archive the current handover. Confirms first because
  // the row disappears from the default list view; we lean on the copy
  // to remind the user that this is reversible (Show archived → Restore).
  const onArchive = useCallback(async () => {
    if (!id || archiving) return;
    if (!doc.data) return;
    const ok = window.confirm(
      `Archive "${doc.data.scope_value}"?\n\n` +
        "This hides it from the default list view. The on-disk Markdown " +
        "is kept and you can restore it at any time from the list page's " +
        '"Show archived" toggle.',
    );
    if (!ok) return;
    setArchiving(true);
    try {
      await archiveHandoverDoc(id);
      toast.success(`Archived: ${doc.data.scope_value}`);
      qc.invalidateQueries({ queryKey: ["handover-docs"] });
      qc.invalidateQueries({ queryKey: ["handover-doc", id] });
      navigate("/collaboration/handovers");
    } catch (err) {
      toast.error(`Archive failed: ${describeError(err)}`);
    } finally {
      setArchiving(false);
    }
  }, [id, archiving, doc.data, qc, navigate]);

  // restore an archived handover from the detail page (when
  // the user lands on it via a deep link while it's archived).
  const onRestoreFromDetail = useCallback(async () => {
    if (!id || archiving) return;
    setArchiving(true);
    try {
      await restoreHandoverDoc(id);
      toast.success("Handover restored");
      qc.invalidateQueries({ queryKey: ["handover-docs"] });
      qc.invalidateQueries({ queryKey: ["handover-doc", id] });
    } catch (err) {
      toast.error(`Restore failed: ${describeError(err)}`);
    } finally {
      setArchiving(false);
    }
  }, [id, archiving, qc]);

  // regenerate just the selected section. Cheap (one
  // Sonnet call, ~$0.05-$0.10) so we skip the cost-estimate fetch
  // the full-regen flow runs first; we still confirm because the
  // splicing overwrites the on-disk body and we want the user to
  // know what's about to change.
  const onRegenerateSection = useCallback(async () => {
    if (!id || regeneratingSection) return;
    if (!doc.data?.body_md) {
      toast.error("No content to regenerate yet — use Refetch to generate the full doc first.");
      return;
    }
    setConfirmRegen(false);
    setRegeneratingSection(selectedSection);
    try {
      await regenerateHandoverSection(id, selectedSection);
      toast.success(`Regenerated: ${sectionLabel}`);
      await qc.invalidateQueries({ queryKey: ["handover-doc", id] });
      qc.invalidateQueries({ queryKey: ["handover-docs"] });
      qc.invalidateQueries({ queryKey: ["handover-audit", id] });
    } catch (err) {
      toast.error(`Section regen failed: ${describeError(err)}`);
    } finally {
      setRegeneratingSection(null);
    }
  }, [id, regeneratingSection, doc.data, selectedSection, qc]);

  // Editor — open: seed the textarea with the loaded body so
  // the unsaved-changes detector keys cleanly off (draft !== loaded).
  const loadedBody = doc.data?.body_md ?? "";
  const dirty = editing && draft !== loadedBody;

  // Re-shuffle handover sections for the active lens. Pure transform —
  // memoize so we don't re-parse the Markdown on every render of the
  // toggle row, freshness banner, doubt panel, etc.
  // Also collapse duplicate ``## H2`` headers (the section-splice bug
  // doubled them in old generations; the per-section regen fix
  // prevents new ones, but we strip on read so old docs render
  // cleanly without a backfill).
  //
  // IMPORTANT: this memo MUST live above the conditional early returns
  // below (loading / error). Hooks rules require a stable call order
  // across renders — putting useMemo *after* an `if (doc.isLoading)
  // return …` triggered "Rendered more hooks during this render than
  // during the previous render" the moment the query resolved, which
  // surfaced as a blank page (React unmounts the component on a hooks
  // violation) — see the ErrorBoundary panel for the original throw.
  const lensBodyMd = useMemo(
    () =>
      applyHandoverLens(
        _collapseDuplicateH2Headers(loadedBody),
        lens,
      ),
    [loadedBody, lens],
  );

  const onEdit = useCallback(() => {
    setDraft(loadedBody);
    setEditing(true);
  }, [loadedBody]);

  const onCancelEdit = useCallback(() => {
    if (
      draft !== loadedBody &&
      !window.confirm("Discard unsaved changes?")
    ) {
      return;
    }
    setEditing(false);
    setDraft("");
  }, [draft, loadedBody]);

  const onSaveEdit = useCallback(async () => {
    if (!id || saving) return;
    const trimmed = draft;
    if (!trimmed.trim()) {
      toast.error("Body can't be empty");
      return;
    }
    setSaving(true);
    try {
      await patchHandoverDocBody(id, trimmed);
      toast.success("Handover saved");
      await qc.invalidateQueries({ queryKey: ["handover-doc", id] });
      setEditing(false);
      setDraft("");
    } catch (err) {
      toast.error(`Save failed: ${describeError(err)}`);
    } finally {
      setSaving(false);
    }
  }, [id, draft, saving, qc]);

  // Keyboard: ⌘+S saves, Esc cancels (with the unsaved-changes confirm
  // delegated to onCancelEdit). Bound at window level while editing so
  // the textarea doesn't have to be focused for ⌘+S to fire (matches
  // most editors' "save anywhere" behaviour).
  useEffect(() => {
    if (!editing) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        onSaveEdit();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        onCancelEdit();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [editing, onSaveEdit, onCancelEdit]);

  if (doc.isLoading) {
    return (
      <div className="mx-auto max-w-[900px] space-y-3 px-8 py-8">
        <Skeleton className="h-12 w-2/3" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (doc.isError || !doc.data) {
    return (
      <div className="mx-auto max-w-[720px] px-8 py-12">
        <EmptyHero
          title="Handover not found"
          body={describeError(doc.error) || "We couldn't load this handover."}
        />
        <div className="mt-4 text-center">
          <Link
            to="/collaboration/handovers"
            className="text-[13px] font-medium text-accent hover:underline"
          >
            ← Back to handovers
          </Link>
        </div>
      </div>
    );
  }

  const data = doc.data;
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(data.body_md ?? "");
      toast.success("Handover Markdown copied");
    } catch {
      toast.error("Couldn't copy");
    }
  };

  return (
    <div className="mx-auto max-w-[900px] space-y-5 px-8 py-8">
      <header>
        <Link
          to="/collaboration/handovers"
          className="text-[12px] font-medium text-fg-2 hover:text-fg-0"
        >
          ← Back to handovers
        </Link>
        <div className="mt-2 flex items-center gap-2">
          <FreshnessDot state={data.freshness_state} />
          <span className="text-[10.5px] font-semibold uppercase tracking-wide text-fg-2">
            {data.scope_kind} · {data.freshness_state}
          </span>
        </div>
        <h1 className="mt-1 text-[22px] font-semibold tracking-tight text-fg-0">
          {data.scope_value}
        </h1>
        <p className="mt-1 text-[12px] text-fg-3">
          {data.last_generated_at
            ? `Last generated ${formatRelativeDate(data.last_generated_at)}`
            : "Never generated"}
          {data.fs_path && (
            <>
              {" · "}
              <span className="font-mono">{data.fs_path}</span>
            </>
          )}
        </p>
      </header>

      {/* Stub / empty-body warning — shown when there's no real content yet.
          This is the most common first-visit state: the handover was
          queued before extraction ran so the generator had no entity
          data to work with and fell back to a minimal stub. */}
      {!editing && (data.body_md == null || data.body_md.trim().length < 100 || data.body_md.includes("(stub)")) && (
        <div className="rounded-xl border border-warn/40 bg-warn-soft/10 px-4 py-3">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" strokeWidth={1.75} />
            <div className="flex-1 min-w-0">
              <p className="text-[13px] font-semibold text-fg-0">
                This handover has no useful content yet
              </p>
              <p className="mt-1 text-[12.5px] text-fg-2">
                It was generated before the repos in this scope were extracted, so there was no entity data to work with.
                {" "}To get a real handover:
              </p>
              <ol className="mt-2 space-y-1 text-[12px] text-fg-2">
                <li>1. Go to <Link to="/knowledge" className="font-medium text-accent hover:underline">Knowledge</Link> → find the relevant repos → run <strong>Re-index</strong></li>
                <li>2. Once indexing completes, come back here and click <strong>Refetch</strong> above</li>
              </ol>
            </div>
            <button
              onClick={onRefresh}
              disabled={refreshing}
              className="shrink-0 inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12px] font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-60"
            >
              {refreshing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2} />
              ) : (
                <RefreshCcw className="h-3.5 w-3.5" strokeWidth={1.75} />
              )}
              {refreshing ? "Regenerating…" : "Refetch now"}
            </button>
          </div>
        </div>
      )}

      {/* While editing we collapse the page chrome to focus mode —
          freshness banner, lens toggle, and candidates panel reappear
          on save/cancel. */}
      {!editing && data.freshness_state !== "fresh" && (
        <FreshnessBanner
          state={data.freshness_state}
          scopeKind={data.scope_kind}
          onRefresh={onRefresh}
          refreshing={refreshing}
        />
      )}

      <div className="flex flex-wrap items-center gap-2">
        {editing ? (
          <>
            <button
              onClick={onSaveEdit}
              disabled={saving || !draft.trim() || !dirty}
              className={cn(
                "inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-medium",
                saving || !draft.trim() || !dirty
                  ? "bg-bg-3 text-fg-3"
                  : "bg-accent text-accent-fg hover:bg-accent-hover",
              )}
              title="Save (⌘+S)"
            >
              {saving ? (
                <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
              ) : (
                <Save className="h-3 w-3" strokeWidth={1.75} />
              )}
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              onClick={onCancelEdit}
              disabled={saving}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
              title="Cancel (Esc)"
            >
              <X className="h-3 w-3" strokeWidth={1.75} />
              Cancel
            </button>
            {dirty && (
              <span
                className="inline-flex items-center gap-1.5 text-[11px] font-medium text-warn"
                role="status"
                aria-live="polite"
              >
                <span
                  className="h-1.5 w-1.5 rounded-full bg-warn"
                  aria-hidden
                />
                Unsaved changes
              </span>
            )}
            <span className="ml-auto text-[10.5px] text-fg-3">
              <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                ⌘
              </kbd>
              {" + "}
              <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                S
              </kbd>{" "}
              to save ·{" "}
              <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                Esc
              </kbd>{" "}
              to cancel
            </span>
          </>
        ) : (
          <>
            <button
              onClick={onCopy}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
              title="Copy the handover Markdown"
            >
              <ClipboardCheck className="h-3 w-3" strokeWidth={1.75} />
              Copy
            </button>
            <button
              onClick={() =>
                qc.invalidateQueries({ queryKey: ["handover-doc", id] })
              }
              className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
            >
              <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
              Refetch
            </button>
            <button
              onClick={onEdit}
              disabled={data.body_md == null}
              className={cn(
                "inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11.5px] font-medium",
                data.body_md == null
                  ? "bg-bg-2 text-fg-3"
                  : "bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
              )}
              title={
                data.body_md == null
                  ? "No body to edit"
                  : "Edit the handover Markdown in-place"
              }
            >
              <Pencil className="h-3 w-3" strokeWidth={1.75} />
              Edit
            </button>
            {/* diff the current body against any prior
                snapshot. Disabled when there's no body to compare.
                The modal owns the snapshot fetch + selection. */}
            <button
              onClick={() => setShowHistory(true)}
              disabled={data.body_md == null}
              className={cn(
                "inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11.5px] font-medium",
                data.body_md == null
                  ? "bg-bg-2 text-fg-3"
                  : "bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
              )}
              title={
                data.body_md == null
                  ? "No body to diff"
                  : "Compare the current body against a past snapshot"
              }
            >
              <GitCompareArrows className="h-3 w-3" strokeWidth={1.75} />
              Show changes
            </button>
            {/* roll the on-disk body back to a prior snapshot.
                Reuses the same body-history snapshots; the modal owns
                the restore call and current body is snapshotted as
                ``pre-restore`` so it's reversible. */}
            <button
              onClick={() => setShowRestore(true)}
              disabled={data.body_md == null}
              className={cn(
                "inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11.5px] font-medium",
                data.body_md == null
                  ? "bg-bg-2 text-fg-3"
                  : "bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
              )}
              title={
                data.body_md == null
                  ? "No body to restore"
                  : "Restore the body to a previous snapshot"
              }
            >
              <RotateCcw className="h-3 w-3" strokeWidth={1.75} />
              Restore a version
            </button>
            {/* archive / restore. Soft-archive is reversible —
                emphasised in the confirm dialog copy. The button flips
                to Restore when this doc is already archived (e.g. a
                deep-link from the list page's "Show archived" toggle). */}
            {data.archived_at == null ? (
              <button
                onClick={onArchive}
                disabled={archiving}
                className={cn(
                  "inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11.5px] font-medium",
                  archiving
                    ? "bg-bg-3 text-fg-3"
                    : "bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                )}
                title="Hide this handover from default list views (reversible)"
              >
                {archiving ? (
                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                ) : (
                  <Archive className="h-3 w-3" strokeWidth={1.75} />
                )}
                {archiving ? "Archiving…" : "Archive"}
              </button>
            ) : (
              <button
                onClick={onRestoreFromDetail}
                disabled={archiving}
                className={cn(
                  "inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11.5px] font-medium",
                  archiving
                    ? "bg-bg-3 text-fg-3"
                    : "bg-warn-soft text-warn hover:bg-warn-soft/70",
                )}
                title="This handover is archived — restore to make it visible again"
              >
                {archiving ? (
                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                ) : (
                  <ArchiveRestore className="h-3 w-3" strokeWidth={1.75} />
                )}
                {archiving ? "Restoring…" : "Restore"}
              </button>
            )}
            <div
              className="ml-auto inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 p-0.5"
              role="group"
              aria-label="Handover lens"
            >
              <button
                type="button"
                onClick={() => setLens("default")}
                aria-pressed={lens === "default"}
                title="Standard hand-off section order (Executive summary first)."
                className={cn(
                  "rounded-[5px] px-2 py-0.5 text-[11.5px] font-medium",
                  lens === "default"
                    ? "bg-accent text-accent-fg"
                    : "border-border bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                )}
              >
                Default
              </button>
              <button
                type="button"
                onClick={() => setLens("onboarding")}
                aria-pressed={lens === "onboarding"}
                title="Re-order sections for new joiners — Glossary, Ownership, Architecture first."
                className={cn(
                  "rounded-[5px] px-2 py-0.5 text-[11.5px] font-medium",
                  lens === "onboarding"
                    ? "bg-accent text-accent-fg"
                    : "border-border bg-bg-1 text-fg-1 hover:bg-bg-2 hover:text-fg-0",
                )}
              >
                Onboarding
              </button>
            </div>
          </>
        )}
      </div>

      {!editing && lens === "onboarding" && (
        <div
          className="rounded-md bg-info-soft px-3 py-2 text-[12px] text-info"
          aria-label="Onboarding lens active"
        >
          Onboarding lens — sections re-ordered for new joiners. Toggle off
          for the standard view.
        </div>
      )}

      {/* per-section regeneration. Picker + button. Only
          shown in read mode (editing=false), and disabled when there's
          no body to splice into. The splicer maps each key to its
          ``## <Header>`` block; if the section isn't present the
          backend returns 400 and we surface that via the toast. */}
      {!editing && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-bg-1 px-3 py-2"
          role="group"
          aria-label="Regenerate handover section"
        >
          <RotateCcw
            className="h-3.5 w-3.5 text-fg-2"
            strokeWidth={1.75}
            aria-hidden
          />
          <label
            htmlFor="handover-section-regen-picker"
            className="text-[11.5px] font-medium text-fg-1"
          >
            Regenerate section:
          </label>
          <select
            id="handover-section-regen-picker"
            value={selectedSection}
            onChange={(e) =>
              setSelectedSection(e.target.value as HandoverSectionKey)
            }
            disabled={regeneratingSection !== null}
            className="rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-0 focus:outline-none focus:border-accent disabled:opacity-60"
          >
            {(
              Object.entries(HANDOVER_SECTION_LABELS) as Array<
                [HandoverSectionKey, string]
              >
            ).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
          {confirmRegen ? (
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-fg-2">Rewrite this section?</span>
              <button
                type="button"
                onClick={onRegenerateSection}
                className="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-[11.5px] font-medium text-accent-fg hover:bg-accent-hover"
              >
                <Sparkles className="h-3 w-3" strokeWidth={1.75} />
                Confirm
              </button>
              <button
                type="button"
                onClick={() => setConfirmRegen(false)}
                className="rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => {
                if (data.body_md == null) {
                  toast.error("No content yet — use Refetch to generate the full doc first.");
                  return;
                }
                setConfirmRegen(true);
              }}
              disabled={regeneratingSection !== null}
              className={cn(
                "inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-medium",
                regeneratingSection !== null
                  ? "bg-bg-3 text-fg-3"
                  : data.body_md == null
                    ? "border border-border bg-bg-1 text-fg-3"
                    : "bg-accent text-accent-fg hover:bg-accent-hover",
              )}
            >
              {regeneratingSection !== null ? (
                <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
              ) : (
                <Sparkles className="h-3 w-3" strokeWidth={1.75} />
              )}
              {regeneratingSection !== null ? "Regenerating…" : "Regenerate"}
            </button>
          )}
          {/* Per-section reader flag — operates on the same selected
              section as the regenerate picker. Lets a reader mark the
              section outdated/incorrect/etc. for owner review. */}
          {id && data.body_md != null && (
            <HandoverSectionFeedback
              handoverId={id}
              sectionKey={selectedSection}
              sectionTitle={HANDOVER_SECTION_LABELS[selectedSection]}
            />
          )}
          <span className="ml-auto text-[10.5px] text-fg-3">
            ~$0.05-$0.10 per section · prior body snapshotted
          </span>
        </div>
      )}

      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          aria-label="Handover Markdown editor"
          className="w-full rounded-lg border border-border bg-bg-1 px-4 py-3 font-mono text-[12.5px] leading-relaxed text-fg-0 placeholder:text-fg-3 focus:outline-none focus:border-accent"
          style={{ height: "70vh" }}
          autoFocus
        />
      ) : data.body_md ? (
        <article className="rounded-lg border border-border bg-bg-1 px-6 py-5">
          <Prose>{lensBodyMd}</Prose>
        </article>
      ) : (
        <EmptyHero
          title="Body unavailable"
          body="The handover row exists but the on-disk Markdown couldn't be read. Re-generate the handover from the list page."
        />
      )}

      {!editing && id && <HandoverReferencingSessions handoverId={id} />}

      {!editing && id && <HandoverFeedbackList handoverId={id} />}

      {!editing && <HandoverActivityPanel handoverId={id ?? ""} />}

      {!editing && <DoubtCandidatesPanel handoverId={id ?? ""} />}

      {showHistory && id && (
        <HandoverDiffModal
          handoverId={id}
          currentBody={data.body_md ?? ""}
          onClose={() => setShowHistory(false)}
        />
      )}

      {showRestore && id && (
        <HandoverRestoreVersion
          handoverId={id}
          snapshots={restoreHistory.data ?? []}
          onClose={() => setShowRestore(false)}
          onRestored={() => {
            qc.invalidateQueries({ queryKey: ["handover-doc", id] });
            qc.invalidateQueries({
              queryKey: ["handover-body-history", id],
            });
          }}
        />
      )}
    </div>
  );
}

/* ─── Activity timeline ──────────────────────────── */

/**
 * Collapsible disclosure listing the lifecycle audit rows for a
 * handover (generate / archive / restore / edit / promote). The
 * backend audit log was added separately; this surfaces it without
 * paying for the fetch on every detail view — the query stays
 * disabled until the user expands the <details>.
 */
function HandoverActivityPanel({ handoverId }: { handoverId: string }) {
  const [open, setOpen] = useState(false);
  const audits = useQuery({
    queryKey: ["handover-audit", handoverId],
    queryFn: () => getHandoverAudits(handoverId, 50),
    enabled: open && !!handoverId,
    staleTime: 30_000,
  });
  const entries = audits.data ?? [];
  // <details> ↔ React state bridge: the native onToggle event flips
  // ``open`` so the query enables, but we also drive ``open`` from
  // state so re-renders stay in sync (e.g. when the parent invalidates
  // the query and we want the panel to refresh).
  return (
    <details
      open={open}
      onToggle={(e) =>
        setOpen((e.currentTarget as HTMLDetailsElement).open)
      }
      className="rounded-lg border border-border bg-bg-1"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-5 py-3 text-[13px] font-semibold text-fg-0 hover:bg-bg-2/50">
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 text-fg-2 transition-transform",
            open && "rotate-90",
          )}
          strokeWidth={1.75}
        />
        <Activity className="h-3.5 w-3.5 text-fg-2" strokeWidth={1.75} />
        Activity
        {open && audits.isSuccess && (
          <span className="rounded-full bg-bg-2 px-1.5 py-0.5 text-[10px] font-semibold text-fg-2">
            {entries.length}
          </span>
        )}
      </summary>
      <div className="border-t border-border px-5 py-3">
        {audits.isLoading && (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-3/4" />
          </div>
        )}
        {audits.isError && (
          <p className="text-[12px] text-warn">
            Couldn't load activity: {describeError(audits.error)}
          </p>
        )}
        {audits.isSuccess && entries.length === 0 && (
          <p className="text-[12px] text-fg-3">No activity yet.</p>
        )}
        {audits.isSuccess && entries.length > 0 && (
          <ul className="space-y-1.5">
            {entries.map((entry) => (
              <HandoverActivityRow key={entry.id} entry={entry} />
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}

const ACTIVITY_EVENT_PILL: Record<
  HandoverAuditEventType,
  { cls: string; label: string }
> = {
  generated: { cls: "bg-accent-soft text-accent", label: "Generated" },
  archived: { cls: "bg-warn-soft text-warn", label: "Archived" },
  restored: { cls: "bg-ok-soft text-ok", label: "Restored" },
  edited: { cls: "bg-info-soft text-info", label: "Edited" },
  promoted: {
    cls: "bg-accent-soft/60 text-accent",
    label: "Promoted",
  },
};

function HandoverActivityRow({ entry }: { entry: HandoverAuditEntry }) {
  const [expanded, setExpanded] = useState(false);
  const pill =
    ACTIVITY_EVENT_PILL[entry.event_type] ?? {
      cls: "bg-bg-2 text-fg-2",
      label: entry.event_type,
    };
  const hasMetadata =
    entry.metadata && Object.keys(entry.metadata).length > 0;
  const actor = entry.actor ?? "system";
  return (
    <li className="rounded-md border border-border bg-bg-0 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
        <span
          className={cn(
            "inline-flex items-center rounded-full px-1.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide",
            pill.cls,
          )}
        >
          {pill.label}
        </span>
        <span className="font-mono text-[11px] text-fg-1">{actor}</span>
        <span className="text-fg-3">
          {formatRelativeDate(entry.created_at)}
        </span>
        {hasMetadata && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto inline-flex items-center gap-1 rounded border border-border bg-bg-1 px-1.5 py-0.5 text-[10.5px] font-medium text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            aria-expanded={expanded}
          >
            <ChevronRight
              className={cn(
                "h-2.5 w-2.5 transition-transform",
                expanded && "rotate-90",
              )}
              strokeWidth={1.75}
            />
            {expanded ? "Hide" : "Details"}
          </button>
        )}
      </div>
      {expanded && hasMetadata && (
        <pre className="mt-2 overflow-x-auto rounded border border-border bg-bg-2 px-2 py-1.5 font-mono text-[10.5px] leading-snug text-fg-1">
          {JSON.stringify(entry.metadata, null, 2)}
        </pre>
      )}
    </li>
  );
}

/* ─── Doubt-candidates panel (loop-back) ────────────────── */

function DoubtCandidatesPanel({ handoverId }: { handoverId: string }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["handover-doubt-candidates", handoverId],
    queryFn: () => getHandoverDoubtCandidates(handoverId, 3),
    enabled: !!handoverId,
    staleTime: 30_000,
  });
  const candidates = data ?? [];
  if (isLoading || candidates.length === 0) return null;
  const onPromoted = () => {
    qc.invalidateQueries({ queryKey: ["handover-doc", handoverId] });
    qc.invalidateQueries({ queryKey: ["handover-doubt-candidates", handoverId] });
  };
  return (
    <section className="rounded-lg border border-warn/40 bg-warn-soft/20 px-5 py-4">
      <header className="flex items-center gap-2">
        <HelpCircle className="h-3.5 w-3.5 text-warn" strokeWidth={1.75} />
        <h3 className="text-[13px] font-semibold text-fg-0">
          Common open questions
        </h3>
        <span className="rounded-full bg-warn-soft px-1.5 py-0.5 text-[10px] font-semibold text-warn">
          {candidates.length}
        </span>
      </header>
      <p className="mt-1 text-[11.5px] text-fg-2">
        Recurring questions across recent collaboration sessions whose
        scope matched this handover. Promote one with a definitive
        answer and the next clarifier will resolve future occurrences
        as <code className="text-fg-1">answered_by_doc</code> with a
        citation back here — no ping needed.
      </p>
      <ul className="mt-3 space-y-2">
        {candidates.map((c, i) => (
          <DoubtCandidateRow
            key={i}
            handoverId={handoverId}
            candidate={c}
            onPromoted={onPromoted}
          />
        ))}
      </ul>
    </section>
  );
}

function DoubtCandidateRow({
  handoverId,
  candidate,
  onPromoted,
}: {
  handoverId: string;
  candidate: DoubtCandidate;
  onPromoted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async () => {
    const trimmed = answer.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      await promoteDoubtCandidate(handoverId, {
        question: candidate.representative_question,
        answer_md: trimmed,
      });
      toast.success("Answer added to handover — future doubts will resolve from doc");
      setAnswer("");
      setOpen(false);
      onPromoted();
    } catch (err) {
      toast.error(`Promote failed: ${describeError(err)}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <li className="rounded-md border border-border bg-bg-0 p-3">
      <p className="text-[12.5px] leading-snug text-fg-1">
        {candidate.representative_question}
      </p>
      <div className="mt-1.5 flex items-center gap-2 text-[10.5px] text-fg-3">
        <span className="inline-flex items-center gap-1 rounded-full bg-warn-soft px-1.5 py-0.5 font-mono font-semibold text-warn">
          ×{candidate.occurrence_count}
        </span>
        <span>
          {candidate.occurrence_count === 1 ? "1 session" : `${candidate.occurrence_count} sessions`}
        </span>
        <span aria-hidden>·</span>
        <span>last seen {formatRelativeDate(candidate.last_seen_at)}</span>
        <button
          onClick={() => setOpen((o) => !o)}
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-accent/40 bg-accent-soft px-1.5 py-0.5 text-[10.5px] font-medium text-accent hover:bg-accent hover:text-bg-0"
        >
          <BookOpen className="h-2.5 w-2.5" strokeWidth={1.75} />
          {open ? "Cancel" : "Add answer to handover"}
        </button>
      </div>
      {open && (
        <div className="mt-3 space-y-2">
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                onSubmit();
              }
            }}
            placeholder={`The team's answer to "${candidate.representative_question.slice(0, 60)}…"\n\nMarkdown is fine. Be specific — this becomes the citation future clarifiers serve back.`}
            className="min-h-[100px] w-full resize-none rounded-md border border-border bg-bg-1 px-3 py-2 text-[12.5px] text-fg-0 placeholder:text-fg-3 focus:outline-none focus:border-accent"
            autoFocus
          />
          <div className="flex items-center justify-between">
            <span className="text-[10.5px] text-fg-3">
              <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                ⌘
              </kbd>{" "}
              +{" "}
              <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
                Enter
              </kbd>{" "}
              to submit
            </span>
            <button
              onClick={onSubmit}
              disabled={!answer.trim() || submitting}
              className={cn(
                "inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-medium",
                answer.trim() && !submitting
                  ? "bg-accent text-accent-fg hover:bg-accent-hover"
                  : "bg-bg-3 text-fg-3",
              )}
            >
              {submitting ? (
                <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
              ) : (
                <ClipboardCheck className="h-3 w-3" strokeWidth={1.75} />
              )}
              {submitting ? "Adding…" : "Promote"}
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

/* ─── Freshness banner — surfaces auto-staleness ──────── */

function FreshnessBanner({
  state,
  scopeKind,
  onRefresh,
  refreshing,
}: {
  state: HandoverFreshnessState;
  scopeKind: HandoverScopeKindLocal;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  // Tone scales with severity: stale + child-derived states warn-soft;
  // very_stale escalates to err-soft. enriched_by_children is informational
  // (children added; root narrative unchanged) so we keep it neutral.
  const tone =
    state === "very_stale"
      ? { wrap: "bg-err-soft text-err", icon: "text-err" }
      : state === "enriched_by_children"
        ? { wrap: "bg-info-soft text-info", icon: "text-info" }
        : { wrap: "bg-warn-soft text-warn", icon: "text-warn" };

  const stateLabel =
    state === "very_stale"
      ? "very stale"
      : state === "enriched_by_children"
        ? "enriched by children"
        : state === "stale_due_to_children"
          ? "stale due to children"
          : "stale";

  // Repo-scope can dispatch to the regenerate endpoint directly; everything
  // else needs the workstream-B aggregator which doesn't ship here. We still
  // render the banner + a fallback button so the user knows what to do.
  const canRefreshHere = scopeKind === "repo";

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex items-start gap-3 rounded-lg px-4 py-3",
        tone.wrap,
      )}
    >
      <AlertTriangle
        className={cn("mt-0.5 h-4 w-4 shrink-0", tone.icon)}
        strokeWidth={1.75}
        aria-hidden
      />
      <div className="min-w-0 flex-1 text-[12.5px] leading-snug">
        <p className="font-semibold">
          This handover is {stateLabel}.
        </p>
        <p className="mt-0.5 opacity-90">
          Underlying source data has changed since it was last generated.
          {!canRefreshHere && " Refresh from the list page."}
        </p>
      </div>
      {canRefreshHere && (
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-md px-2.5 py-1 text-[11.5px] font-medium transition-colors",
            refreshing
              ? "bg-bg-3 text-fg-3"
              : "bg-bg-0/40 hover:bg-bg-0/70",
          )}
        >
          {refreshing ? (
            <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
          ) : (
            <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
          )}
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      )}
    </div>
  );
}

/* ─── Body-history diff modal ─────────────────────────
 *
 * Lets the operator pick any prior snapshot (regen / edit / promote
 * boundary) and see a unified line-by-line diff against the current
 * body. The snapshot list is fetched lazily — opening the modal is
 * the only trigger for the network call so closed-by-default detail
 * pages don't pay the cost.
 *
 * Diff algorithm: classic LCS-by-line, walked back into a unified
 * sequence of {type: 'eq' | 'add' | 'del', text}. For typical
 * handover bodies (≤ 600 lines) the O(n*m) cost is negligible and
 * we avoid the bundle weight of jsdiff. The naive O(n*m) memory
 * usage is also fine at this scale; if a future handover blows
 * past a few thousand lines we can swap in Hunt-Szymanski without
 * touching the call site.
 */

const HISTORY_EVENT_PILL: Record<
  HandoverBodyHistoryEventType,
  { cls: string; label: string }
> = {
  "pre-regen": { cls: "bg-accent-soft text-accent", label: "Before regen" },
  "pre-edit": { cls: "bg-info-soft text-info", label: "Before edit" },
  "pre-promote": {
    cls: "bg-warn-soft text-warn",
    label: "Before promote",
  },
};

type DiffLine =
  | { type: "eq"; text: string }
  | { type: "add"; text: string }
  | { type: "del"; text: string };

/**
 * Compute a unified line-diff between ``oldText`` and ``newText``.
 * Pure-function, no external libs. Walks an LCS table back to
 * produce a stable interleaving of equal / removed / added lines.
 *
 * Exported (under an underscore-prefixed alias) so the unit-test
 * suite can exercise the algorithm without rendering the modal.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper exported for unit tests; HMR-only, no runtime impact
export function _diffHandoverBodyLines(
  oldText: string, newText: string,
): DiffLine[] {
  return diffLines(oldText, newText);
}
function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const n = a.length;
  const m = b.length;
  // LCS length table. ``dp[i][j]`` = length of the longest common
  // subsequence of a[i:] and b[j:]. Built bottom-up so the walk-back
  // can read it left-to-right as a "should I keep, drop, or add?"
  // decision per cell.
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }
  const result: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      result.push({ type: "eq", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ type: "del", text: a[i] });
      i++;
    } else {
      result.push({ type: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) {
    result.push({ type: "del", text: a[i] });
    i++;
  }
  while (j < m) {
    result.push({ type: "add", text: b[j] });
    j++;
  }
  return result;
}

function HandoverDiffModal({
  handoverId,
  currentBody,
  onClose,
}: {
  handoverId: string;
  currentBody: string;
  onClose: () => void;
}) {
  const history = useQuery({
    queryKey: ["handover-body-history", handoverId],
    queryFn: () => getHandoverBodyHistory(handoverId, 20),
    enabled: !!handoverId,
    staleTime: 30_000,
  });

  const entries: HandoverBodyHistoryEntry[] = history.data ?? [];
  // Default to the most-recent snapshot so opening the modal lands
  // on the most useful diff (latest revision boundary). When the
  // history is empty the dropdown shows the empty-state hint.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const effectiveId =
    selectedId ?? (entries.length > 0 ? entries[0].id : null);
  const selected =
    entries.find((e) => e.id === effectiveId) ?? null;

  // ESC dismisses. Bound at window level so the dropdown / scroll
  // pane don't have to be focused for the key to fire.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // Memo the diff so re-renders for unrelated state (mouse hover
  // etc.) don't re-walk the LCS table on every paint.
  const lines = useMemo(() => {
    if (!selected) return [];
    return diffLines(selected.body_md, currentBody);
  }, [selected, currentBody]);

  const stats = useMemo(() => {
    let added = 0;
    let removed = 0;
    for (const ln of lines) {
      if (ln.type === "add") added++;
      else if (ln.type === "del") removed++;
    }
    return { added, removed };
  }, [lines]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-center bg-bg-0/80 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="Handover body history diff"
      onClick={onClose}
    >
      <div
        className="my-auto flex h-[min(85vh,860px)] w-full max-w-[1100px] flex-col rounded-lg border border-border bg-bg-1 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header — title + version selector + dismiss. */}
        <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-3">
          <History className="h-4 w-4 text-fg-2" strokeWidth={1.75} />
          <h2 className="text-[14px] font-semibold text-fg-0">
            Body history
          </h2>
          {history.isLoading && (
            <Loader2
              className="h-3 w-3 animate-spin text-fg-2"
              strokeWidth={2}
            />
          )}
          {history.isSuccess && entries.length > 0 && (
            <select
              value={effectiveId ?? ""}
              onChange={(e) => setSelectedId(e.target.value)}
              className="ml-2 max-w-full rounded-md border border-border bg-bg-0 px-2 py-1 text-[12px] text-fg-1 focus:border-accent focus:outline-none"
              aria-label="Pick a past snapshot to diff against"
            >
              {entries.map((entry) => {
                const pill = HISTORY_EVENT_PILL[entry.event_type] ?? {
                  label: entry.event_type,
                  cls: "",
                };
                return (
                  <option key={entry.id} value={entry.id}>
                    {pill.label} · {formatRelativeDate(entry.created_at)}
                  </option>
                );
              })}
            </select>
          )}
          {selected && (
            <span className="text-[11px] text-fg-3">
              <span className="font-mono text-ok">+{stats.added}</span>
              {" / "}
              <span className="font-mono text-warn">−{stats.removed}</span>
            </span>
          )}
          <button
            type="button"
            onClick={onClose}
            className="ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md border border-border bg-bg-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Close (Esc)"
            aria-label="Close diff viewer"
          >
            <X className="h-3.5 w-3.5" strokeWidth={1.75} />
          </button>
        </div>

        {/* Body — diff pane. */}
        <div className="flex-1 overflow-auto">
          {history.isLoading && (
            <div className="space-y-2 p-5">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          )}
          {history.isError && (
            <p className="p-5 text-[12px] text-warn">
              Couldn't load history: {describeError(history.error)}
            </p>
          )}
          {history.isSuccess && entries.length === 0 && (
            <div className="p-8 text-center text-[12px] text-fg-3">
              No prior versions yet — versions are captured on the first
              edit or regenerate. Nothing has changed this body since it
              was generated, so there is nothing to compare against.
            </div>
          )}
          {history.isSuccess && entries.length > 0 && selected && (
            <HandoverDiffPane lines={lines} />
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Renders the unified diff as a monospace block. Each line is
 * one row; ``+`` / ``−`` markers are colour-coded so glance-scan
 * is fast. Long lines wrap rather than scroll horizontally — the
 * pane already scrolls vertically for the whole modal, and double
 * scrollbars are obnoxious.
 */
function HandoverDiffPane({ lines }: { lines: DiffLine[] }) {
  if (lines.length === 0) {
    return (
      <p className="p-8 text-center text-[12px] text-fg-3">
        Bodies are identical.
      </p>
    );
  }
  return (
    <pre className="m-0 whitespace-pre-wrap break-words p-0 font-mono text-[11.5px] leading-relaxed">
      {lines.map((line, idx) => {
        const cls =
          line.type === "add"
            ? "bg-ok-soft/30 text-ok"
            : line.type === "del"
            ? "bg-warn-soft/30 text-warn"
            : "text-fg-2";
        const marker =
          line.type === "add" ? "+" : line.type === "del" ? "−" : " ";
        return (
          <div
            key={idx}
            className={cn(
              "flex gap-2 px-5 py-0.5",
              line.type === "add" || line.type === "del"
                ? "font-medium"
                : "",
              cls,
            )}
          >
            <span
              className="select-none text-fg-3"
              aria-hidden
            >
              {marker}
            </span>
            <span className="flex-1">{line.text || " "}</span>
          </div>
        );
      })}
    </pre>
  );
}

// Local aliases — keeps the banner's signature readable without re-importing
// the union types from api.ts at every call site.
type HandoverFreshnessState = HandoverDocSummary["freshness_state"];
type HandoverScopeKindLocal = HandoverDocSummary["scope_kind"];

