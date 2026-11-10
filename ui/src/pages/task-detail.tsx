import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { useState, useEffect } from "react";
import {
  ArrowLeft,
  ClipboardList,
  AlertTriangle,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  Play,
  Ban,
  MessageSquare,
  ExternalLink,
  MessageCircleQuestion,
  ChevronRight,
  Sparkles,
  Shield,
  ShieldAlert,
  ListChecks,
  FileCode,
  GitBranch,
  Zap,
  RotateCcw,
  ClipboardCheck,
  MinusCircle,
  Check,
  GitPullRequest,
  Bug,
  Eye,
  Link2,
  RefreshCw,
  FileText,
  Tag,
  Archive,
  ArchiveRestore,
} from "lucide-react";
import { cn, describeError } from "@/lib/utils";
import { Prose } from "@/components/prose";
import { JiraAuditPanel } from "@/components/jira-audit-panel";
import {
  advanceTask,
  cancelTask,
  getTask,
  getTaskConversations,
  createConversation,
  linkConversationToTask,
  runDiscussion,
  runPlan,
  listPhases,
  listArtifacts,
  listAssumptions,
  listClarifications,
  listPlans,
  listVerifyItems,
  answerClarification,
  approveArtifact,
  executeTask,
  retryPlan,
  getExecutionStatus,
  runVerification,
  overrideVerifyItem,
  createPR,
  debugPlan,
  enrichTaskFromJira,
  archiveTask,
  unarchiveTask,
  getAssumptionHistory,
  type TaskChatLink,
  type Clarification,
  type Artifact,
  type Assumption,
  type PlanRow,
  type VerifyItem,
  type PRResult,
  type DebugReport,
  type JiraEnrichment,
  type AssumptionHistory,
  type AssumptionVersion,
} from "@/lib/api";

/**
 * TaskDetail — single-task view. Layer 1 ships a scaffold:
 *   • Intent card
 *   • Phase timeline (empty until phases are created in Layer 3)
 *   • Clarification Queue (wired in Layer 3)
 *   • Events log (live)
 *   • Manual "advance state" controls for now (will be replaced by
 *     per-step approve buttons in later layers)
 */
export function TaskDetailPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const task = useQuery({
    queryKey: ["task", id],
    queryFn: () => getTask(id!),
    enabled: !!id,
    refetchInterval: 5_000,
  });
  const chats = useQuery({
    queryKey: ["task-conversations", id],
    queryFn: () => getTaskConversations(id!),
    enabled: !!id,
    staleTime: 15_000,
  });
  const phases = useQuery({
    queryKey: ["task-phases", id],
    queryFn: () => listPhases(id!),
    enabled: !!id,
    refetchInterval: 10_000,
  });
  const contexts = useQuery({
    queryKey: ["task-contexts", id],
    queryFn: () => listArtifacts(id!, { kind: "context", latest_only: true }),
    enabled: !!id,
    refetchInterval: 10_000,
  });
  const clarifications = useQuery({
    queryKey: ["task-clarifications", id],
    queryFn: () => listClarifications(id!, "open"),
    enabled: !!id,
    refetchInterval: 5_000,
  });
  const plans = useQuery({
    queryKey: ["task-plans", id],
    queryFn: () => listPlans(id!),
    enabled: !!id,
    refetchInterval: 10_000,
  });
  const verifyItems = useQuery({
    queryKey: ["task-verify-items", id],
    queryFn: () => listVerifyItems(id!),
    enabled: !!id,
    refetchInterval: 15_000,
  });
  const planArtifacts = useQuery({
    queryKey: ["task-plan-artifacts", id],
    queryFn: () => listArtifacts(id!, { kind: "plan", latest_only: true }),
    enabled: !!id,
    refetchInterval: 10_000,
  });
  const executionStatus = useQuery({
    queryKey: ["task-execution-status", id],
    queryFn: () => getExecutionStatus(id!),
    enabled: !!id,
    refetchInterval: 3_000,
  });
  const [discussionRunning, setDiscussionRunning] = useState(false);
  const [discussionMessage, setDiscussionMessage] = useState("");
  const [planRunning, setPlanRunning] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [targetRepoPath, setTargetRepoPath] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [verifySummary, setVerifySummary] = useState<string | null>(null);
  const [prBase, setPrBase] = useState("main");
  const [prDraft, setPrDraft] = useState(false);
  const [prUseGh, setPrUseGh] = useState(true);
  const [prRunning, setPrRunning] = useState(false);
  const [prPreview, setPrPreview] = useState<PRResult | null>(null);
  const [debugReports, setDebugReports] = useState<Record<string, DebugReport>>(
    {},
  );
  const [debugRunningFor, setDebugRunningFor] = useState<string | null>(null);
  const [enriching, setEnriching] = useState(false);

  // Reset all transient action state when navigating between tasks so stale
  // loading indicators from task A don't bleed into the view of task B.
  useEffect(() => {
    setDiscussionRunning(false);
    setDiscussionMessage("");
    setPlanRunning(false);
    setExecuting(false);
    setTargetRepoPath("");
    setVerifying(false);
    setVerifySummary(null);
    setPrBase("main");
    setPrDraft(false);
    setPrUseGh(true);
    setPrRunning(false);
    setPrPreview(null);
    setDebugReports({});
    setDebugRunningFor(null);
    setEnriching(false);
  }, [id]);

  const runDiscussionNow = async () => {
    if (!id || discussionRunning) return;
    setDiscussionRunning(true);
    try {
      const result = await runDiscussion(id, {
        user_message: discussionMessage || null,
      });
      setDiscussionMessage("");
      toast.success(
        `v${result.artifact_version}: +${result.assumptions_written}A ` +
          `${result.clarifications_opened}? · $${result.cost_usd.toFixed(3)}`,
      );
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-contexts", id] });
      qc.invalidateQueries({ queryKey: ["task-phases", id] });
      qc.invalidateQueries({ queryKey: ["task-clarifications", id] });
      qc.invalidateQueries({ queryKey: ["task-plans", id] });
      qc.invalidateQueries({ queryKey: ["task-plan-artifacts", id] });
    } catch (err) {
      toast.error(
        `Discussion failed: ${describeError(err)}`,
      );
    } finally {
      setDiscussionRunning(false);
    }
  };

  const runPlanNow = async () => {
    if (!id || planRunning) return;
    setPlanRunning(true);
    try {
      const result = await runPlan(id, {});
      toast.success(
        `plan v${result.artifact_version}: ${result.tasks_written} tasks · ` +
          `${result.ready_to_execute ? "ready" : "needs more"} · $${result.cost_usd.toFixed(3)}`,
      );
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-plans", id] });
      qc.invalidateQueries({ queryKey: ["task-plan-artifacts", id] });
      qc.invalidateQueries({ queryKey: ["task-verify-items", id] });
      qc.invalidateQueries({ queryKey: ["task-phases", id] });
    } catch (err) {
      toast.error(
        `Plan failed: ${describeError(err)}`,
      );
    } finally {
      setPlanRunning(false);
    }
  };

  const approveArtifactNow = async (artifactId: string) => {
    if (!id) return;
    try {
      const r = await approveArtifact(id, artifactId);
      if (r.already_approved) {
        toast.info("Already approved");
      } else {
        toast.success(`${r.kind} approved`);
      }
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-contexts", id] });
      qc.invalidateQueries({ queryKey: ["task-plan-artifacts", id] });
      qc.invalidateQueries({ queryKey: ["task-plans", id] });
    } catch (err) {
      toast.error(
        `Approval failed: ${describeError(err)}`,
      );
    }
  };

  const executeTaskNow = async () => {
    if (!id || executing) return;
    const path = targetRepoPath.trim();
    if (!path) {
      toast.error("Set a target repo path first");
      return;
    }
    setExecuting(true);
    try {
      const r = await executeTask(id, { target_repo_path: path });
      toast.success(
        `Execute: ${r.plans_completed}/${r.plans_executed} completed ` +
          (r.plans_failed ? `· ${r.plans_failed} failed ` : "") +
          `· $${r.total_cost_usd.toFixed(3)}`,
      );
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-plans", id] });
      qc.invalidateQueries({ queryKey: ["task-execution-status", id] });
    } catch (err) {
      toast.error(
        `Execute failed: ${describeError(err)}`,
      );
    } finally {
      setExecuting(false);
    }
  };

  const retryPlanNow = async (planId: string) => {
    if (!id) return;
    try {
      const r = await retryPlan(id, planId);
      toast.success(
        `Plan ${r.plan_key}: ${r.status}${r.git_sha ? ` · ${r.git_sha}` : ""}`,
      );
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-plans", id] });
      qc.invalidateQueries({ queryKey: ["task-execution-status", id] });
    } catch (err) {
      toast.error(
        `Retry failed: ${describeError(err)}`,
      );
    }
  };

  const runVerifyNow = async () => {
    if (!id || verifying) return;
    setVerifying(true);
    try {
      const r = await runVerification(id);
      setVerifySummary(r.overall_assessment);
      toast.success(
        `Verify: ${r.passed}p / ${r.failed}f / ${r.skipped}s · ` +
          `$${r.cost_usd.toFixed(3)} · ${r.final_task_state}`,
      );
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-verify-items", id] });
    } catch (err) {
      toast.error(
        `Verify failed: ${describeError(err)}`,
      );
    } finally {
      setVerifying(false);
    }
  };

  const overrideVerifyNow = async (
    itemId: string, verdict: "pass" | "fail" | "skipped",
  ) => {
    if (!id) return;
    try {
      const r = await overrideVerifyItem(id, itemId, verdict);
      toast.success(`Item marked ${r.verdict} · task: ${r.task_state}`);
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["task-verify-items", id] });
    } catch (err) {
      toast.error(
        `Override failed: ${describeError(err)}`,
      );
    }
  };

  const openPrNow = async (dryRun: boolean) => {
    if (!id || prRunning) return;
    setPrRunning(true);
    try {
      const r = await createPR(id, {
        base_branch: prBase,
        draft: prDraft,
        use_gh: prUseGh,
        dry_run: dryRun,
      });
      if (dryRun) {
        setPrPreview(r);
        toast.info("Preview ready — review below");
      } else {
        setPrPreview(null);
        toast.success(
          r.pr_url
            ? `PR opened: ${r.pr_url}`
            : r.pushed
              ? `Pushed ${r.branch}${r.notes.length ? " · " + r.notes[0] : ""}`
              : "Done",
        );
      }
      qc.invalidateQueries({ queryKey: ["task", id] });
    } catch (err) {
      toast.error(
        `PR failed: ${describeError(err)}`,
      );
    } finally {
      setPrRunning(false);
    }
  };

  const runDebugNow = async (planId: string) => {
    if (!id) return;
    setDebugRunningFor(planId);
    try {
      const report = await debugPlan(id, planId);
      setDebugReports((prev) => ({ ...prev, [planId]: report }));
      toast.success(
        `Diagnosed ${report.plan_key}: ${report.retry_safe ? "retry likely" : "needs human"}`,
      );
    } catch (err) {
      toast.error(
        `Debug failed: ${describeError(err)}`,
      );
    } finally {
      setDebugRunningFor(null);
    }
  };

  const toggleArchive = async () => {
    if (!id || !task.data) return;
    try {
      if (task.data.archived_at) {
        await unarchiveTask(id);
        toast.success("Restored to live");
      } else {
        await archiveTask(id);
        toast.success("Archived");
      }
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    } catch (err) {
      toast.error(
        `Failed: ${describeError(err)}`,
      );
    }
  };

  const reEnrichNow = async () => {
    if (!id || enriching) return;
    setEnriching(true);
    try {
      const r = await enrichTaskFromJira(id, true);
      toast.success(r.note);
      qc.invalidateQueries({ queryKey: ["task", id] });
    } catch (err) {
      toast.error(
        `Enrichment failed: ${describeError(err)}`,
      );
    } finally {
      setEnriching(false);
    }
  };

  const spawnSideChat = async () => {
    if (!id || !task.data) return;
    try {
      const { id: convId } = await createConversation({});
      await linkConversationToTask(id, {
        conversation_id: convId,
        link_type: "spawned_chat",
        note: "Side chat from task detail",
      });
      toast.success("Side chat created");
      navigate(`/?conv=${convId}`);
    } catch (err) {
      toast.error(
        `Failed: ${describeError(err)}`,
      );
    }
  };

  const onAdvance = async (toState: string) => {
    if (!id) return;
    try {
      await advanceTask(id, toState);
      toast.success(`Moved to ${toState}`);
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    }
  };

  const onCancel = async () => {
    if (!id) return;
    if (!confirm("Cancel this task?")) return;
    try {
      await cancelTask(id);
      toast.success("Cancelled");
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    } catch (err) {
      toast.error(`Failed: ${describeError(err)}`);
    }
  };

  if (task.isLoading) {
    return (
      <div className="mx-auto max-w-4xl px-8 py-10 text-[13px] text-fg-2">
        Loading…
      </div>
    );
  }
  if (task.isError || !task.data) {
    return (
      <div className="mx-auto max-w-4xl px-8 py-10">
        <Link
          to="/tasks"
          className="text-[12.5px] text-accent hover:underline"
        >
          ← Tasks
        </Link>
        <div className="mt-4 rounded-md border border-err-soft bg-err-soft px-3 py-2 text-[13px] text-err">
          Couldn't load task:{" "}
          {task.error instanceof Error ? task.error.message : "unknown"}
        </div>
      </div>
    );
  }

  const b = task.data;
  const canAdvance = CANONICAL_NEXT[b.state] ?? [];
  const isTerminal = b.state === "success" || b.state === "cancelled";

  return (
    <div className="mx-auto max-w-[1100px] px-8 py-6">
      {/* Breadcrumb */}
      <Link
        to="/tasks"
        className="inline-flex items-center gap-1 text-[12.5px] text-fg-2 hover:text-accent"
      >
        <ArrowLeft className="h-3.5 w-3.5" strokeWidth={1.75} />
        Tasks
      </Link>

      {/* Header */}
      <header className="mt-3 flex items-start justify-between gap-6">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[20px] font-semibold tracking-tight text-fg-0">
              {b.title}
            </h1>
            {b.jira_key && (
              <span className="shrink-0 rounded border border-border bg-bg-2 px-1.5 py-0.5 font-mono text-[11px] text-fg-2">
                {b.jira_key}
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-fg-2">
            <StateChip state={b.state} />
            <span>· {b.phases_done}/{b.phases_total || 1} phases</span>
            <span>
              · {b.clarifications_pending} clarification
              {b.clarifications_pending === 1 ? "" : "s"} open
            </span>
            {b.branch_name && (
              <span className="font-mono">· branch {b.branch_name}</span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={runDiscussionNow}
            disabled={discussionRunning || isTerminal}
            className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent-soft px-2.5 py-1.5 text-[12px] font-semibold text-accent hover:bg-accent hover:text-bg-0 disabled:opacity-50"
            title="Run one discussion round (costs ~$0.05–$0.10 Sonnet)"
          >
            {discussionRunning ? (
              <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
            ) : (
              <Sparkles className="h-3 w-3" strokeWidth={2} />
            )}
            {discussionRunning ? "Discussing…" : "Run discussion"}
          </button>
          {(b.state === "discuss_done" || b.state === "drafting_plan" ||
            (planArtifacts.data?.some((a) => a.state === "stale") ?? false)) && (
            <button
              onClick={runPlanNow}
              disabled={planRunning || isTerminal}
              className="inline-flex items-center gap-1 rounded-md border border-info bg-info-soft px-2.5 py-1.5 text-[12px] font-semibold text-info hover:bg-info hover:text-bg-0 disabled:opacity-50"
              title="Generate an execution plan from the approved context"
            >
              {planRunning ? (
                <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
              ) : (
                <ListChecks className="h-3 w-3" strokeWidth={2} />
              )}
              {planRunning
                ? "Planning…"
                : planArtifacts.data?.some((a) => a.state === "stale")
                  ? "Re-plan"
                  : "Run plan"}
            </button>
          )}
          {(b.state === "plan_ready" || b.state === "executing" ||
            b.state === "blocked") && (
            <button
              onClick={executeTaskNow}
              disabled={executing || isTerminal}
              className="inline-flex items-center gap-1 rounded-md border border-warn bg-warn-soft px-2.5 py-1.5 text-[12px] font-semibold text-warn hover:bg-warn hover:text-bg-0 disabled:opacity-50"
              title="Run every approved plan inside a git worktree (writes code)"
            >
              {executing ? (
                <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
              ) : (
                <Zap className="h-3 w-3" strokeWidth={2} />
              )}
              {executing ? "Executing…" : "Execute"}
            </button>
          )}
          {(b.state === "verifying" || b.state === "blocked") && (
            <button
              onClick={runVerifyNow}
              disabled={verifying || isTerminal}
              className="inline-flex items-center gap-1 rounded-md border border-ok bg-ok-soft px-2.5 py-1.5 text-[12px] font-semibold text-ok hover:bg-ok hover:text-bg-0 disabled:opacity-50"
              title="Grade the acceptance checks with a verifier agent"
            >
              {verifying ? (
                <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
              ) : (
                <ClipboardCheck className="h-3 w-3" strokeWidth={2} />
              )}
              {verifying ? "Verifying…" : "Run verification"}
            </button>
          )}
          {b.pr_url && (
            <a
              href={b.pr_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent-soft px-2.5 py-1.5 text-[12px] font-semibold text-accent hover:bg-accent hover:text-bg-0"
            >
              <GitPullRequest className="h-3 w-3" strokeWidth={2} />
              {b.pr_number ? `PR #${b.pr_number}` : "PR"}
              <ExternalLink className="h-2.5 w-2.5" strokeWidth={2} />
            </a>
          )}
          <button
            onClick={spawnSideChat}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2.5 py-1.5 text-[12px] font-medium text-fg-1 hover:border-accent hover:text-accent"
            title="Open a side chat with this task as context"
          >
            <MessageSquare className="h-3 w-3" strokeWidth={1.75} />
            Ask chat
          </button>
          {canAdvance.map((s) => (
            <button
              key={s}
              onClick={() => onAdvance(s)}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2.5 py-1.5 text-[12px] font-medium text-fg-1 hover:border-accent hover:text-accent"
              title={`Move to ${s}`}
            >
              <Play className="h-3 w-3" strokeWidth={1.75} />
              {s}
            </button>
          ))}
          {!isTerminal && (
            <button
              onClick={onCancel}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2.5 py-1.5 text-[12px] font-medium text-fg-2 hover:border-err/60 hover:text-err"
            >
              <Ban className="h-3 w-3" strokeWidth={1.75} />
              Cancel
            </button>
          )}
          <button
            onClick={toggleArchive}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2.5 py-1.5 text-[12px] font-medium text-fg-2 hover:border-fg-3 hover:text-fg-0"
            title={
              task.data.archived_at
                ? "Unarchive — return to the live list"
                : "Archive — soft-hide from the live list"
            }
          >
            {task.data.archived_at ? (
              <>
                <ArchiveRestore className="h-3 w-3" strokeWidth={1.75} />
                Unarchive
              </>
            ) : (
              <>
                <Archive className="h-3 w-3" strokeWidth={1.75} />
                Archive
              </>
            )}
          </button>
        </div>
      </header>

      {/* Execution status strip (appears once worktree exists OR task is plan-ready) */}
      {(executionStatus.data?.worktree_path ||
        b.state === "plan_ready" ||
        b.state === "executing" ||
        b.state === "verifying" ||
        b.state === "blocked") && (
        <section className="mt-4 rounded-lg border border-border bg-bg-1 px-4 py-2.5">
          <div className="flex flex-wrap items-center gap-3 text-[12px] text-fg-2">
            <span className="flex items-center gap-1.5 font-semibold text-fg-1">
              <Zap className="h-3 w-3 text-warn" strokeWidth={1.75} />
              Execution
            </span>
            <PlanCountChip
              label="queued"
              n={(executionStatus.data?.counts.approved ?? 0) +
                (executionStatus.data?.counts.pending ?? 0)}
            />
            <PlanCountChip
              label="running"
              n={executionStatus.data?.counts.executing ?? 0}
              animate
            />
            <PlanCountChip
              label="completed"
              n={executionStatus.data?.counts.completed ?? 0}
              tone="ok"
            />
            <PlanCountChip
              label="failed"
              n={executionStatus.data?.counts.failed ?? 0}
              tone="err"
            />
            <PlanCountChip
              label="rejected"
              n={executionStatus.data?.counts.rejected ?? 0}
              tone="warn"
            />
            {executionStatus.data?.branch_name && (
              <span className="inline-flex items-center gap-1 rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-3">
                <GitBranch className="h-2.5 w-2.5" strokeWidth={1.75} />
                {executionStatus.data.branch_name}
              </span>
            )}
            {(b.state === "plan_ready" || b.state === "blocked") && (
              <div className="ml-auto flex items-center gap-1.5">
                <input
                  value={targetRepoPath}
                  onChange={(e) => setTargetRepoPath(e.target.value)}
                  placeholder="/absolute/path/to/target/repo"
                  className="w-[280px] rounded-md border border-border bg-bg-1 px-2 py-1 font-mono text-[11.5px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
                />
              </div>
            )}
          </div>
        </section>
      )}

      {/* Clarification queue — live */}
      <ClarificationQueue
        taskId={id!}
        clarifications={clarifications.data ?? []}
        onAnswered={() => {
          qc.invalidateQueries({ queryKey: ["task", id] });
          qc.invalidateQueries({ queryKey: ["task-clarifications", id] });
        }}
      />

      {/* Discussion-round input (free-text addendum passed to the agent) */}
      {!isTerminal && (
        <section className="mt-3 rounded-lg border border-border bg-bg-1 px-3 py-2">
          <div className="flex items-center gap-2">
            <MessageCircleQuestion
              className="h-3 w-3 shrink-0 text-fg-3"
              strokeWidth={1.75}
            />
            <input
              value={discussionMessage}
              onChange={(e) => setDiscussionMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  runDiscussionNow();
                }
              }}
              disabled={discussionRunning}
              placeholder="Optional note for the next discussion round — ⌘↵ to run"
              className="min-w-0 flex-1 bg-transparent text-[13px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
            />
            {discussionMessage.trim() && (
              <button
                onClick={() => setDiscussionMessage("")}
                className="shrink-0 rounded-full bg-bg-2 px-2 py-0.5 text-[11px] text-fg-2 hover:text-fg-0"
              >
                clear
              </button>
            )}
          </div>
        </section>
      )}

      {/* Jira enrichment — populates on task create when jira_key set */}
      {b.jira_key && (
        <JiraEnrichmentCard
          jiraKey={b.jira_key}
          enrichment={b.jira_enrichment ?? null}
          enrichedAt={b.enriched_at ?? null}
          onRefetch={reEnrichNow}
          refetching={enriching}
        />
      )}

      {/* Jira audit trail — every Jira write Bifrost has performed
          for this task, with per-row retract. Hidden when no audit
          rows exist (read-only or never-Jira-linked tasks). */}
      <JiraAuditPanel taskId={b.id} />

      {/* Intent */}
      <section className="mt-4 rounded-lg border border-border bg-bg-1 px-4 py-3">
        <h2 className="text-[11.5px] font-semibold uppercase tracking-wide text-fg-3">
          Intent
        </h2>
        <p className="mt-1.5 whitespace-pre-wrap text-[13.5px] text-fg-0">
          {b.intent_text}
        </p>
        {b.acceptance_criteria.length > 0 && (
          <div className="mt-3 border-t border-border pt-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-fg-3">
              Acceptance criteria
            </h3>
            <ul className="mt-1.5 space-y-1 text-[13px] text-fg-1">
              {b.acceptance_criteria.map((c, i) => (
                <li key={i}>
                  ✓ {typeof c === "string" ? c : JSON.stringify(c)}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {/* Related conversations */}
      {chats.data && chats.data.length > 0 && (
        <section className="mt-4 rounded-lg border border-border bg-bg-1">
          <header className="flex items-center justify-between border-b border-border px-4 py-2">
            <h2 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
              <MessageSquare
                className="h-3 w-3 text-fg-2"
                strokeWidth={1.75}
              />
              Related conversations
              <span className="font-mono text-[10.5px] text-fg-3">
                ({chats.data.length})
              </span>
            </h2>
          </header>
          <ul className="divide-y divide-border">
            {chats.data.map((c: TaskChatLink) => (
              <li key={c.conversation_id + c.link_type}>
                <Link
                  to={`/?conv=${c.conversation_id}`}
                  className="group flex items-start gap-2.5 px-4 py-2 hover:bg-bg-2"
                >
                  <span
                    className={cn(
                      "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                      c.link_type === "spawned_task"
                        ? "bg-accent-soft text-accent"
                        : c.link_type === "spawned_chat"
                          ? "bg-info-soft text-info"
                          : c.link_type === "correction"
                            ? "bg-warn-soft text-warn"
                            : "bg-bg-2 text-fg-2",
                    )}
                  >
                    {c.link_type.replace("_", " ")}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[13px] font-medium text-fg-0 group-hover:text-accent">
                        {c.title}
                      </span>
                      <span className="font-mono text-[10.5px] text-fg-3">
                        {c.message_count} msg
                      </span>
                    </div>
                    {c.last_message_preview && (
                      <p className="mt-0.5 truncate text-[11.5px] text-fg-3">
                        {c.last_message_preview}
                      </p>
                    )}
                  </div>
                  <ExternalLink
                    className="h-3 w-3 shrink-0 text-fg-3 group-hover:text-accent"
                    strokeWidth={1.75}
                  />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Phase timeline + latest context artifact */}
      <section className="mt-4 rounded-lg border border-border bg-bg-1">
        <header className="flex items-center justify-between border-b border-border px-4 py-2">
          <h2 className="text-[12.5px] font-semibold text-fg-1">
            Phase timeline
          </h2>
          <span className="text-[10.5px] text-fg-3">
            Discuss → Plan → Execute → Verify
          </span>
        </header>
        {(phases.data?.length ?? 0) === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-center">
            <ClipboardList
              className="mb-2 h-8 w-8 text-fg-3"
              strokeWidth={1.5}
            />
            <p className="text-[13px] font-medium text-fg-1">
              No phase yet
            </p>
            <p className="mt-1 max-w-sm text-[12px] text-fg-3">
              Click <span className="font-semibold text-accent">Run
              discussion</span> above and the agent will create the first
              phase + a shared-understanding artifact.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {phases.data!.map((p) => {
              const ctx = contexts.data?.find((a) => a.phase_id === p.id);
              return (
                <li key={p.id} className="p-4">
                  <div className="flex items-center gap-2 text-[12px] text-fg-2">
                    <span className="font-mono text-[10.5px] text-fg-3">
                      {String(p.order_index + 1).padStart(2, "0")}
                    </span>
                    <span className="text-[13.5px] font-semibold text-fg-0">
                      {p.title}
                    </span>
                    <StepPill step={p.current_step} />
                    <span className="text-[10.5px] text-fg-3">
                      · {p.slug}
                    </span>
                  </div>
                  {ctx ? (
                    <ContextArtifactCard
                      taskId={id!}
                      artifact={ctx}
                      onApprove={approveArtifactNow}
                      phaseId={p.id}
                    />
                  ) : (
                    <p className="mt-2 text-[12px] text-fg-3">
                      No context artifact yet — run the discussion agent.
                    </p>
                  )}
                  {(() => {
                    const plan = planArtifacts.data?.find(
                      (a) => a.phase_id === p.id,
                    );
                    if (!plan) return null;
                    const phasePlans = (plans.data ?? []).filter(
                      (pr) => pr.phase_id === p.id,
                    );
                    return (
                      <PlanArtifactCard
                        taskId={id!}
                        artifact={plan}
                        planRows={phasePlans}
                        onApprove={approveArtifactNow}
                        onRetry={retryPlanNow}
                        onDebug={runDebugNow}
                        debugReports={debugReports}
                        debugRunningFor={debugRunningFor}
                      />
                    );
                  })()}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* PR panel — success or already-pushed state */}
      {(b.state === "success" || b.pushed_at) && (
        <section className="mt-4 rounded-lg border border-accent/40 bg-accent-soft/30 px-4 py-3">
          <header className="mb-2 flex items-center justify-between">
            <h2 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
              <GitPullRequest className="h-3 w-3 text-accent" strokeWidth={1.75} />
              Pull request
              {b.pr_url ? (
                <span className="rounded-full bg-accent-soft px-1.5 py-0.5 font-mono text-[10.5px] text-accent">
                  #{b.pr_number} open
                </span>
              ) : b.pushed_at ? (
                <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
                  pushed
                </span>
              ) : null}
            </h2>
            {b.pushed_at && (
              <span className="font-mono text-[10.5px] text-fg-3">
                pushed {new Date(b.pushed_at).toLocaleString()}
              </span>
            )}
          </header>
          {b.pr_url ? (
            <a
              href={b.pr_url}
              target="_blank"
              rel="noreferrer"
              className="block truncate rounded-md border border-border bg-bg-1 px-3 py-2 font-mono text-[12px] text-accent hover:border-accent"
            >
              {b.pr_url} <ExternalLink className="inline h-3 w-3" />
            </a>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1 text-[11.5px] text-fg-2">
                base
                <input
                  value={prBase}
                  onChange={(e) => setPrBase(e.target.value)}
                  className="w-[120px] rounded-md border border-border bg-bg-1 px-2 py-1 font-mono text-[11.5px] text-fg-0 focus:border-accent focus:outline-none"
                />
              </label>
              <label className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] text-fg-2 hover:text-fg-0">
                <input
                  type="checkbox"
                  checked={prDraft}
                  onChange={(e) => setPrDraft(e.target.checked)}
                  className="accent-accent"
                />
                draft
              </label>
              <label className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11.5px] text-fg-2 hover:text-fg-0">
                <input
                  type="checkbox"
                  checked={prUseGh}
                  onChange={(e) => setPrUseGh(e.target.checked)}
                  className="accent-accent"
                />
                gh pr create
              </label>
              <button
                onClick={() => openPrNow(true)}
                disabled={prRunning}
                className="ml-auto inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2.5 py-1 text-[11.5px] font-medium text-fg-1 hover:border-accent hover:text-accent disabled:opacity-50"
              >
                <Eye className="h-3 w-3" strokeWidth={1.75} />
                Preview
              </button>
              <button
                onClick={() => openPrNow(false)}
                disabled={prRunning}
                className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent px-2.5 py-1 text-[11.5px] font-semibold text-bg-0 hover:bg-accent/80 disabled:opacity-50"
              >
                {prRunning ? (
                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                ) : (
                  <GitPullRequest className="h-3 w-3" strokeWidth={2} />
                )}
                {prRunning ? "Pushing…" : "Open PR"}
              </button>
            </div>
          )}
          {prPreview && (
            <div className="mt-3 rounded-md border border-border bg-bg-0">
              <header className="flex items-center justify-between border-b border-border px-3 py-1.5">
                <h3 className="text-[11.5px] font-semibold text-fg-1">
                  Preview
                  <span className="ml-1.5 font-mono text-[10.5px] text-fg-3">
                    {prPreview.branch} → {prPreview.base}
                  </span>
                </h3>
                <button
                  onClick={() => setPrPreview(null)}
                  className="text-[10.5px] text-fg-3 hover:text-fg-1"
                >
                  close
                </button>
              </header>
              <div className="border-b border-border px-3 py-2">
                <p className="text-[11px] font-mono uppercase tracking-wide text-fg-3">
                  title
                </p>
                <p className="mt-0.5 text-[13px] font-semibold text-fg-0">
                  {prPreview.title}
                </p>
              </div>
              <div className="max-h-[300px] overflow-y-auto border-b border-border px-3 py-2">
                <p className="text-[11px] font-mono uppercase tracking-wide text-fg-3">
                  body
                </p>
                <Prose className="mt-1">{prPreview.body}</Prose>
              </div>
              <div className="px-3 py-1.5 font-mono text-[10.5px] text-fg-3">
                {prPreview.commands_run.map((c, i) => (
                  <div key={i}>$ {c}</div>
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {/* Verify items + overall summary */}
      {(verifyItems.data?.length ?? 0) > 0 && (
        <section className="mt-4 rounded-lg border border-border bg-bg-1">
          <header className="flex items-center justify-between border-b border-border px-4 py-2">
            <h2 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
              <ListChecks className="h-3 w-3 text-fg-3" strokeWidth={1.75} />
              Acceptance checks
              <span className="font-mono text-[10.5px] text-fg-3">
                ({verifyItems.data!.length})
              </span>
            </h2>
            <VerifyCountStrip items={verifyItems.data!} />
          </header>
          {verifySummary && (
            <div className="border-b border-border bg-bg-2/40 px-4 py-2 text-[12px] text-fg-1">
              <span className="mr-1.5 font-mono text-[10.5px] uppercase text-fg-3">
                summary
              </span>
              {verifySummary}
            </div>
          )}
          <ul className="divide-y divide-border">
            {verifyItems.data!.map((v) => (
              <VerifyItemRow
                key={v.id}
                item={v}
                onOverride={overrideVerifyNow}
              />
            ))}
          </ul>
        </section>
      )}

      {/* Event log */}
      <section className="mt-4 rounded-lg border border-border bg-bg-1">
        <header className="border-b border-border px-4 py-2">
          <h2 className="text-[12.5px] font-semibold text-fg-1">
            Events{" "}
            <span className="font-normal text-fg-3">
              ({b.events.length})
            </span>
          </h2>
        </header>
        {b.events.length === 0 ? (
          <div className="px-4 py-6 text-center text-[12px] text-fg-3">
            No events yet
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {b.events.map((e, i) => (
              <li key={i} className="px-4 py-2 text-[12px]">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[10.5px] text-fg-3">
                    {new Date(e.at).toLocaleTimeString()}
                  </span>
                  <span
                    className={cn(
                      "inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                      e.actor === "user"
                        ? "bg-accent-soft text-accent"
                        : e.actor === "system"
                          ? "bg-bg-2 text-fg-2"
                          : "bg-info-soft text-info",
                    )}
                  >
                    {e.actor}
                  </span>
                  <span className="font-medium text-fg-1">{e.action}</span>
                  {e.payload && Object.keys(e.payload).length > 0 && (
                    <span className="truncate font-mono text-[10.5px] text-fg-3">
                      {JSON.stringify(e.payload)}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/* ── Helpers ───────────────────────────────────────────────── */

function StateChip({ state }: { state: string }) {
  const cfg: Record<
    string,
    { label: string; tone: "fg" | "ok" | "warn" | "err" | "accent" | "info" }
  > = {
    draft_intent: { label: "Draft", tone: "fg" },
    discussing: { label: "Discussing", tone: "accent" },
    discuss_done: { label: "Discuss done", tone: "accent" },
    drafting_plan: { label: "Drafting plan", tone: "info" },
    plan_ready: { label: "Plan ready", tone: "info" },
    executing: { label: "Executing", tone: "accent" },
    verifying: { label: "Verifying", tone: "info" },
    success: { label: "Done", tone: "ok" },
    blocked: { label: "Blocked", tone: "err" },
    cancelled: { label: "Cancelled", tone: "err" },
  };
  const c = cfg[state] ?? { label: state, tone: "fg" };
  const Icon = {
    ok: CheckCircle2,
    err: XCircle,
    accent: Loader2,
    info: Clock,
    warn: AlertTriangle,
    fg: Clock,
  }[c.tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] font-medium",
        c.tone === "ok" && "bg-ok-soft text-ok",
        c.tone === "err" && "bg-err-soft text-err",
        c.tone === "accent" && "bg-accent-soft text-accent",
        c.tone === "info" && "bg-info-soft text-info",
        c.tone === "fg" && "bg-bg-2 text-fg-2",
      )}
    >
      <Icon
        className={cn(
          "h-2.5 w-2.5",
          c.tone === "accent" && state !== "discuss_done" && "animate-spin",
        )}
        strokeWidth={2}
      />
      {c.label}
    </span>
  );
}

/** Legal forward transitions mirror task_lifecycle.py. Keep in sync. */
const CANONICAL_NEXT: Record<string, string[]> = {
  draft_intent: ["discussing"],
  discussing: ["discuss_done"],
  discuss_done: ["drafting_plan"],
  drafting_plan: ["plan_ready"],
  plan_ready: ["executing"],
  executing: ["verifying"],
  verifying: ["success", "blocked"],
  blocked: ["executing", "drafting_plan"],
};


/* ── Clarification Queue ─────────────────────────────────────── */


function ClarificationQueue({
  taskId,
  clarifications,
  onAnswered,
}: {
  taskId: string;
  clarifications: Clarification[];
  onAnswered: () => void;
}) {
  if (!clarifications.length) return null;
  const blockers = clarifications.filter((c) => c.blocks_state);
  return (
    <section
      className={cn(
        "mt-4 rounded-lg border px-3 py-2",
        blockers.length
          ? "border-warn/50 bg-warn-soft/30"
          : "border-border bg-bg-1",
      )}
    >
      <header className="flex items-center gap-1.5 pb-1.5">
        <AlertTriangle
          className={cn(
            "h-3 w-3",
            blockers.length ? "text-warn" : "text-fg-3",
          )}
          strokeWidth={1.75}
        />
        <h2 className="text-[12.5px] font-semibold text-fg-1">
          Clarification queue{" "}
          <span className="font-normal text-fg-3">
            ({clarifications.length})
          </span>
        </h2>
        {blockers.length > 0 && (
          <span className="rounded-full bg-warn/20 px-1.5 py-0.5 font-mono text-[10px] text-warn">
            {blockers.length} blocker{blockers.length === 1 ? "" : "s"}
          </span>
        )}
      </header>
      <ul className="divide-y divide-border">
        {clarifications.map((c) => (
          <ClarificationRow
            key={c.id}
            taskId={taskId}
            clar={c}
            onAnswered={onAnswered}
          />
        ))}
      </ul>
    </section>
  );
}


function ClarificationRow({
  taskId,
  clar,
  onAnswered,
}: {
  taskId: string;
  clar: Clarification;
  onAnswered: () => void;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (value: string) => {
    if (!value.trim() || busy) return;
    setBusy(true);
    try {
      await answerClarification(taskId, clar.id, value.trim(), "user");
      toast.success("Answered");
      onAnswered();
    } catch (err) {
      toast.error(
        `Failed: ${describeError(err)}`,
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="py-2">
      <div className="flex items-start gap-2">
        <KindIcon kind={clar.kind} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="text-[13px] text-fg-0">{clar.question}</p>
            {clar.blocks_state && (
              <span className="rounded-full bg-warn/20 px-1.5 py-0.5 font-mono text-[10px] text-warn">
                blocks {clar.blocks_state}
              </span>
            )}
            <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-3">
              {clar.kind}
            </span>
          </div>
          {clar.options && clar.options.length > 0 ? (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {clar.options.map((opt, i) => (
                <button
                  key={i}
                  disabled={busy}
                  onClick={() => submit(opt.label || opt.value)}
                  className="rounded-md border border-border bg-bg-1 px-2 py-1 text-[12px] text-fg-1 hover:border-accent hover:text-accent disabled:opacity-50"
                >
                  {opt.label || opt.value}
                </button>
              ))}
              <input
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submit(answer);
                }}
                placeholder="or type another answer…"
                disabled={busy}
                className="min-w-[200px] flex-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[12px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
              />
            </div>
          ) : (
            <div className="mt-1.5 flex items-center gap-1.5">
              <input
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submit(answer);
                }}
                placeholder="Your answer…"
                disabled={busy}
                className="flex-1 rounded-md border border-border bg-bg-1 px-2 py-1 text-[12px] text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
              />
              <button
                onClick={() => submit(answer)}
                disabled={busy || !answer.trim()}
                className="rounded-md border border-accent bg-accent-soft px-2 py-1 text-[12px] font-medium text-accent hover:bg-accent hover:text-bg-0 disabled:opacity-40"
              >
                {busy ? "…" : "Answer"}
              </button>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}


function KindIcon({ kind }: { kind: Clarification["kind"] }) {
  const Icon =
    kind === "conflict"
      ? ShieldAlert
      : kind === "ambiguity"
        ? MessageCircleQuestion
        : kind === "need_research"
          ? Sparkles
          : Shield;
  const color =
    kind === "conflict"
      ? "text-err"
      : kind === "ambiguity"
        ? "text-warn"
        : kind === "need_research"
          ? "text-info"
          : "text-accent";
  return (
    <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", color)} strokeWidth={1.75} />
  );
}


/* ── Context artifact card ──────────────────────────────────── */


function ContextArtifactCard({
  taskId,
  artifact,
  onApprove,
  phaseId,
}: {
  taskId: string;
  artifact: Artifact;
  onApprove: (artifactId: string) => void;
  phaseId: string;
}) {
  const assumptions = useQuery({
    queryKey: ["task-assumptions", taskId, artifact.id],
    queryFn: () => listAssumptions(taskId, artifact.id, "active"),
    staleTime: 30_000,
  });
  // artifact.parsed is typed Record<string, unknown> — bracket access
  // preserves that; the previous ``as any`` weakened the whole chain.
  const parsedReady = artifact.parsed?.["ready_to_plan"] === true;
  const canApprove = artifact.state === "draft";
  return (
    <div
      className={cn(
        "mt-2.5 rounded-md border bg-bg-0",
        artifact.state === "stale"
          ? "border-warn/50"
          : "border-border",
      )}
    >
      <header className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <div className="flex items-center gap-1.5 text-[11px] text-fg-2">
          <span className="font-mono font-semibold text-fg-1">
            context.md
          </span>
          <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-3">
            v{artifact.version}
          </span>
          <ArtifactStateBadge state={artifact.state} />
          {parsedReady && (
            <span className="rounded-full bg-ok-soft px-1.5 py-0.5 font-mono text-[10px] text-ok">
              ready to plan
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10.5px] text-fg-3">
            {new Date(artifact.created_at).toLocaleString()}
          </span>
          {canApprove && (
            <button
              onClick={() => onApprove(artifact.id)}
              className="rounded-md border border-accent bg-accent-soft px-1.5 py-0.5 text-[10.5px] font-semibold text-accent hover:bg-accent hover:text-bg-0"
            >
              Approve
            </button>
          )}
        </div>
      </header>
      <details open className="group">
        <summary className="flex cursor-pointer select-none items-center gap-1 px-3 py-1.5 text-[11px] font-medium text-fg-2 hover:text-fg-0">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Shared understanding
        </summary>
        <div className="max-h-[480px] overflow-y-auto border-t border-border px-4 py-3">
          <Prose>{artifact.body_md || "*(no content)*"}</Prose>
        </div>
      </details>
      <details className="group border-t border-border">
        <summary className="flex cursor-pointer select-none items-center gap-1 px-3 py-1.5 text-[11px] font-medium text-fg-2 hover:text-fg-0">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Assumptions{" "}
          <span className="font-normal text-fg-3">
            ({assumptions.data?.length ?? "…"})
          </span>
        </summary>
        <div className="max-h-[320px] overflow-y-auto border-t border-border">
          {(assumptions.data?.length ?? 0) === 0 ? (
            <p className="p-3 text-[12px] text-fg-3">
              No active assumptions.
            </p>
          ) : (
            <table className="w-full text-[12px]">
              <thead className="bg-bg-2 text-[10.5px] uppercase tracking-wide text-fg-3">
                <tr>
                  <th className="px-3 py-1.5 text-left">Key</th>
                  <th className="px-3 py-1.5 text-left">Value</th>
                  <th className="px-3 py-1.5 text-left">Source</th>
                  <th className="px-3 py-1.5 text-right">Tier</th>
                  <th className="px-3 py-1.5 text-right">Conf</th>
                </tr>
              </thead>
              <tbody>
                {assumptions.data!.map((a) => (
                  <AssumptionRow key={a.id} a={a} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </details>
      {artifact.version > 1 && (
        <AssumptionEvolutionPanel
          taskId={taskId}
          phaseId={phaseId}
        />
      )}
    </div>
  );
}


function AssumptionEvolutionPanel({
  taskId,
  phaseId,
}: {
  taskId: string;
  phaseId: string;
}) {
  const [open, setOpen] = useState(false);
  const history = useQuery<AssumptionHistory>({
    queryKey: ["assumption-history", taskId, phaseId],
    queryFn: () => getAssumptionHistory(taskId, phaseId),
    enabled: open,
    staleTime: 20_000,
  });
  return (
    <details
      className="group border-t border-border"
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer select-none items-center gap-1 px-3 py-1.5 text-[11px] font-medium text-fg-2 hover:text-fg-0">
        <ChevronRight
          className="h-3 w-3 transition-transform group-open:rotate-90"
          strokeWidth={1.75}
        />
        Assumption evolution across versions
      </summary>
      <div className="overflow-x-auto border-t border-border">
        {!open ? null : history.isLoading ? (
          <p className="px-3 py-3 text-[12px] text-fg-3">Loading…</p>
        ) : !history.data || Object.keys(history.data.keys).length === 0 ? (
          <p className="px-3 py-3 text-[12px] text-fg-3">
            Only one version of this artifact exists — no evolution yet.
          </p>
        ) : (
          <AssumptionHistoryTable history={history.data} />
        )}
      </div>
    </details>
  );
}


function AssumptionHistoryTable({
  history,
}: {
  history: AssumptionHistory;
}) {
  const versions = history.versions;
  const sortedKeys = Object.keys(history.keys).sort((a, b) => {
    // Natural sort: A1, A2, ..., A10, A11
    const parseN = (s: string) => parseInt(s.replace(/^[A-Z]+/, ""), 10);
    return parseN(a) - parseN(b);
  });
  return (
    <table className="w-full text-[11px]">
      <thead className="bg-bg-2 text-[10px] uppercase tracking-wide text-fg-3">
        <tr>
          <th className="sticky left-0 bg-bg-2 px-2 py-1.5 text-left">Key</th>
          {versions.map((v) => (
            <th key={v.version} className="px-2 py-1.5 text-center">
              v{v.version}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sortedKeys.map((k) => (
          <AssumptionEvolutionRow
            key={k}
            assumptionKey={k}
            versions={versions}
            trail={history.keys[k]}
          />
        ))}
      </tbody>
    </table>
  );
}


function AssumptionEvolutionRow({
  assumptionKey,
  versions,
  trail,
}: {
  assumptionKey: string;
  versions: AssumptionHistory["versions"];
  trail: AssumptionVersion[];
}) {
  // Build a lookup by artifact_version so we can render one cell per
  // version, even when a key was absent in some.
  const byVersion = new Map<number, AssumptionVersion>();
  for (const v of trail) byVersion.set(v.artifact_version, v);
  let prev: AssumptionVersion | undefined = undefined;
  return (
    <tr className="border-t border-border/60 align-top">
      <td className="sticky left-0 bg-bg-1 px-2 py-1.5 font-mono text-[11px] font-semibold text-fg-2">
        {assumptionKey}
      </td>
      {versions.map((v) => {
        const entry = byVersion.get(v.version);
        const diffTier = prev && entry && prev.tier !== entry.tier;
        const diffSource =
          prev && entry && prev.source_type !== entry.source_type;
        const diffState = prev && entry && prev.state !== entry.state;
        const changed = diffTier || diffSource || diffState;
        const cell = !entry ? (
          <td
            key={v.version}
            className="px-2 py-1.5 text-center text-fg-3"
          >
            <span className="font-mono text-[10px]">—</span>
          </td>
        ) : (
          <td
            key={v.version}
            className={cn(
              "px-2 py-1.5",
              changed && "bg-accent-soft/30",
            )}
            title={entry.value}
          >
            <div className="flex items-center gap-1">
              <span
                className={cn(
                  "rounded px-1 py-0.5 font-mono text-[10px]",
                  entry.tier <= 2
                    ? "bg-ok-soft text-ok"
                    : entry.tier === 3
                      ? "bg-info-soft text-info"
                      : entry.tier === 4
                        ? "bg-warn-soft text-warn"
                        : "bg-bg-2 text-fg-3",
                  diffTier && "ring-1 ring-accent",
                )}
              >
                T{entry.tier}
              </span>
              {entry.state !== "active" && (
                <span
                  className={cn(
                    "rounded px-1 py-0.5 font-mono text-[10px]",
                    entry.state === "retracted"
                      ? "bg-err-soft text-err"
                      : "bg-warn-soft text-warn",
                  )}
                >
                  {entry.state}
                </span>
              )}
              <span className="font-mono text-[10px] text-fg-3">
                {entry.confidence.toFixed(2)}
              </span>
            </div>
            <div className="mt-0.5 truncate text-[10.5px] text-fg-2 max-w-[260px]">
              {entry.source_type}
            </div>
          </td>
        );
        if (entry) prev = entry;
        return cell;
      })}
    </tr>
  );
}


function AssumptionRow({ a }: { a: Assumption }) {
  return (
    <tr className="border-t border-border/60 align-top hover:bg-bg-2/40">
      <td className="px-3 py-1.5 font-mono text-[11px] font-semibold text-fg-2">
        {a.key}
      </td>
      <td className="px-3 py-1.5 text-fg-0">{a.value}</td>
      <td className="px-3 py-1.5 text-fg-2">
        <span className="rounded bg-bg-2 px-1 py-0.5 font-mono text-[10.5px]">
          {a.source_type}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right font-mono">
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10.5px]",
            a.tier <= 2
              ? "bg-ok-soft text-ok"
              : a.tier === 3
                ? "bg-info-soft text-info"
                : a.tier === 4
                  ? "bg-warn-soft text-warn"
                  : "bg-bg-2 text-fg-3",
          )}
        >
          T{a.tier}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-[11px] text-fg-2">
        {a.confidence.toFixed(2)}
      </td>
    </tr>
  );
}


/* ── Plan artifact card ─────────────────────────────────────── */


function PlanArtifactCard({
  taskId,
  artifact,
  planRows,
  onApprove,
  onRetry,
  onDebug,
  debugReports,
  debugRunningFor,
}: {
  taskId: string;
  artifact: Artifact;
  planRows: PlanRow[];
  onApprove: (artifactId: string) => void;
  onRetry: (planId: string) => void;
  onDebug: (planId: string) => void;
  debugReports: Record<string, DebugReport>;
  debugRunningFor: string | null;
}) {
  const canApprove = artifact.state === "draft";
  const parsed = artifact.parsed ?? {};
  const readyToExecute = parsed["ready_to_execute"] === true;
  return (
    <div
      className={cn(
        "mt-2 rounded-md border bg-bg-0",
        artifact.state === "stale" ? "border-warn/60" : "border-border",
      )}
    >
      <header className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <div className="flex items-center gap-1.5 text-[11px] text-fg-2">
          <span className="font-mono font-semibold text-fg-1">plan.md</span>
          <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-3">
            v{artifact.version}
          </span>
          <ArtifactStateBadge state={artifact.state} />
          {readyToExecute && artifact.state !== "stale" && (
            <span className="rounded-full bg-ok-soft px-1.5 py-0.5 font-mono text-[10px] text-ok">
              ready to execute
            </span>
          )}
          {artifact.state === "stale" && (
            <span className="rounded-full bg-warn/20 px-1.5 py-0.5 font-mono text-[10px] text-warn">
              dependent assumption retracted
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10.5px] text-fg-3">
            {new Date(artifact.created_at).toLocaleString()}
          </span>
          {canApprove && (
            <button
              onClick={() => onApprove(artifact.id)}
              className="rounded-md border border-accent bg-accent-soft px-1.5 py-0.5 text-[10.5px] font-semibold text-accent hover:bg-accent hover:text-bg-0"
            >
              Approve
            </button>
          )}
        </div>
      </header>
      <details open={artifact.state !== "stale"} className="group">
        <summary className="flex cursor-pointer select-none items-center gap-1 px-3 py-1.5 text-[11px] font-medium text-fg-2 hover:text-fg-0">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Plan overview
        </summary>
        <div className="max-h-[480px] overflow-y-auto border-t border-border px-4 py-3">
          <Prose>{artifact.body_md || "*(no content)*"}</Prose>
        </div>
      </details>
      <details open={planRows.length > 0 && planRows.length <= 8} className="group border-t border-border">
        <summary className="flex cursor-pointer select-none items-center gap-1 px-3 py-1.5 text-[11px] font-medium text-fg-2 hover:text-fg-0">
          <ChevronRight
            className="h-3 w-3 transition-transform group-open:rotate-90"
            strokeWidth={1.75}
          />
          Atomic tasks{" "}
          <span className="font-normal text-fg-3">({planRows.length})</span>
        </summary>
        <div className="max-h-[500px] overflow-y-auto border-t border-border">
          {planRows.length === 0 ? (
            <p className="p-3 text-[12px] text-fg-3">No tasks yet.</p>
          ) : (
            <ul className="divide-y divide-border">
              {planRows.map((p) => (
                <PlanTaskRow
                  key={p.id}
                  plan={p}
                  onRetry={onRetry}
                  onDebug={onDebug}
                  debugReport={debugReports[p.id]}
                  debugRunning={debugRunningFor === p.id}
                />
              ))}
            </ul>
          )}
        </div>
      </details>
    </div>
  );
}


function PlanTaskRow({
  plan,
  onRetry,
  onDebug,
  debugReport,
  debugRunning,
}: {
  plan: PlanRow;
  onRetry: (planId: string) => void;
  onDebug: (planId: string) => void;
  debugReport?: DebugReport;
  debugRunning?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const tone =
    plan.status === "approved"
      ? "text-ok"
      : plan.status === "rejected"
        ? "text-warn"
        : plan.status === "executing"
          ? "text-accent"
          : plan.status === "completed"
            ? "text-ok"
            : plan.status === "failed"
              ? "text-err"
              : "text-fg-2";
  return (
    <li className="px-3 py-2">
      <div
        className="flex cursor-pointer items-start gap-2"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="shrink-0 rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-2">
          W{plan.wave}
        </span>
        <span className="shrink-0 font-mono text-[10.5px] font-semibold text-fg-3">
          {plan.plan_key}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="text-[12.5px] font-medium text-fg-0">
              {plan.name}
            </span>
            <span
              className={cn(
                "rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px]",
                tone,
              )}
            >
              {plan.status === "executing" ? (
                <Loader2
                  className="mr-0.5 inline h-2.5 w-2.5 animate-spin"
                  strokeWidth={2}
                />
              ) : null}
              {plan.status}
            </span>
            {plan.git_sha && (
              <span className="font-mono text-[10px] text-ok">
                {plan.git_sha}
              </span>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-1.5 text-[10.5px] text-fg-3">
            {plan.files.map((f) => (
              <span key={f} className="inline-flex items-center gap-0.5 font-mono">
                <FileCode className="h-2.5 w-2.5" strokeWidth={1.75} />
                {f}
              </span>
            ))}
            {plan.depends_on.length > 0 && (
              <span className="inline-flex items-center gap-0.5 font-mono">
                <GitBranch className="h-2.5 w-2.5" strokeWidth={1.75} />
                depends: {plan.depends_on.join(", ")}
              </span>
            )}
            <span className="font-mono">
              {plan.assumption_ids.length} assumption
              {plan.assumption_ids.length === 1 ? "" : "s"}
            </span>
          </div>
        </div>
        {plan.status === "failed" && (
          <div className="flex shrink-0 gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDebug(plan.id);
              }}
              disabled={debugRunning}
              className="inline-flex items-center gap-0.5 rounded-md border border-border bg-bg-1 px-1.5 py-0.5 text-[10.5px] font-medium text-fg-1 hover:border-warn hover:text-warn disabled:opacity-50"
              title="Diagnose this failure with a debug agent"
            >
              {debugRunning ? (
                <Loader2 className="h-2.5 w-2.5 animate-spin" strokeWidth={2} />
              ) : (
                <Bug className="h-2.5 w-2.5" strokeWidth={1.75} />
              )}
              debug
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRetry(plan.id);
              }}
              className="inline-flex items-center gap-0.5 rounded-md border border-border bg-bg-1 px-1.5 py-0.5 text-[10.5px] font-medium text-fg-1 hover:border-accent hover:text-accent"
              title="Re-run this plan in the existing worktree"
            >
              <RotateCcw className="h-2.5 w-2.5" strokeWidth={1.75} />
              retry
            </button>
          </div>
        )}
        <ChevronRight
          className={cn(
            "mt-1 h-3 w-3 text-fg-3 transition-transform",
            expanded && "rotate-90",
          )}
          strokeWidth={1.75}
        />
      </div>
      {expanded && (
        <div className="mt-2 space-y-1 border-l-2 border-border pl-3 text-[11.5px] text-fg-1">
          <div>
            <span className="text-fg-3">Action:</span>{" "}
            <span className="whitespace-pre-wrap">{plan.action_body}</span>
          </div>
          {plan.verify_cmd && (
            <div className="font-mono text-[11px]">
              <span className="text-fg-3">Verify:</span>{" "}
              <code className="rounded bg-bg-2 px-1">{plan.verify_cmd}</code>
            </div>
          )}
          <div>
            <span className="text-fg-3">Done when:</span> {plan.done_criterion}
          </div>
          {plan.confidence_basis.length > 0 && (
            <div className="text-[10.5px] text-fg-3">
              {plan.confidence_basis.join(" · ")}
            </div>
          )}
        </div>
      )}
      {debugReport && (
        <div className="mt-2 rounded-md border border-warn/40 bg-warn-soft/30 px-2.5 py-2 text-[11.5px]">
          <div className="mb-1 flex items-center gap-1.5 text-fg-2">
            <Bug className="h-2.5 w-2.5 text-warn" strokeWidth={1.75} />
            <span className="font-mono text-[10.5px] uppercase tracking-wide">
              debug report
            </span>
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
                debugReport.retry_safe
                  ? "bg-ok-soft text-ok"
                  : "bg-bg-2 text-fg-3",
              )}
            >
              {debugReport.retry_safe ? "retry plausible" : "needs human"}
            </span>
            <span className="ml-auto font-mono text-[10px] text-fg-3">
              {debugReport.tool_calls} tools · ${debugReport.cost_usd.toFixed(3)}
            </span>
          </div>
          <p className="text-fg-0">
            <span className="font-mono text-[10.5px] text-fg-3">
              diagnosis ·
            </span>{" "}
            {debugReport.diagnosis}
          </p>
          <p className="mt-1 text-fg-1">
            <span className="font-mono text-[10.5px] text-fg-3">
              fix ·
            </span>{" "}
            {debugReport.suggested_fix}
          </p>
          {debugReport.retry_safe && debugReport.retry_prompt && (
            <div className="mt-2 flex items-start gap-1.5">
              <code className="flex-1 truncate rounded bg-bg-2 px-2 py-1 font-mono text-[10.5px] text-fg-1">
                {debugReport.retry_prompt}
              </code>
              <button
                onClick={() => onRetry(plan.id)}
                className="shrink-0 rounded-md border border-accent bg-accent-soft px-2 py-1 text-[10.5px] font-semibold text-accent hover:bg-accent hover:text-bg-0"
                title="Retry this plan using the suggested hint"
              >
                retry with fix
              </button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}


function ArtifactStateBadge({ state }: { state: string }) {
  const cfg: Record<string, { label: string; cn: string }> = {
    draft: { label: "draft", cn: "bg-bg-2 text-fg-3" },
    approved: { label: "approved", cn: "bg-ok-soft text-ok" },
    stale: { label: "stale", cn: "bg-warn-soft text-warn" },
    invalidated: { label: "invalidated", cn: "bg-err-soft text-err" },
  };
  const c = cfg[state] ?? cfg.draft;
  return (
    <span
      className={cn("rounded-full px-1.5 py-0.5 font-mono text-[10px]", c.cn)}
    >
      {c.label}
    </span>
  );
}


/* ── Jira enrichment card ───────────────────────────────────── */


function JiraEnrichmentCard({
  jiraKey,
  enrichment,
  enrichedAt,
  onRefetch,
  refetching,
}: {
  jiraKey: string;
  enrichment: JiraEnrichment | null;
  enrichedAt: string | null;
  onRefetch: () => void;
  refetching: boolean;
}) {
  const missing = !enrichment;
  return (
    <section
      className={cn(
        "mt-4 rounded-lg border bg-bg-1",
        missing ? "border-border" : "border-accent/40 bg-accent-soft/20",
      )}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-2">
        <h2 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-fg-1">
          <FileText className="h-3 w-3 text-accent" strokeWidth={1.75} />
          Jira
          <a
            href={enrichment?.url ?? undefined}
            target="_blank"
            rel="noreferrer"
            className={cn(
              "rounded-full px-1.5 py-0.5 font-mono text-[10.5px]",
              enrichment?.url
                ? "bg-accent-soft text-accent hover:bg-accent hover:text-bg-0"
                : "bg-bg-2 text-fg-2",
            )}
          >
            {jiraKey}
          </a>
          {enrichment?.status && (
            <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2">
              {enrichment.status}
            </span>
          )}
          {enrichment?.priority && (
            <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-3">
              {enrichment.priority}
            </span>
          )}
          {enrichment?.source && enrichment.source !== "none" && (
            <span className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-3">
              via {enrichment.source}
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2">
          {enrichedAt && (
            <span className="font-mono text-[10.5px] text-fg-3">
              {new Date(enrichedAt).toLocaleString()}
            </span>
          )}
          <button
            onClick={onRefetch}
            disabled={refetching}
            className="inline-flex items-center gap-0.5 rounded-md border border-border bg-bg-1 px-2 py-1 text-[11px] font-medium text-fg-1 hover:border-accent hover:text-accent disabled:opacity-50"
            title="Re-fetch from cache (or live if Jira creds configured)"
          >
            {refetching ? (
              <Loader2 className="h-2.5 w-2.5 animate-spin" strokeWidth={2} />
            ) : (
              <RefreshCw className="h-2.5 w-2.5" strokeWidth={1.75} />
            )}
            re-fetch
          </button>
        </div>
      </header>
      {missing ? (
        <div className="px-4 py-3 text-[12px] text-fg-3">
          Not yet enriched. Click{" "}
          <span className="font-semibold">re-fetch</span> to try the local
          ``jira_issues`` cache (or live Jira if{" "}
          <code className="font-mono text-[11px]">JIRA_BASE_URL</code> /{" "}
          <code className="font-mono text-[11px]">JIRA_EMAIL</code> /{" "}
          <code className="font-mono text-[11px]">JIRA_API_TOKEN</code> are
          configured).
        </div>
      ) : (
        <div className="px-4 py-3">
          {enrichment?.title && (
            <h3 className="text-[13.5px] font-semibold text-fg-0">
              {enrichment.title}
            </h3>
          )}
          <div className="mt-1 flex flex-wrap gap-1.5 text-[10.5px]">
            {(enrichment?.components ?? []).map((c) => (
              <span
                key={c}
                className="inline-flex items-center gap-0.5 rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-fg-2"
              >
                <Tag className="h-2.5 w-2.5" strokeWidth={1.75} />
                {c}
              </span>
            ))}
            {(enrichment?.labels ?? []).map((l) => (
              <span
                key={l}
                className="inline-flex items-center rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-fg-3"
              >
                {l}
              </span>
            ))}
          </div>
          {enrichment?.description_excerpt && (
            <details className="group mt-2">
              <summary className="flex cursor-pointer select-none items-center gap-1 text-[11px] font-medium text-fg-2 hover:text-fg-0">
                <ChevronRight
                  className="h-3 w-3 transition-transform group-open:rotate-90"
                  strokeWidth={1.75}
                />
                Description
              </summary>
              <pre className="mt-1 max-h-[320px] overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-bg-0 p-2.5 text-[11.5px] text-fg-1">
                {enrichment.description_excerpt}
              </pre>
            </details>
          )}
          {(enrichment?.acceptance_criteria ?? []).length > 0 && (
            <div className="mt-2">
              <p className="text-[10.5px] font-mono uppercase tracking-wide text-fg-3">
                Acceptance criteria (extracted)
              </p>
              <ul className="mt-1 space-y-0.5 text-[12px] text-fg-1">
                {(enrichment?.acceptance_criteria ?? []).map((c, i) => (
                  <li key={i} className="flex gap-1.5">
                    <Check
                      className="mt-0.5 h-2.5 w-2.5 shrink-0 text-ok"
                      strokeWidth={2}
                    />
                    {c}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(enrichment?.confluence_links ?? []).length > 0 && (
            <div className="mt-2">
              <p className="text-[10.5px] font-mono uppercase tracking-wide text-fg-3">
                Linked Confluence
              </p>
              <ul className="mt-1 space-y-0.5 text-[11px]">
                {(enrichment?.confluence_links ?? []).map((url) => (
                  <li key={url}>
                    <a
                      href={url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 truncate text-accent hover:underline"
                    >
                      <Link2 className="h-2.5 w-2.5" strokeWidth={1.75} />
                      <span className="truncate font-mono">{url}</span>
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(enrichment?.related_keys ?? []).length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <p className="text-[10.5px] font-mono uppercase tracking-wide text-fg-3">
                Related
              </p>
              {(enrichment?.related_keys ?? []).map((k) => (
                <span
                  key={k}
                  className="rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-2"
                >
                  {k}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}


/* ── Verify ui ──────────────────────────────────────────────── */


function VerifyCountStrip({ items }: { items: VerifyItem[] }) {
  const by = items.reduce<Record<string, number>>((a, it) => {
    a[it.status] = (a[it.status] ?? 0) + 1;
    return a;
  }, {});
  return (
    <div className="flex items-center gap-1.5">
      {(["pass", "fail", "skipped", "pending"] as const).map((k) => {
        const n = by[k] ?? 0;
        if (n === 0 && k !== "pending") return null;
        const tone = {
          pass: "bg-ok-soft text-ok",
          fail: "bg-err-soft text-err",
          skipped: "bg-bg-2 text-fg-2",
          pending: "bg-bg-2 text-fg-3",
        }[k];
        return (
          <span
            key={k}
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-mono text-[10.5px]",
              tone,
            )}
          >
            <span className="tabular-nums font-semibold">{n}</span>
            {k}
          </span>
        );
      })}
    </div>
  );
}


function VerifyItemRow({
  item,
  onOverride,
}: {
  item: VerifyItem;
  onOverride: (itemId: string, verdict: "pass" | "fail" | "skipped") => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const notes = _parseVerifyNotes(item.user_notes);
  const dotClass =
    item.status === "pass"
      ? "bg-ok"
      : item.status === "fail"
        ? "bg-err"
        : item.status === "skipped"
          ? "bg-fg-3"
          : "bg-bg-2 border border-border";
  const hasRationale =
    !!(notes.rationale || (notes.evidence && notes.evidence.length));
  return (
    <li className="px-4 py-2">
      <div
        className="flex items-start gap-2.5"
        onClick={() => hasRationale && setExpanded((v) => !v)}
        style={{ cursor: hasRationale ? "pointer" : "default" }}
      >
        <span className={cn("mt-0.5 h-3 w-3 shrink-0 rounded-full", dotClass)} />
        <div className="min-w-0 flex-1">
          <p className="text-[12.5px] text-fg-1">{item.text}</p>
          {hasRationale && !expanded && notes.rationale && (
            <p className="mt-0.5 truncate text-[11px] text-fg-3">
              → {notes.rationale}
            </p>
          )}
          {expanded && hasRationale && (
            <div className="mt-1 space-y-1 rounded-md border border-border bg-bg-2/60 px-2 py-1 text-[11.5px]">
              {notes.rationale && (
                <p className="text-fg-1">
                  <span className="font-mono text-[10.5px] text-fg-3">
                    rationale
                  </span>
                  {" "}
                  {notes.rationale}
                </p>
              )}
              {notes.evidence?.length ? (
                <ul className="list-disc pl-4 text-[11px] text-fg-2">
                  {notes.evidence.map((e, i) => (
                    <li key={i} className="font-mono">{e}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-0.5">
          <OverrideBtn
            label="pass"
            icon={<Check className="h-2.5 w-2.5" strokeWidth={2.5} />}
            active={item.status === "pass"}
            tone="ok"
            onClick={() => onOverride(item.id, "pass")}
          />
          <OverrideBtn
            label="fail"
            icon={<XCircle className="h-2.5 w-2.5" strokeWidth={2} />}
            active={item.status === "fail"}
            tone="err"
            onClick={() => onOverride(item.id, "fail")}
          />
          <OverrideBtn
            label="skip"
            icon={<MinusCircle className="h-2.5 w-2.5" strokeWidth={2} />}
            active={item.status === "skipped"}
            tone="fg"
            onClick={() => onOverride(item.id, "skipped")}
          />
        </div>
      </div>
    </li>
  );
}


function OverrideBtn({
  label,
  icon,
  active,
  tone,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  active: boolean;
  tone: "ok" | "err" | "fg";
  onClick: () => void;
}) {
  const cls = {
    ok: active ? "bg-ok text-bg-0" : "text-fg-3 hover:text-ok hover:bg-ok-soft",
    err: active ? "bg-err text-bg-0" : "text-fg-3 hover:text-err hover:bg-err-soft",
    fg: active ? "bg-fg-2 text-bg-0" : "text-fg-3 hover:text-fg-1 hover:bg-bg-2",
  }[tone];
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={cn(
        "inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        cls,
      )}
      title={`Override to ${label}`}
    >
      {icon}
      {label}
    </button>
  );
}


function _parseVerifyNotes(raw: string | null): {
  rationale?: string;
  evidence?: string[];
} {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      return {
        rationale: parsed.rationale as string | undefined,
        evidence: Array.isArray(parsed.evidence)
          ? (parsed.evidence as string[])
          : undefined,
      };
    }
  } catch {
    // Raw string (pre-v7 row or user free-text)
    return { rationale: raw };
  }
  return { rationale: raw };
}


/* ── Plan count chip ────────────────────────────────────────── */


function PlanCountChip({
  label,
  n,
  tone = "fg",
  animate = false,
}: {
  label: string;
  n: number;
  tone?: "fg" | "ok" | "warn" | "err" | "accent";
  animate?: boolean;
}) {
  if (n === 0 && (tone === "fg" || label === "queued")) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-bg-2 px-1.5 py-0.5 font-mono text-[10.5px] text-fg-3">
        <span className="tabular-nums">0</span>
        {label}
      </span>
    );
  }
  const toneClass = {
    fg: "bg-bg-2 text-fg-2",
    ok: "bg-ok-soft text-ok",
    warn: "bg-warn-soft text-warn",
    err: "bg-err-soft text-err",
    accent: "bg-accent-soft text-accent",
  }[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-mono text-[10.5px]",
        toneClass,
        animate && n > 0 && "animate-pulse",
      )}
    >
      <span className="tabular-nums font-semibold">{n}</span>
      {label}
    </span>
  );
}


/* ── Step pill ──────────────────────────────────────────────── */


function StepPill({ step }: { step: string }) {
  const cfg: Record<string, string> = {
    discuss: "bg-accent-soft text-accent",
    plan: "bg-info-soft text-info",
    execute: "bg-warn-soft text-warn",
    verify: "bg-ok-soft text-ok",
    done: "bg-bg-2 text-fg-2",
  };
  return (
    <span
      className={cn(
        "rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        cfg[step] ?? "bg-bg-2 text-fg-2",
      )}
    >
      {step}
    </span>
  );
}
