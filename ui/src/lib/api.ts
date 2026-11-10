/**
 * Bifrost REST API client.
 * Base URL read from VITE_API_BASE_URL env var (default: /api/v1).
 */

import { csrfHeaders } from "./csrf";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export interface BifrostEnvelope<T> {
  data: T;
  meta?: Record<string, unknown>;
  errors?: Array<{ code: string; message: string }>;
}

// ── Repo types ──────────────────────────────────────────────

export interface RepoSummary {
  id: string;
  urn: string;
  name: string;
  url: string;
  domain_id: string | null;
  freshness_state: string;
  index_status: string;
  last_indexed_at: string | null;
}

export interface RepoFreshness {
  state: string;
  last_commit_indexed: string | null;
  last_indexed_at: string | null;
  pending_commits: number;
}

export interface RepoStats {
  component_count: number;
  flow_count: number;
  dependency_count: number;
}

export interface RepoDetail {
  id: string;
  urn: string;
  name: string;
  url: string;
  domain_id: string | null;
  freshness: RepoFreshness;
  stats: RepoStats;
}

// ── Entity types ────────────────────────────────────────────

export interface EntitySummary {
  id: string;
  urn: string;
  name: string;
  entity_type: string;
  confidence_level: string;
  freshness_state: string;
}

export interface EntitySearchResult {
  entity: EntitySummary;
  score: number;
}

export interface EntityDetail {
  id: string;
  urn: string;
  name: string;
  qualified_name: string | null;
  entity_type: string;
  knowledge_tier: string | null;
  metadata: Record<string, unknown> | null;
  confidence: Record<string, unknown> | null;
  confidence_level: string;
  freshness_state: string;
  source_basis: Record<string, unknown> | null;
  repo_id: string | null;
  domain_id: string | null;
  created_at: string;
  updated_at: string;
}

// ── Task types ──────────────────────────────────────────────

export interface TaskSummary {
  id: string;
  type: string;
  status: string;
  query: string;
  created_at: string;
  updated_at: string;
  result: Record<string, unknown> | null;
  error_message: string | null;
}

export interface TaskDetail extends TaskSummary {
  result: Record<string, unknown> | null;
}

// ── Query types ─────────────────────────────────────────────

export interface SourceCitation {
  urn: string;
  name: string;
  entity_type?: string | null;
  relevance?: number | null;
}

export interface QueryAnswer {
  answer: string;
  confidence: string;
  sources: SourceCitation[];
  gaps: string[];
  conflicts: Record<string, unknown>[];
  task_id: string;
}

// ── API functions ───────────────────────────────────────────

/**
 * Typed error thrown from `request()`. Carries the HTTP status + parsed
 * body so callers can handle specific failure modes — e.g. HTTP 409
 * "query_in_flight" responses from POST /messages, which the UI
 * should treat as "already listening on the existing stream" rather
 * than as a user-facing error.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...csrfHeaders(), ...init?.headers },
  });
  if (!res.ok) {
    const text = await res.text();
    let detail: unknown = text;
    try {
      detail = JSON.parse(text);
    } catch {
      /* plain-text body — leave as-is */
    }
    throw new ApiError(
      res.status,
      detail,
      `API ${res.status}: ${typeof detail === "string" ? detail : text}`,
    );
  }
  const envelope: BifrostEnvelope<T> = await res.json();
  return envelope.data;
}

// Repos
// ── Tasks (Action Layer) ──────────────────────────────────
export interface TaskSummary {
  id: string;
  title: string;
  state: string;
  created_from: "chat" | "jira" | "direct";
  jira_key: string | null;
  jira_url: string | null;
  clarifications_pending: number;
  phases_total: number;
  phases_done: number;
  total_cost_usd?: number;
  ask_back_count?: number;
  plans_total?: number;
  plans_completed?: number;
  plans_failed?: number;
  has_worktree?: boolean;
  has_pr?: boolean;
  pr_url?: string | null;
  pr_number?: number | null;
  created_at: string;
  updated_at: string;
}

export interface TaskStats {
  pr_merged?: number;
  active: number;
  success: number;
  blocked: number;
  cancelled: number;
  total_recent: number;
  cost_recent_usd: number;
  success_rate: number;
  avg_cost_per_task: number;
  window_days: number;
}

export const getTaskStats = (window_days = 7) =>
  request<TaskStats>(`/tasks/stats?window_days=${window_days}`);
export interface TaskEvent {
  actor: string;
  action: string;
  payload: Record<string, unknown>;
  at: string;
}
export interface JiraEnrichment {
  jira_key: string;
  title?: string | null;
  status?: string | null;
  priority?: string | null;
  issue_type?: string | null;
  url?: string | null;
  description_excerpt?: string | null;
  labels?: string[];
  components?: string[];
  acceptance_criteria?: string[];
  confluence_links?: string[];
  related_keys?: string[];
  source?: "cache" | "live" | "none" | string;
  fetched_at?: string;
}

export interface TaskDetail extends TaskSummary {
  intent_text: string;
  conversation_id: string | null;
  worktree_path: string | null;
  branch_name: string | null;
  pr_url?: string | null;
  pr_number?: number | null;
  pushed_at?: string | null;
  jira_enrichment?: JiraEnrichment | null;
  enriched_at?: string | null;
  acceptance_criteria: Array<string | Record<string, unknown>>;
  events: TaskEvent[];
}
export interface IntentClassification {
  intent: "ask" | "plan" | "execute" | "quick" | "clarify";
  confidence: number;
  reasoning: string;
  jira_key: string | null;
  clarifying_question: string | null;
}

export const classifyIntent = (message: string) =>
  request<IntentClassification>("/intent/classify", {
    method: "POST",
    body: JSON.stringify({ message }),
  });

export const createTask = (params: {
  title: string;
  intent_text: string;
  created_from?: "chat" | "jira" | "direct";
  conversation_id?: string | null;
  jira_key?: string | null;
}) =>
  request<TaskSummary>("/tasks", {
    method: "POST",
    body: JSON.stringify({
      created_from: "direct",
      ...params,
    }),
  });

export const listTasks = (
  state?: string,
  opts: {
    include_archived?: boolean;
    archived_only?: boolean;
    q?: string;
    limit?: number;
  } = {},
) => {
  const qs = new URLSearchParams();
  if (state) qs.set("state", state);
  if (opts.include_archived) qs.set("include_archived", "true");
  if (opts.archived_only) qs.set("archived_only", "true");
  if (opts.q && opts.q.trim()) qs.set("q", opts.q.trim());
  if (opts.limit) qs.set("limit", String(opts.limit));
  const q = qs.toString();
  return request<TaskSummary[]>(`/tasks${q ? `?${q}` : ""}`);
};

export const archiveTask = (task_id: string) =>
  request<{
    task_id: string;
    archived: boolean;
    already_archived: boolean;
    archived_at: string | null;
  }>(`/tasks/${task_id}/archive`, { method: "POST" });

export const unarchiveTask = (task_id: string) =>
  request<{
    task_id: string;
    archived: boolean;
    was_archived: boolean;
  }>(`/tasks/${task_id}/unarchive`, { method: "POST" });

export const autoArchiveTasks = (params: {
  older_than_days?: number;
  dry_run?: boolean;
} = {}) =>
  request<{
    dry_run: boolean;
    candidates: Array<{ id: string; state: string; updated_at: string }>;
    archived: number;
  }>(`/tasks/auto-archive`, {
    method: "POST",
    body: JSON.stringify({
      older_than_days: 14,
      dry_run: true,
      ...params,
    }),
  });

export const getTask = (id: string) =>
  request<TaskDetail>(`/tasks/${id}`);

export const advanceTask = (id: string, to_state: string, reason = "") =>
  request<TaskSummary>(`/tasks/${id}/advance`, {
    method: "POST",
    body: JSON.stringify({ to_state, reason }),
  });

export const cancelTask = (id: string) =>
  request<TaskSummary>(`/tasks/${id}/cancel`, { method: "POST" });

// ── Chat ↔ Task linking ────────────────────────────────────
export interface TaskChatLink {
  conversation_id: string;
  title: string;
  link_type: "spawned_task" | "spawned_chat" | "reference" | "correction";
  link_message_id: string | null;
  note: string | null;
  last_message_preview: string | null;
  message_count: number;
  linked_at: string;
}
export interface ConvTaskLink {
  task_id: string;
  title: string;
  state: string;
  link_type: string;
  link_message_id: string | null;
  linked_at: string;
  // SDLC lineage fields rendered inline in chat
  jira_key: string | null;
  jira_url: string | null;
  pr_url: string | null;
  pr_number: number | null;
}

export const linkConversationToTask = (
  task_id: string,
  params: {
    conversation_id: string;
    link_type?: "spawned_task" | "spawned_chat" | "reference" | "correction";
    link_message_id?: string | null;
    note?: string | null;
  },
) =>
  request<{ status: string }>(`/tasks/${task_id}/link`, {
    method: "POST",
    body: JSON.stringify({ link_type: "reference", ...params }),
  });

export const getTaskConversations = (task_id: string) =>
  request<TaskChatLink[]>(`/tasks/${task_id}/conversations`);

export const getConversationTasks = (conversation_id: string) =>
  request<ConvTaskLink[]>(`/conversations/${conversation_id}/tasks`);


// ── Conversation connections (chat right-rail panel) ─────────────


export interface ConnTask {
  task_id: string;
  title: string;
  state: string;
  jira_key: string | null;
  jira_url: string | null;
  pr_url: string | null;
  pr_number: number | null;
  has_pr: boolean;
  link_message_id: string | null;
  linked_at: string;
  updated_at: string;
  clarifications_pending: number;
  plans_total: number;
  plans_completed: number;
  plans_failed: number;
  total_cost_usd: number;
}

export interface ConnAsk {
  id: string;
  question: string;
  asker_email: string;
  target_squad: string | null;
  target_slack_channel: string | null;
  status: string;
  created_at: string;
  answered_at: string | null;
  escalated_at: string | null;
}

export interface ConnRelatedConv {
  id: string;
  title: string;
  updated_at: string;
  message_count: number;
}

export interface ConversationConnections {
  conversation_id: string;
  tasks: ConnTask[];
  asks: ConnAsk[];
  related: ConnRelatedConv[];
  counts: {
    tasks: number;
    asks: number;
    related: number;
    total: number;
  };
}

export const getConversationConnections = (conversation_id: string) =>
  request<ConversationConnections>(
    `/conversations/${conversation_id}/connections`,
  );

export const spawnTaskFromChat = (
  conversation_id: string,
  params: {
    message_id?: string | null;
    title?: string | null;
    override_intent?: string | null;
    project_key?: string | null;
    issue_type?: string;
    acceptance_criteria?: string[] | null;
    create_jira_now?: boolean;
  } = {},
) =>
  request<TaskSummary>(
    `/conversations/${conversation_id}/spawn-task`,
    {
      method: "POST",
      body: JSON.stringify({
        conversation_id,
        ...params,
      }),
    },
  );


// ── Spawn modal: preview + retract endpoints ───────────────────────


export interface SpawnPreview {
  suggested_title: string;
  suggested_intent: string;
  suggested_acceptance_criteria: string[];
  suggested_issue_type: string;
  resolved_project_key: string | null;
  resolved_project_source: string;
  resolved_project_reason: string;
  candidate_projects: string[];
  all_projects: string[];
}

export const previewSpawnTask = (
  conversation_id: string,
  params: { message_id?: string | null } = {},
) =>
  request<SpawnPreview>(
    `/conversations/${conversation_id}/spawn-task/preview`,
    {
      method: "POST",
      body: JSON.stringify({
        conversation_id,
        ...params,
      }),
    },
  );

export const retractJiraWrite = (
  task_id: string,
  audit_id: string,
  reason: string,
) =>
  request<{ ok: boolean; noop?: boolean }>(
    `/tasks/${task_id}/jira-retract`,
    {
      method: "POST",
      body: JSON.stringify({ audit_id, reason }),
    },
  );

/** One audit row from ``task_jira_audit`` — every Jira write Bifrost
 * has performed for a task. Retractable while ``retracted_at`` is
 * NULL.  */
export interface JiraAuditRow {
  id: string;
  action: "create" | "transition" | "comment" | "retract" | string;
  jira_key: string | null;
  jira_url: string | null;
  before_state: string | null;
  after_state: string | null;
  jira_state_before: string | null;
  jira_state_after: string | null;
  ok: boolean;
  error_message: string | null;
  actor: string;
  created_at: string;
  retracted_at: string | null;
  payload: Record<string, unknown>;
}

export const listTaskJiraAudit = (task_id: string) =>
  request<JiraAuditRow[]>(`/tasks/${task_id}/jira-audit`);

/** Adopt an existing Jira ticket as a Bifrost task. */
export const taskFromJira = (params: {
  jira_key: string;
  conversation_id?: string | null;
  link_message_id?: string | null;
}) =>
  request<TaskSummary>(`/tasks/from-jira`, {
    method: "POST",
    body: JSON.stringify(params),
  });


// ── Collaboration — 5 primitives ───────────────


/** Universal "Discuss this" — spawn a conversation pre-loaded with
 * context. Returns the new conversation id; FE navigates to
 * /query?conv=<id> immediately. */
export const seedConversation = (params: {
  title?: string | null;
  scope_repo_ids?: string[];
  scope_domain_id?: string | null;
  heading?: string | null;
  seed_context?: string | null;
  follow_up_question?: string | null;
}) =>
  request<{ id: string }>(`/conversations/seed`, {
    method: "POST",
    body: JSON.stringify(params),
  });


// P1 Squad Pulse
export interface PulseTaskRow {
  id: string;
  title: string;
  state: string;
  owner_email: string | null;
  jira_key: string | null;
  jira_url: string | null;
  pr_url: string | null;
  pr_number: number | null;
  updated_at: string;
}
export interface PulseChatRow {
  id: string;
  title: string;
  owner_email: string | null;
  last_message_at: string;
  cited_urn_count: number;
}
export interface PulseGDocRow {
  title: string;
  url: string;
  last_modified: string | null;
  repo: string | null;
}
export interface PulseJiraRow {
  key: string;
  title: string;
  url: string;
  status: string | null;
  repo: string | null;
}
export interface PulseContributorRow {
  email: string;
  activity_count: number;
}
export interface PulseReport {
  scope: { kind: string; key: string; label: string };
  range_days: number;
  generated_at: string;
  tasks_shipped_count: number;
  tasks_in_flight_count: number;
  tasks_blocked_count: number;
  prs_merged_count: number;
  prs_open_count: number;
  gdocs_added_count: number;
  jira_closed_count: number;
  chats_count: number;
  tasks_shipped: PulseTaskRow[];
  tasks_in_flight: PulseTaskRow[];
  tasks_blocked: PulseTaskRow[];
  prs_merged: unknown[];
  prs_open: unknown[];
  gdocs_added: PulseGDocRow[];
  jira_closed: PulseJiraRow[];
  recent_chats: PulseChatRow[];
  top_contributors: PulseContributorRow[];
}

export const getPulse = (params: {
  squad?: string;
  domain_id?: string;
  repo_id?: string;
  range_days?: number;
}) => {
  const q = new URLSearchParams();
  if (params.squad) q.set("squad", params.squad);
  if (params.domain_id) q.set("domain_id", params.domain_id);
  if (params.repo_id) q.set("repo_id", params.repo_id);
  if (params.range_days) q.set("range_days", String(params.range_days));
  return request<PulseReport>(`/collaboration/pulse?${q}`);
};

export const getPulseSectionMarkdown = (params: {
  squad?: string;
  domain_id?: string;
  repo_id?: string;
  range_days?: number;
  section: string;
}) => {
  const q = new URLSearchParams({ section: params.section });
  if (params.squad) q.set("squad", params.squad);
  if (params.domain_id) q.set("domain_id", params.domain_id);
  if (params.repo_id) q.set("repo_id", params.repo_id);
  if (params.range_days) q.set("range_days", String(params.range_days));
  return request<{ markdown: string }>(
    `/collaboration/pulse/section-markdown?${q}`,
  );
};


// P2 Asks
export interface AskRow {
  id: string;
  asker_email: string;
  question: string;
  scope_repo_ids: string[];
  target_squad: string | null;
  target_slack_channel: string | null;
  target_jira_project: string | null;
  slack_draft_md: string | null;
  slack_thread_url: string | null;
  answer_conversation_id: string | null;
  status:
    | "drafted" | "routed" | "sent" | "answered"
    | "escalated" | "cancelled";
  created_at: string;
  updated_at: string;
  answered_at: string | null;
  escalated_at: string | null;
}

export const createAsk = (params: {
  question: string;
  scope_repo_ids?: string[];
  target_squad_override?: string | null;
}) =>
  request<AskRow>(`/collaboration/asks`, {
    method: "POST",
    body: JSON.stringify(params),
  });

export const listAsks = (params: {
  asker_email?: string;
  target_squad?: string;
  status?: string;
  limit?: number;
} = {}) => {
  const q = new URLSearchParams();
  if (params.asker_email) q.set("asker_email", params.asker_email);
  if (params.target_squad) q.set("target_squad", params.target_squad);
  if (params.status) q.set("status", params.status);
  if (params.limit) q.set("limit", String(params.limit));
  return request<AskRow[]>(`/collaboration/asks?${q}`);
};

export const askMarkSent = (id: string, slack_thread_url: string | null) =>
  request<AskRow>(`/collaboration/asks/${id}/mark-sent`, {
    method: "POST",
    body: JSON.stringify({ slack_thread_url }),
  });

export const askMarkAnswered = (id: string, answer_conversation_id?: string) =>
  request<AskRow>(`/collaboration/asks/${id}/mark-answered`, {
    method: "POST",
    body: JSON.stringify({
      answer_conversation_id: answer_conversation_id ?? null,
    }),
  });

export const askEscalate = (id: string) =>
  request<AskRow>(`/collaboration/asks/${id}/escalate`, { method: "POST" });

export const askCancel = (id: string) =>
  request<AskRow>(`/collaboration/asks/${id}/cancel`, { method: "POST" });


// P3 Code Review Queue
export interface ReviewItem {
  pr_url: string;
  pr_number: number | null;
  title: string;
  state: string;
  author: string | null;
  repo_name: string | null;
  rationale: string;
  source: string;
  task_id: string | null;
  task_title: string | null;
  task_state: string | null;
  last_seen_at: string;
}
export interface ReviewQueueResp {
  email: string;
  items: ReviewItem[];
  generated_at: string;
  note: string;
}

export const getReviewQueue = (params: { email?: string; range_days?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.email) q.set("email", params.email);
  if (params.range_days) q.set("range_days", String(params.range_days));
  return request<ReviewQueueResp>(`/collaboration/review-queue?${q}`);
};


// P4 Standup
export interface StandupDraftResp {
  email: string;
  range_days: number;
  generated_at: string;
  markdown: string;
  done: Array<Record<string, unknown>>;
  in_flight: Array<Record<string, unknown>>;
  blocked: Array<Record<string, unknown>>;
  open_asks: Array<Record<string, unknown>>;
}

export const getStandupDraft = (params: { email?: string; range_days?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.email) q.set("email", params.email);
  if (params.range_days) q.set("range_days", String(params.range_days));
  return request<StandupDraftResp>(`/collaboration/standup-draft?${q}`);
};


// P5 Domain Hub
export interface HubService {
  repo_id: string;
  name: string;
  description: string | null;
  component_count: number;
  flow_count: number;
}
export interface HubGDoc {
  title: string;
  url: string;
  last_modified: string | null;
  repo: string | null;
}
export interface HubEpic {
  key: string;
  title: string;
  url: string;
  status: string | null;
  repo: string | null;
}
export interface DomainHubResp {
  domain: string;
  label: string;
  slack_channels: string[];
  jira_projects: string[];
  services: HubService[];
  top_contributors: Array<{ email: string; activity_count: number }>;
  recent_design_docs: HubGDoc[];
  active_epics: HubEpic[];
  generated_at: string;
}

export const getDomainHub = (squad: string, range_days: number = 30) =>
  request<DomainHubResp>(
    `/collaboration/domain-hub?squad=${encodeURIComponent(squad)}&range_days=${range_days}`,
  );


// Personal Inbox — drives the Overview hero panel + sidebar badges
// + Cmd+K palette's recents. Single source of truth for "what's mine
// right now" across the product.

export interface InboxTask {
  id: string;
  title: string;
  state: string;
  jira_key: string | null;
  jira_url: string | null;
  pr_url: string | null;
  pr_number: number | null;
  updated_at: string;
}
export interface InboxAsk {
  id: string;
  question: string;
  asker_email: string;
  target_squad: string | null;
  target_slack_channel: string | null;
  status: string;
  created_at: string;
}
export interface InboxReviewPR {
  pr_url: string;
  pr_number: number | null;
  title: string;
  task_id: string | null;
  task_title: string | null;
  rationale: string;
}
export interface InboxCounts {
  escalated: number;
  blocked: number;
  awaiting_me: number;
  in_review: number;
  open_work: number;
  total_actionable: number;
}
export interface InboxReport {
  email: string;
  generated_at: string;
  counts: InboxCounts;
  escalated_asks: InboxAsk[];
  blocked_tasks: InboxTask[];
  awaiting_my_squad: InboxAsk[];
  my_prs_in_review: InboxReviewPR[];
  open_work: InboxTask[];
}

export const getInbox = (email?: string) => {
  const q = new URLSearchParams();
  if (email) q.set("email", email);
  return request<InboxReport>(
    `/collaboration/inbox${q.toString() ? "?" + q : ""}`,
  );
};

export const promoteMessageToTask = (
  task_id: string,
  message_id: string,
  note?: string,
) =>
  request<{ clarification_id: string }>(
    `/tasks/${task_id}/promote-message`,
    {
      method: "POST",
      body: JSON.stringify({
        task_id,
        message_id,
        note: note ?? null,
      }),
    },
  );

// ── Layer 2 — ask-back + budget + gap detection ─────────────
export interface BudgetSnapshot {
  budget: { ask_backs_max: number; cost_max_usd: number; extractions_max: number };
  usage: { ask_backs_used: number; cost_used_usd: number; extractions_used: number };
  remaining: { ask_backs: number; cost_usd: number; extractions: number };
}

export interface GapAssessment {
  verdict: "adequate" | "partial" | "insufficient";
  coverage_score: number;
  min_distance: number | null;
  strong_match_count: number;
  weak_match_count: number;
  scope_entity_count: number;
  entity_matches: Array<{
    id: string;
    name: string;
    entity_type: string;
    distance: number;
    scope_repo_id: string | null;
  }>;
  keywords_used: string[];
  suggested_repos_to_extract: Array<{
    owner: string;
    name: string;
    url: string;
    description: string | null;
    already_registered: boolean;
    reason: string;
  }>;
}

export interface Clarification {
  id: string;
  phase_id: string | null;
  source: "agent" | "user" | "system";
  kind: "need_research" | "need_user_input" | "conflict" | "ambiguity";
  question: string;
  options: Array<{ value: string; label: string }>;
  blocks_state: string | null;
  state: "open" | "answered" | "skipped" | "superseded";
  answer: string | null;
  answered_by: string | null;
  answered_at: string | null;
  created_at: string;
}

export interface AskBackRow {
  id: string;
  phase_id: string | null;
  agent_kind: string;
  question: string;
  scope: Record<string, unknown>;
  answered_from: string;
  triggered_extraction_id: string | null;
  research_tokens: number;
  research_cost: number;
  duration_ms: number;
  answer_summary: string | null;
  answer_confidence: number | null;
  created_at: string;
}

export const getBudget = (task_id: string) =>
  request<BudgetSnapshot>(`/tasks/${task_id}/budget`);

export const listClarifications = (task_id: string, state: "open" | "all" = "open") =>
  request<Clarification[]>(`/tasks/${task_id}/clarifications?state=${state}`);

export const answerClarification = (
  task_id: string,
  clarification_id: string,
  answer: string,
  answered_by: "user" | "bifrost_research" | "bifrost_extraction" | "system" = "user",
) =>
  request<{ id: string; state: string; answered_at: string }>(
    `/tasks/${task_id}/clarifications/${clarification_id}/answer`,
    { method: "POST", body: JSON.stringify({ answer, answered_by }) },
  );

export const listAskBacks = (task_id: string) =>
  request<AskBackRow[]>(`/tasks/${task_id}/ask-backs`);

// ── Layer 3 — discussion agent + phases + artifacts ─────────
export interface Phase {
  id: string;
  order_index: number;
  slug: string;
  title: string;
  state: string;
  current_step: "discuss" | "plan" | "execute" | "verify" | "done";
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  id: string;
  phase_id: string;
  kind: "context" | "research" | "plan" | "summary" | "verify";
  version: number;
  state: "draft" | "approved" | "stale" | "invalidated";
  body_md: string;
  parsed: Record<string, unknown>;
  produced_by_agent: string | null;
  created_at: string;
  approved_at: string | null;
  phase_title: string;
}

export interface Assumption {
  id: string;
  key: string;
  value: string;
  source_type: string;
  confidence: number;
  tier: number;
  provenance: Record<string, unknown>;
  state: "active" | "retracted" | "conflicting";
  retracted_at: string | null;
  created_at: string;
}

export interface DiscussionResult {
  task_id: string;
  phase_id: string;
  artifact_id: string;
  artifact_version: number;
  ready_to_plan: boolean;
  context_md: string;
  assumptions_written: number;
  assumptions_retracted: number;
  clarifications_opened: number;
  duration_ms: number;
  cost_usd: number;
  ask_back_id: string | null;
}

export const runDiscussion = (
  task_id: string,
  params: { user_message?: string | null; phase_id?: string | null } = {},
) =>
  request<DiscussionResult>(`/tasks/${task_id}/discuss`, {
    method: "POST",
    body: JSON.stringify(params),
  });

export const listPhases = (task_id: string) =>
  request<Phase[]>(`/tasks/${task_id}/phases`);

export const listArtifacts = (
  task_id: string,
  params: { kind?: string; phase_id?: string; latest_only?: boolean } = {},
) => {
  const qs = new URLSearchParams();
  if (params.kind) qs.set("kind", params.kind);
  if (params.phase_id) qs.set("phase_id", params.phase_id);
  if (params.latest_only === false) qs.set("latest_only", "false");
  const q = qs.toString();
  return request<Artifact[]>(`/tasks/${task_id}/artifacts${q ? `?${q}` : ""}`);
};

export const listAssumptions = (
  task_id: string,
  artifact_id: string,
  state: "active" | "all" = "active",
) =>
  request<Assumption[]>(
    `/tasks/${task_id}/artifacts/${artifact_id}/assumptions?state=${state}`,
  );

export interface AssumptionVersion {
  artifact_version: number;
  value: string;
  source_type: string;
  confidence: number;
  tier: number;
  state: "active" | "retracted" | "conflicting";
  retracted_by_artifact_id: string | null;
  created_at: string;
}

export interface AssumptionHistory {
  phase_id: string;
  versions: Array<{
    artifact_id: string;
    kind: string;
    version: number;
    state: string;
    created_at: string;
    approved_at: string | null;
  }>;
  keys: Record<string, AssumptionVersion[]>;
}

export const getAssumptionHistory = (task_id: string, phase_id: string) =>
  request<AssumptionHistory>(
    `/tasks/${task_id}/phases/${phase_id}/assumption-history`,
  );

// ── Layer 5 — plan agent + approval ─────────────────────────

export interface PlanRow {
  id: string;
  phase_id: string;
  artifact_id: string | null;
  order_index: number;
  plan_key: string;
  name: string;
  files: string[];
  urns: string[];
  action_body: string;
  verify_cmd: string | null;
  done_criterion: string;
  confidence_basis: string[];
  wave: number;
  depends_on: string[];
  assumption_ids: string[];
  status: "pending" | "approved" | "executing" | "completed" | "failed" | "rejected";
  git_sha: string | null;
  commit_msg: string | null;
  output_log: string | null;
  executed_at: string | null;
  created_at: string;
}

export interface VerifyItem {
  id: string;
  phase_id: string;
  plan_id: string | null;
  order_index: number;
  text: string;
  status: "pending" | "pass" | "fail" | "skipped";
  user_notes: string | null;
  answered_at: string | null;
}

export interface PlanResult {
  task_id: string;
  phase_id: string;
  artifact_id: string;
  artifact_version: number;
  ready_to_execute: boolean;
  plan_md: string;
  tasks_written: number;
  verify_items_written: number;
  assumption_links: number;
  skipped_unknown_keys: string[];
  duration_ms: number;
  cost_usd: number;
  ask_back_id: string | null;
}

export const runPlan = (
  task_id: string,
  params: { phase_id?: string | null; user_message?: string | null } = {},
) =>
  request<PlanResult>(`/tasks/${task_id}/plan`, {
    method: "POST",
    body: JSON.stringify(params),
  });

export const listPlans = (
  task_id: string,
  params: { phase_id?: string; artifact_id?: string; status?: string } = {},
) => {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v) qs.set(k, String(v));
  });
  const q = qs.toString();
  return request<PlanRow[]>(`/tasks/${task_id}/plans${q ? `?${q}` : ""}`);
};

export const listVerifyItems = (
  task_id: string,
  params: { phase_id?: string } = {},
) => {
  const qs = new URLSearchParams();
  if (params.phase_id) qs.set("phase_id", params.phase_id);
  const q = qs.toString();
  return request<VerifyItem[]>(
    `/tasks/${task_id}/verify-items${q ? `?${q}` : ""}`,
  );
};

export const approveArtifact = (task_id: string, artifact_id: string) =>
  request<{
    artifact_id: string;
    kind: "context" | "plan";
    approved_at?: string;
    already_approved?: boolean;
  }>(`/tasks/${task_id}/artifacts/${artifact_id}/approve`, { method: "POST" });

// ── Layer 6 — worktree + executor ───────────────────────────

export interface PlanOutcome {
  plan_id: string;
  plan_key: string;
  status: string;
  git_sha: string | null;
  commit_msg: string | null;
  files_changed: string[];
  tool_calls: number;
  agent_cost_usd: number;
  duration_ms: number;
  verify_passed: boolean | null;
  output_log: string;
}

export interface ExecutionRunResult {
  task_id: string;
  worktree_path: string;
  branch: string;
  plans_executed: number;
  plans_completed: number;
  plans_failed: number;
  plans_skipped: number;
  total_cost_usd: number;
  outcomes: PlanOutcome[];
  stopped_at_plan: string | null;
  final_task_state: string;
}

export interface ExecutionStatus {
  task_state: string;
  worktree_path: string | null;
  branch_name: string | null;
  counts: {
    pending: number;
    approved: number;
    executing: number;
    completed: number;
    failed: number;
    rejected: number;
  };
}

export const executeTask = (
  task_id: string,
  params: { target_repo_path: string; resume?: boolean },
) =>
  request<ExecutionRunResult>(`/tasks/${task_id}/execute`, {
    method: "POST",
    body: JSON.stringify({ resume: true, ...params }),
  });

export const retryPlan = (task_id: string, plan_id: string) =>
  request<PlanOutcome>(`/tasks/${task_id}/plans/${plan_id}/retry`, {
    method: "POST",
    body: JSON.stringify({ reset_status: true }),
  });

export const getExecutionStatus = (task_id: string) =>
  request<ExecutionStatus>(`/tasks/${task_id}/execution-status`);

export const removeWorktree = (task_id: string, force = false) =>
  request<{ removed: boolean; path?: string; branch?: string; reason?: string }>(
    `/tasks/${task_id}/worktree/remove?force=${force}`,
    { method: "POST" },
  );

// ── Layer 7 — verifier ──────────────────────────────────────

export interface VerifyRunResult {
  task_id: string;
  graded: number;
  passed: number;
  failed: number;
  skipped: number;
  overall_assessment: string;
  ready_for_pr: boolean;
  cost_usd: number;
  duration_ms: number;
  final_task_state: string;
  items: Array<{
    id: string;
    text: string;
    verdict: "pass" | "fail" | "skipped" | "pending";
    rationale?: string;
    evidence?: string[];
  }>;
}

export const runVerification = (task_id: string) =>
  request<VerifyRunResult>(`/tasks/${task_id}/verify`, {
    method: "POST",
    body: "{}",
  });

export const overrideVerifyItem = (
  task_id: string,
  item_id: string,
  verdict: "pass" | "fail" | "skipped",
  notes?: string,
) =>
  request<{ item_id: string; verdict: string; task_state: string }>(
    `/tasks/${task_id}/verify-items/${item_id}/override`,
    {
      method: "POST",
      body: JSON.stringify({ verdict, notes }),
    },
  );

// ── Layer 8 — PR + debug ────────────────────────────────────

export interface PRResult {
  task_id: string;
  branch: string;
  base: string;
  title: string;
  body: string;
  pushed: boolean;
  pushed_at: string | null;
  pr_url: string | null;
  pr_number: number | null;
  dry_run: boolean;
  commands_run: string[];
  notes: string[];
}

export interface DebugReport {
  task_id: string;
  plan_id: string;
  plan_key: string;
  diagnosis: string;
  suggested_fix: string;
  retry_prompt: string;
  retry_safe: boolean;
  duration_ms: number;
  cost_usd: number;
  tool_calls: number;
  transcript: string[];
}

export const createPR = (
  task_id: string,
  params: {
    base_branch?: string;
    draft?: boolean;
    use_gh?: boolean;
    dry_run?: boolean;
  } = {},
) =>
  request<PRResult>(`/tasks/${task_id}/create-pr`, {
    method: "POST",
    body: JSON.stringify({
      base_branch: "main",
      draft: false,
      use_gh: true,
      dry_run: false,
      ...params,
    }),
  });

export const debugPlan = (task_id: string, plan_id: string) =>
  request<DebugReport>(`/tasks/${task_id}/plans/${plan_id}/debug`, {
    method: "POST",
    body: "{}",
  });

// ── Layer 9 — /quick one-shot ───────────────────────────────

export interface QuickRunResult {
  task_id: string;
  title: string;
  final_state: string;
  plan_key: string;
  plan_status: string;
  git_sha: string | null;
  verify_passed: number;
  verify_failed: number;
  verify_skipped: number;
  ready_for_pr: boolean;
  total_cost_usd: number;
  duration_ms: number;
  plan_md: string;
  context_md: string;
  fallback_to_full: boolean;
  notes: string[];
}

export const runQuick = (params: {
  message: string;
  target_repo_path: string;
  title?: string | null;
  jira_key?: string | null;
  skip_verify?: boolean;
}) =>
  request<QuickRunResult>("/quick", {
    method: "POST",
    body: JSON.stringify(params),
  });

// ── Layer 10 polish — Jira enrichment ───────────────────────

export const enrichTaskFromJira = (
  task_id: string,
  force_refresh = false,
) =>
  request<{
    task_id: string;
    jira_key: string | null;
    enriched: boolean;
    enrichment: JiraEnrichment | null;
    acceptance_criteria_added: number;
    note: string;
  }>(`/tasks/${task_id}/enrich-from-jira`, {
    method: "POST",
    body: JSON.stringify({ force_refresh }),
  });


// ── Layer 4 — learning loop (admin) ─────────────────────────

export interface LearningStats {
  deltas: {
    total: number;
    auto_downgrades: number;
    auto_upgrades: number;
    pending_upgrades: number;
    noise: number;
    last_7_days: number;
    targets_touched: number;
    tasks_involved: number;
  };
  judgements: {
    total: number;
    pending: number;
    confirmed: number;
    contradicted: number;
  };
  config: { upgrade_confirmations_needed: number; noise_floor: number };
}

export interface ConfidenceDelta {
  id: string;
  target_kind: "entity" | "relationship";
  target_id: string;
  old_confidence: number | null;
  new_confidence: number;
  old_level: string | null;
  new_level: string;
  old_tier: number | null;
  new_tier: number;
  reason: string;
  task_id: string | null;
  assumption_id: string | null;
  plan_id: string | null;
  applied_at: string;
  applied_by: string;
}

export interface PendingUpgrade {
  target_kind: "entity" | "relationship";
  target_id: string;
  distinct_tasks: number;
  confirmations_needed: number;
  max_proposed_confidence: number | null;
  current_confidence: number | null;
  first_seen: string | null;
  last_seen: string | null;
}

export interface AgentJudgement {
  id: string;
  task_id: string;
  phase_id: string | null;
  agent_kind: string;
  run_id: string | null;
  claim: string;
  cited_tiers: number[];
  cited_urns: string[];
  outcome: string;
  outcome_at: string | null;
  correction_text: string | null;
  created_at: string;
}

export const getLearningStats = () =>
  request<LearningStats>("/admin/learning/stats");

// ── Knowledge cache effectiveness (semantic agent_results cache) ────

export interface CacheStatsByKind {
  hits: number;
  misses: number;
  hit_rate: number;
  spent_usd: number;
}

export interface CacheStatsTopTarget {
  agent_kind: string;
  target_id: string;
  hits: number;
}

export interface CacheStats {
  window_hours: number;
  lifetime: {
    hits: number;
    misses: number;
    hit_rate: number;
    distinct_targets: number;
    distinct_conversations: number;
    agent_spent_usd: number;
    runs: number;
    runs_total_cost_usd: number;
    runs_saved_cost_usd: number;
  };
  totals: {
    hits: number;
    misses: number;
    hit_rate: number;
    saved_cost_usd: number;
    total_cost_usd: number;
    runs: number;
  };
  by_kind: Record<string, CacheStatsByKind>;
  top_targets: CacheStatsTopTarget[];
}

export const getCacheStats = (windowHours: number = 24) =>
  request<CacheStats>(
    `/admin/learning/cache-stats?window_hours=${windowHours}`,
  );

// ── Agent quality dashboard ─────────────────────────────────

export interface AgentQualitySnapshot {
  window_days: number;
  trend_window_days: number;
  verify_distribution: {
    total: number;
    total_recent: number;
    by_status: Array<{
      status: string;
      n: number;
      recent: number;
      pct: number;
      recent_pct: number;
    }>;
  };
  plan_outcomes: {
    total: number;
    by_status: Array<{ status: string; n: number; pct: number }>;
  };
  agent_metrics: Array<{
    agent_kind: string;
    n: number;
    cost_usd: number;
    tokens: number;
    avg_duration_ms: number;
    avg_confidence: number;
    budget_hits: number;
  }>;
  assumption_tiers: {
    total_active: number;
    by_tier: Array<{ tier: number; n: number; pct: number }>;
    by_tier_source: Array<{ tier: number; source_type: string; n: number }>;
  };
  plan_readiness: {
    ready: { n_phases: number; all_completed: number; any_failed: number };
    not_ready: { n_phases: number; all_completed: number; any_failed: number };
    optimism_score: number | null;
  };
  quick_mode: {
    invocations: number;
    successes: number;
    blocked: number;
    cancelled: number;
    success_rate: number;
  };
  tasks_by_state: Record<string, number>;
}

export const getAgentQuality = (params: {
  window_days?: number;
  trend_window_days?: number;
} = {}) => {
  const qs = new URLSearchParams();
  if (params.window_days) qs.set("window_days", String(params.window_days));
  if (params.trend_window_days)
    qs.set("trend_window_days", String(params.trend_window_days));
  const q = qs.toString();
  return request<AgentQualitySnapshot>(
    `/admin/agent-quality${q ? `?${q}` : ""}`,
  );
};

export const listLearningDeltas = (params: {
  task_id?: string;
  target_kind?: "entity" | "relationship";
  target_id?: string;
  limit?: number;
} = {}) => {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  });
  const q = qs.toString();
  return request<ConfidenceDelta[]>(
    `/admin/learning/deltas${q ? `?${q}` : ""}`,
  );
};

export const listPendingUpgrades = (limit = 50) =>
  request<PendingUpgrade[]>(
    `/admin/learning/pending-upgrades?limit=${limit}`,
  );

export const listLearningJudgements = (params: {
  task_id?: string;
  outcome?: string;
  limit?: number;
} = {}) => {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  });
  const q = qs.toString();
  return request<AgentJudgement[]>(
    `/admin/learning/judgements${q ? `?${q}` : ""}`,
  );
};

export const confirmTarget = (params: {
  target_kind: "entity" | "relationship";
  target_id: string;
  new_confidence: number;
  task_id?: string | null;
  reason?: string;
  actor?: string;
}) =>
  request<{
    delta_id: string;
    applied_new_confidence: number | null;
    direction: "up" | "down" | "noop";
    applied_by: string;
    promoted_delta_ids: string[];
  }>("/admin/learning/confirm", {
    method: "POST",
    body: JSON.stringify({
      reason: "audit_confirmed",
      actor: "user",
      ...params,
    }),
  });

export const contradictTarget = (params: {
  target_kind: "entity" | "relationship";
  target_id: string;
  new_confidence: number;
  task_id?: string | null;
  reason?: string;
  actor?: string;
}) =>
  request<{
    delta_id: string;
    applied_new_confidence: number | null;
    direction: "up" | "down" | "noop";
    applied_by: string;
  }>("/admin/learning/contradict", {
    method: "POST",
    body: JSON.stringify({
      reason: "audit_contradicted",
      actor: "user",
      ...params,
    }),
  });


export const listRepos = () => request<RepoSummary[]>("/repos");
export const getRepo = (id: string) => request<RepoDetail>(`/repos/${id}`);
export const triggerExtraction = (repoId: string, forceFull = false) =>
  request<{ task_id: string }>(`/repos/${repoId}/extract`, {
    method: "POST",
    body: JSON.stringify({ force_full: forceFull }),
  });

/**
 * Rich extraction trigger: force_full OR invalidate_phases (re-run only
 * the named phases + their downstream dependents). Omit both for a pure
 * "Resume" — the worker picks up from the checkpoint and skips done phases.
 */
export const triggerExtractionRich = (
  repoId: string,
  opts: {
    force_full?: boolean;
    invalidate_phases?: string[];
    max_files?: number;
  },
) =>
  request<{ task_id: string }>(`/repos/${repoId}/extract`, {
    method: "POST",
    body: JSON.stringify({
      force_full: opts.force_full ?? false,
      invalidate_phases: opts.invalidate_phases ?? null,
      max_files: opts.max_files ?? null,
    }),
  });

export interface RepoCheckpointPhase {
  phase: string;
  label: string;
  done: boolean;
  units_done: number;
}

export interface RepoCheckpointStatus {
  repo_id: string;
  completed_phases: string[];
  resume_phase: string | null;
  last_updated: string | null;
  phases: RepoCheckpointPhase[];
}

export const getRepoCheckpoint = (repoId: string) =>
  request<RepoCheckpointStatus>(`/repos/${repoId}/checkpoint`);
export const registerRepo = (params: {
  name: string;
  url: string;
  domain_id?: string | null;
  description?: string;
}) =>
  request<{ id: string; urn: string; status: string }>(`/repos`, {
    method: "POST",
    body: JSON.stringify({
      name: params.name,
      url: params.url,
      domain_id: params.domain_id || null,
      description: params.description,
    }),
  });
export const deregisterRepo = (id: string) =>
  request<{ id: string; status: string }>(`/repos/${id}`, {
    method: "DELETE",
  });

export const syncRepoOwnership = (id: string) =>
  request<{
    status: string;
    squad: string | null;
    downstream_services_count: number;
    error: string | null;
  }>(`/repos/${id}/sync-ownership`, { method: "POST" });

// ── Knowledge tab ──────────────────────────────────────────
export interface KnowledgeRepo {
  id: string;
  name: string;
  url: string;
  domain_id: string | null;
  domain_name: string | null;
  freshness_state: string;
  index_status: string;
  last_indexed_at: string | null;
  last_commit_indexed: string | null;
  component_count: number;
  flow_count: number;
  dependency_count: number;
  squad: string | null;
  service_tier: string | null;
  region: string | null;
  jira_project_key: string | null;
  confluence_space_key: string | null;
  slack_channel: string | null;
  compliance_tags: string[];
  downstream_services_count: number;
  latest_task_id: string | null;
  latest_task_status: string | null;
  latest_task_updated_at: string | null;
  summary: string | null;
  queries_last_7d: number;
  downstream_service_names: string[];
}

export interface KnowledgeOverview {
  total: number;
  fresh: number;
  stale: number;
  not_indexed: number;
  by_domain: Record<string, number>;
  repos: KnowledgeRepo[];
}

export const getKnowledgeOverview = () =>
  request<KnowledgeOverview>("/knowledge/repos");

// Domains
export interface DomainSummary {
  id: string;
  urn: string;
  name: string;
  description: string | null;
}
export const listDomains = () => request<DomainSummary[]>("/domains");

export interface DomainRepoEntry {
  id: string;
  name: string;
  url: string;
  freshness_state: string;
  last_indexed_at: string | null;
  entity_counts: Record<string, number>;
  latest_task_id: string | null;
  latest_task_status: string | null;
}

export interface DomainDetail {
  id: string;
  name: string;
  description: string | null;
  repos: DomainRepoEntry[];
  cross_repo_flow_count: number;
  total_entities: number;
  fresh_count: number;
  stale_count: number;
}

export const getDomainDetail = (id: string) =>
  request<DomainDetail>(`/domains/${id}`);

export interface TeamRepoEntry {
  full_name: string;
  action: "added" | "linked" | "already_linked" | "error";
  repo_id: string | null;
  git_url: string;
  description: string | null;
  task_id: string | null;
  error: string | null;
}

export interface TeamRepoDiscoveryResult {
  team: string;
  dry_run: boolean;
  candidates_found: number;
  providers: string[];
  added: TeamRepoEntry[];
  linked: TeamRepoEntry[];
  already_linked: TeamRepoEntry[];
  errors: TeamRepoEntry[];
}

export const discoverDomainRepos = (
  id: string,
  opts: { team?: string; dry_run?: boolean; trigger_extraction?: boolean } = {},
) =>
  request<TeamRepoDiscoveryResult>(`/domains/${id}/discover-repos`, {
    method: "POST",
    body: JSON.stringify({
      team: opts.team ?? null,
      dry_run: opts.dry_run ?? false,
      trigger_extraction: opts.trigger_extraction ?? false,
    }),
  });

// Entities
export const listEntities = (params?: {
  entity_type?: string;
  repo_id?: string;
  domain_id?: string;
  limit?: number;
  offset?: number;
}) => {
  const sp = new URLSearchParams();
  if (params?.entity_type) sp.set("entity_type", params.entity_type);
  if (params?.repo_id) sp.set("repo_id", params.repo_id);
  if (params?.domain_id) sp.set("domain_id", params.domain_id);
  if (params?.limit) sp.set("limit", String(params.limit));
  if (params?.offset) sp.set("offset", String(params.offset));
  const qs = sp.toString();
  return request<EntitySummary[]>(`/entities${qs ? `?${qs}` : ""}`);
};

export const searchEntities = (q: string, entityType?: string, limit = 10) => {
  const sp = new URLSearchParams({ q, limit: String(limit) });
  if (entityType) sp.set("entity_type", entityType);
  return request<EntitySearchResult[]>(`/entities/search?${sp}`);
};

export const getEntity = (urn: string) =>
  request<EntityDetail>(`/entities/${encodeURIComponent(urn)}`);

// Extraction tasks (the background extraction job lifecycle —
// distinct from user-facing Tasks which are at /tasks above).
export const listExtractionTasks = (status?: string, limit = 50) => {
  const sp = new URLSearchParams({ limit: String(limit) });
  if (status) sp.set("status", status);
  return request<TaskSummary[]>(`/extraction-tasks?${sp}`);
};

export const getExtractionTask = (id: string) =>
  request<TaskDetail>(`/extraction-tasks/${id}`);

export const pauseExtractionTask = (id: string) =>
  request<{ id: string; status: string }>(`/extraction-tasks/${id}/pause`, { method: "POST" });

export const resumeExtractionTask = (id: string) =>
  request<{ id: string; status: string }>(`/extraction-tasks/${id}/resume`, { method: "POST" });

export const cancelExtractionTask = (id: string) =>
  request<{ id: string; status: string }>(`/extraction-tasks/${id}`, { method: "DELETE" });

// Query
export const submitQuery = (
  question: string,
  repoIds?: string[],
  domainId?: string,
) =>
  request<QueryAnswer>("/query", {
    method: "POST",
    body: JSON.stringify({
      question,
      repo_ids: repoIds ?? [],
      domain_id: domainId,
    }),
  });

// ── Entity relationships + source ──────────────────────────

export interface RelationshipEdge {
  id: string;
  rel_type: string;
  direction: "outbound" | "inbound";
  source_urn: string;
  source_name: string;
  source_entity_type: string | null;
  target_urn: string;
  target_name: string;
  target_entity_type: string | null;
  confidence: string | null;
}

export interface EntityRelationships {
  outbound: RelationshipEdge[];
  inbound: RelationshipEdge[];
}

export interface EntitySource {
  file_path: string;
  content: string;
  truncated: boolean;
}

export const getEntityRelationships = (entityId: string) =>
  request<EntityRelationships>(
    `/entities/by-id/${entityId}/relationships`,
  );

export const getEntitySource = (entityId: string) =>
  request<EntitySource>(`/entities/by-id/${entityId}/source`);

// ── Cost tracking ──────────────────────────────────────────

export interface CostByRow {
  key: string;
  label: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  calls: number;
}

export interface CostDailyPoint {
  date: string;
  cost_usd: number;
}

export interface CostSummary {
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_calls: number;
  by_model: CostByRow[];
  by_phase: CostByRow[];
  by_repo: CostByRow[];
  last_7_days: CostDailyPoint[];
  window: "7d" | "30d" | "all";
  as_of: string;
}

export const getCostSummary = (window: "7d" | "30d" | "all" = "30d") =>
  request<CostSummary>(`/costs/summary?window=${window}`);

// ── Conversations (chat) ───────────────────────────────────

/** Honest, after-the-fact view of what the last research turn actually
 *  did — sourced from ``conversation_research_state.bundle`` server-side.
 *
 *  ``null`` until the first research turn finishes. The chat header
 *  prefers this over ``scope_repo_ids`` so the user sees the routed
 *  reality instead of the (often empty or oversized) input scope. */
export interface LastResearchSummary {
  repos_used: string[];
  repos_used_names: string[];
  scope_rationale: string | null;
  /** True when the router fell back to defaults or every routed agent
   *  came back empty — drives the amber "router fallback" badge. */
  router_fallback: boolean;
}

export interface ConversationSummary {
  id: string;
  title: string;
  scope_repo_ids: string[];
  scope_domain_id: string | null;
  archived: boolean;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview: string | null;
  last_research?: LastResearchSummary | null;
}

export interface ConversationMessage {
  id: string;
  conversation_id: string;
  turn_index: number;
  role: "user" | "assistant";
  content: string;
  sources: SourceCitation[];
  confidence: string | null;
  reasoning: string | null;
  gaps: string[];
  token_counts: Record<string, number>;
  suggested_followups?: SuggestedFollowup[];
  cost_usd: number | null;
  created_at: string;
}

/** A clickable "Ask next" follow-up prompt suggestion. */
export interface SuggestedFollowup {
  prompt: string;
  rationale?: string;
  repos_to_index?: string[];
}

export interface ConversationDetail extends ConversationSummary {
  messages: ConversationMessage[];
}

export const listConversations = () =>
  request<ConversationSummary[]>(`/conversations`);

export const getConversation = (id: string) =>
  request<ConversationDetail>(`/conversations/${id}`);

export const createConversation = (params: {
  title?: string;
  scope_repo_ids?: string[];
  scope_domain_id?: string | null;
}) =>
  request<{ id: string }>(`/conversations`, {
    method: "POST",
    body: JSON.stringify(params),
  });

export const renameConversation = (id: string, title: string) =>
  request<ConversationSummary>(`/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });

export const deleteConversation = (id: string, opts?: { hard?: boolean }) =>
  request<{ id: string; status: string }>(
    `/conversations/${id}${opts?.hard ? "?hard=true" : ""}`,
    { method: "DELETE" },
  );

/**
 * Un-archive AND un-soft-delete a conversation in one call.
 * Idempotent on a live conversation — just returns
 * the current summary unchanged.
 */
export const restoreConversation = (id: string) =>
  request<ConversationSummary>(`/conversations/${id}/restore`, {
    method: "POST",
  });

export const listArchivedConversations = () =>
  request<ConversationSummary[]>(`/conversations?include_archived=true`);

export interface AppendMessageOptions {
  scopeRepoIds?: string[];
  /**
   * Resumability: pass a UUID generated client-side per turn. A retry
   * with the same key returns the cached final answer instead of
   * spawning a fresh research run (saves ~$10–30 on retries within
   * 24h). Reuse the key on retry, generate a new one for a new turn.
   */
  idempotencyKey?: string;
  /**
   * Bypass the idempotency cache and force a fresh run even if a
   * cached answer exists for this idempotency key. Use when the
   * underlying data (Jira, code, etc.) has changed since the cached
   * answer was produced.
   */
  forceRefresh?: boolean;
}

export interface AppendMessageResult {
  user_message_id: string;
  stream_url: string;
  /**
   * When set, the request was answered from the idempotency cache —
   * no new research run was spawned. The UI can render this answer
   * immediately without waiting for the SSE stream.
   */
  cached_answer?: string | null;
  cached_run_id?: string | null;
}

export const appendMessage = (
  id: string,
  content: string,
  options: AppendMessageOptions | string[] = {},
) => {
  // Back-compat: callers used to pass scopeRepoIds as a 3rd arg.
  const opts: AppendMessageOptions = Array.isArray(options)
    ? { scopeRepoIds: options }
    : options;
  return request<AppendMessageResult>(
    `/conversations/${id}/messages`,
    {
      method: "POST",
      body: JSON.stringify({
        content,
        scope_repo_ids: opts.scopeRepoIds,
        idempotency_key: opts.idempotencyKey,
        force_refresh: opts.forceRefresh ?? false,
      }),
    },
  );
};

/**
 * Read the most recent research_runs row for a conversation.
 *
 * Used on conversation mount + tab visibility change to detect:
 *  - ``in_flight``: a run is currently active. The UI should show
 *    the progress widget and reattach the SSE stream.
 *  - ``latest``: the most recent run's terminal state. Lets the UI
 *    render a "last attempt failed" / "answer ready" banner without
 *    a fresh research call.
 *
 * Both fields can be null on a brand-new conversation.
 */
export interface ResearchRunRecord {
  id: string;
  conversation_id: string;
  turn_id: number;
  status: (
    | "pending" | "scoping" | "fanout" | "synthesizing"
    | "done" | "failed" | "cancelled"
  );
  phase_data: Record<string, unknown>;
  final_answer_text: string | null;
  total_cost_usd: number;
  saved_cost_usd: number;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
  idempotency_key: string | null;
}

export interface ResearchStatus {
  in_flight: ResearchRunRecord | null;
  latest: ResearchRunRecord | null;
}

export const getResearchStatus = (conversationId: string) =>
  request<ResearchStatus>(
    `/conversations/${conversationId}/research-status`,
  );

/**
 * Cancel an in-flight query for a conversation. Best-effort — if no
 * task is running this returns ``{cancelled:false, reason:"not_running"}``.
 * The server publishes a synthetic ``error`` + ``done`` SSE pair so the
 * UI's stream watchdog terminates immediately.
 */
export const cancelConversation = (id: string) =>
  request<{ cancelled: boolean; reason?: string }>(
    `/conversations/${id}/cancel`,
    { method: "POST" },
  );

/**
 * Chat-message feedback (priority-3 of the learning loop). Translates
 * a 👍 or 👎 on an assistant answer into per-citation
 * ``confidence_deltas`` rows on the server. Up = ``audit_confirmed``
 * with a small confidence bump; down = ``audit_contradicted`` with
 * an immediate downgrade (governance applies downgrades right away,
 * upgrades still need cross-confirmation).
 *
 * Returns the count of URNs scored so the UI can show "10 citations
 * recorded" or fall back to a generic "thanks" if the message had
 * no cited entities.
 */
export const submitMessageFeedback = (
  conversationId: string,
  messageId: string,
  verdict: "up" | "down",
  note?: string,
) =>
  request<{
    recorded: number;
    skipped?: number;
    verdict?: string;
    reason?: string;
    message?: string;
  }>(
    `/conversations/${conversationId}/messages/${messageId}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verdict, note }),
    },
  );

/**
 * Cancel a SINGLE per-repo external research agent (jira /
 * confluence / github / slack). The bundle's other agents keep
 * running and synthesis still completes with the partial set —
 * different from ``cancelConversation`` which stops the whole turn.
 *
 * ``agentKey`` is ``"<repo_id>:<source>"``; valid sources are
 * ``jira``, ``confluence``, ``github``, ``slack``. Cancelling the
 * code agent itself isn't supported — code is too central to lose
 * without ending the answer; use ``cancelConversation`` for that.
 *
 * Returns ``{cancelled:false, reason:"not_running"}`` when the
 * agent has already finished or never started. Idempotent.
 */
export const cancelAgent = (
  conversationId: string, agentKey: string,
) =>
  request<{
    cancelled: boolean;
    reason?: string;
    agent_key: string;
  }>(
    `/conversations/${conversationId}/agents/${encodeURIComponent(agentKey)}/cancel`,
    { method: "POST" },
  );

// ── Per-conversation chat settings ──────────────────────────────
//
// Knobs that drive research behaviour: walk depth, breadth,
// edge-confidence floor, edge directions, external-source toggles,
// multi-code-agent cap. The UI panel (ChatSettingsPanel) reads via
// getChatSettings and PATCHes via updateChatSettings; the
// coordinator reads server-side at the top of every research turn.

export interface ChatSettings {
  walk_depth: number;
  walk_breadth_soft_cap: number;
  confidence_floor: number;
  edge_directions: "upstream" | "downstream" | "both";
  edge_types: string[];
  code_first_strict: boolean;
  agents_per_repo_max: number;
  external_sources: Array<"jira" | "confluence" | "github" | "slack" | "gdocs">;
  auto_extract_missing: boolean;
  auto_extract_wait_s: number;
  research_full_depth: boolean;
  research_include_slack: boolean;
  research_include_github: boolean;
  research_include_confluence: boolean;
}

export type PartialChatSettings = Partial<ChatSettings>;

export const getChatSettings = (id: string) =>
  request<ChatSettings>(`/conversations/${id}/settings`);

export const updateChatSettings = (id: string, patch: PartialChatSettings) =>
  request<ChatSettings>(`/conversations/${id}/settings`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

// ── Claude CLI auth ─────────────────────────────────────────

export interface ClaudeCliStatus {
  ok: boolean;
  error?: string;
  hint?: string;
  email?: string;
  org_name?: string;
  subscription_type?: string;
  auth_method?: string;
  api_provider?: string;
  triage_model?: string;
  extraction_model?: string;
  agent_model?: string;
}

/**
 * The /health/claude-cli endpoint returns an un-enveloped object directly
 * (no BifrostEnvelope wrapping) so we fetch it manually.
 *
 * On HTTP error or network failure we THROW so react-query treats it
 * as `isError` rather than caching a false ``{ok:false}``. Without
 * this, a transient API-down moment (backend restart, network blip)
 * would persist the "Claude CLI is not authenticated" banner long
 * after the API came back, until the next refetchInterval tick.
 * Throwing surfaces the error state distinctly from a verified
 * "auth check ran and failed" — the banner only shows on the latter.
 */
export async function getClaudeCliStatus(): Promise<ClaudeCliStatus> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/health/claude-cli`);
  } catch (err) {
    // Network error — backend down, CORS preflight failed, etc.
    // Throw so react-query keeps the LAST GOOD value (or shows
    // nothing if there isn't one) instead of caching a fake failure.
    throw new Error(
      `Network error reaching auth probe: ${(err as Error).message}`,
    );
  }
  if (!res.ok) {
    // Server responded with a 5xx — treat it the same as a network
    // error: don't pin a false "not authenticated" banner.
    throw new Error(`HTTP ${res.status} from /health/claude-cli`);
  }
  return res.json();
}

// ── Collaboration sessions ──────────────────────────────

export type CollabSessionState =
  | "open"
  | "clarifying"
  | "resolved"
  | "briefed"
  | "closed";

export type CollabMessageRole =
  | "requester"
  | "system"
  | "clarifier_agent"
  | "synthesizer_agent";

export type CollabDoubtStatus =
  | "open"
  | "answered_by_doc"
  | "answered_by_human"
  | "unresolved"
  | "escalated";

export type CollabArtifactKind =
  | "final_brief"
  | "estimate"
  | "risk_log"
  | "draft_ask_message";

export interface CollabSessionSummary {
  id: string;
  requester: string;
  state: CollabSessionState;
  requirement_text: string;
  matched_doc_ids: string[];
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  message_count: number;
  open_doubt_count: number;
  linked_task_count?: number;
  linked_task_keys?: string[];
}

export interface CollabMessage {
  id: string;
  session_id: string;
  ord: number;
  role: CollabMessageRole;
  content_md: string;
  source_citations: string[];
  doubt_id: string | null;
  created_at: string;
}

export interface CollabDoubt {
  id: string;
  session_id: string;
  question: string;
  status: CollabDoubtStatus;
  resolved_md: string | null;
  source_ref: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface CollabArtifact {
  id: string;
  session_id: string;
  kind: CollabArtifactKind;
  fs_path: string;
  created_at: string;
}

export interface CollabSessionDetail extends CollabSessionSummary {
  messages: CollabMessage[];
  doubts: CollabDoubt[];
  artifacts: CollabArtifact[];
  // Eagerly-loaded body of the final_brief artifact, if one exists
  // and the file is still readable. ``null`` = no brief yet OR the
  // operator moved/deleted the file out of band.
  brief_md: string | null;
}

export const listCollabSessions = (params?: {
  state?: CollabSessionState;
  requester?: string;
  limit?: number;
}) => {
  const qs = new URLSearchParams();
  if (params?.state) qs.set("state", params.state);
  if (params?.requester) qs.set("requester", params.requester);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<CollabSessionSummary[]>(`/collab-sessions${suffix}`);
};

export const getCollabSession = (id: string) =>
  request<CollabSessionDetail>(`/collab-sessions/${id}`);

export const createCollabSession = (requirement_text: string, requester?: string) =>
  request<{ id: string }>(`/collab-sessions`, {
    method: "POST",
    body: JSON.stringify({ requirement_text, requester }),
  });

export const appendCollabMessage = (
  id: string,
  content_md: string,
  opts?: { doubt_id?: string; source_citations?: string[] },
) =>
  request<CollabMessage>(`/collab-sessions/${id}/messages`, {
    method: "POST",
    body: JSON.stringify({
      content_md,
      doubt_id: opts?.doubt_id,
      source_citations: opts?.source_citations ?? [],
    }),
  });

export const resolveCollabDoubt = (
  sessionId: string,
  doubtId: string,
  body: {
    status: Exclude<CollabDoubtStatus, "open">;
    resolved_md?: string;
    source_ref?: string;
  },
) =>
  request<CollabDoubt>(
    `/collab-sessions/${sessionId}/doubts/${doubtId}/resolve`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );

export const closeCollabSession = (
  id: string,
  acceptOpenDoubtsAsRisks = false,
) =>
  request<CollabSessionDetail>(`/collab-sessions/${id}/close`, {
    method: "POST",
    body: JSON.stringify({
      accept_open_doubts_as_risks: acceptOpenDoubtsAsRisks,
    }),
  });

export const reopenCollabSession = (id: string) =>
  request<CollabSessionDetail>(`/collab-sessions/${id}/reopen`, {
    method: "POST",
  });

export const linkTaskToCollabSession = (sessionId: string, taskId: string) =>
  request<{ ok: boolean }>(`/collab-sessions/${sessionId}/link-task`, {
    method: "POST",
    body: JSON.stringify({ task_id: taskId }),
  });

export interface CollabSessionTask {
  id: string;
  title: string;
  state: string;
  jira_key: string | null;
  jira_url: string | null;
  pr_url: string | null;
  pr_number: number | null;
  updated_at: string;
  linked_at: string;
}

export const getCollabSessionTasks = (sessionId: string) =>
  request<CollabSessionTask[]>(`/collab-sessions/${sessionId}/tasks`);

// ── Handover docs ────────────────────────────────────────

export type HandoverScopeKind =
  | "feature"
  | "repo"
  | "domain"
  | "region"
  | "team"
  | "session";

export type HandoverFreshness =
  | "fresh"
  | "stale"
  | "very_stale"
  | "enriched_by_children"
  | "stale_due_to_children";

export interface HandoverDocSummary {
  id: string;
  scope_kind: HandoverScopeKind;
  scope_value: string;
  parent_id: string | null;
  fs_path: string | null;
  freshness_state: HandoverFreshness;
  last_generated_at: string | null;
  last_validated_at: string | null;
  generation_cost_usd: number | null;
  // non-null when the doc has been soft-archived. Default
  // list views filter archived rows; the "Show archived" toggle in
  // the list page adds them back so the user can restore.
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface HandoverDocDetail extends HandoverDocSummary {
  // Eagerly-loaded Markdown body of the on-disk index.md, or null
  // if the file has been removed/corrupted out of band.
  body_md: string | null;
}

export const listHandoverDocs = (params?: {
  scope_kind?: HandoverScopeKind;
  limit?: number;
  include_archived?: boolean;
}) => {
  const qs = new URLSearchParams();
  if (params?.scope_kind) qs.set("scope_kind", params.scope_kind);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.include_archived) qs.set("include_archived", "true");
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<HandoverDocSummary[]>(`/handover-docs${suffix}`);
};

/**
 * Free-text search across handover docs. Backend
 * ranks by simple score (scope_value match + body occurrences,
 * capped) plus recency tie-break — see
 * ``src/bifrost/api/routes/handover_docs.py:search_handover_docs``.
 * A later version could swap this for a real Postgres tsvector + ts_rank index.
 */
export interface HandoverSearchHit extends HandoverDocSummary {
  score: number;
  snippet: string | null;
}

export const searchHandoverDocs = (q: string, limit?: number) => {
  const qs = new URLSearchParams({ q });
  if (limit) qs.set("limit", String(limit));
  return request<HandoverSearchHit[]>(`/handover-docs/search?${qs.toString()}`);
};

export const getHandoverDoc = (id: string) =>
  request<HandoverDocDetail>(`/handover-docs/${id}`);

/**
 * Soft-archive a handover doc. The on-disk Markdown is
 * untouched — restoring re-enables it. Default list views hide
 * archived rows; ``listHandoverDocs({ include_archived: true })``
 * surfaces them with a Restore button.
 */
export const archiveHandoverDoc = (id: string) =>
  request<HandoverDocDetail>(`/handover-docs/${id}`, {
    method: "DELETE",
  });

export const restoreHandoverDoc = (id: string) =>
  request<HandoverDocDetail>(`/handover-docs/${id}/restore`, {
    method: "POST",
  });

/**
 * Pre-flight cost estimate for a handover generation.
 * Mirrors ``CostEstimateData`` on the backend. The list page fetches
 * this before clicking "Generate" so the user can see what the run
 * will cost; the same estimate gates the generate route via the
 * ``handover_cost_ceiling_exceeded`` 409.
 */
export interface HandoverCostEstimate {
  estimated_usd: number;
  reasoning: string;
  repos_to_generate: number;
  will_use_tree: boolean;
  over_ceiling: boolean;
  ceiling_usd: number;
}

const forceQuery = (force?: boolean) => (force ? "?force=true" : "");

export const generateRepoHandover = (repoId: string, force?: boolean) =>
  request<{ doc_id: string; fs_path: string; title: string; used_fallback: boolean }>(
    `/handover-docs/repos/${repoId}/generate${forceQuery(force)}`,
    { method: "POST" },
  );

export const estimateRepoHandoverCost = (repoId: string) =>
  request<HandoverCostEstimate>(`/handover-docs/repos/${repoId}/estimate`);

export interface DoubtCandidate {
  representative_question: string;
  occurrence_count: number;
  session_ids: string[];
  first_seen_at: string;
  last_seen_at: string;
}

export const getHandoverDoubtCandidates = (
  docId: string,
  minOccurrences = 3,
) =>
  request<DoubtCandidate[]>(
    `/handover-docs/${docId}/candidates?min_occurrences=${minOccurrences}`,
  );

export const promoteDoubtCandidate = (
  docId: string,
  body: { question: string; answer_md: string },
) =>
  request<HandoverDocDetail>(
    `/handover-docs/${docId}/promote-candidate`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );

/**
 * Replace the on-disk Markdown body of a handover doc
 * with human-edited content from the in-UI editor. Backend writes
 * atomically (temp + rename) and bumps ``last_validated_at`` only.
 */
export const patchHandoverDocBody = (docId: string, body_md: string) =>
  request<HandoverDocDetail>(`/handover-docs/${docId}/body`, {
    method: "PATCH",
    body: JSON.stringify({ body_md }),
  });

/**
 * Regenerate a single section of a handover doc
 * in-place. Other sections (including any Q+A appended via
 * promotions and any human edits) are preserved verbatim — only the
 * targeted ``## <Header>`` block is rewritten.
 *
 * Cheap by design (one Sonnet call, ~$0.05-$0.10), so the route is
 * not gated by the per-call cost ceiling that ``generate*Handover``
 * funcs respect.
 */
export type HandoverSectionKey =
  | "executive_summary"
  | "architecture"
  | "critical_flows"
  | "ownership"
  | "dependencies"
  | "ktlo_surface"
  | "glossary"
  | "common_questions";

export const HANDOVER_SECTION_LABELS: Record<HandoverSectionKey, string> = {
  executive_summary: "Executive summary",
  architecture: "Architecture",
  critical_flows: "Critical flows",
  ownership: "Ownership",
  dependencies: "Dependencies",
  ktlo_surface: "KTLO surface",
  glossary: "Glossary",
  common_questions: "Common questions",
};

export const regenerateHandoverSection = (
  docId: string,
  sectionKey: HandoverSectionKey,
) =>
  request<HandoverDocDetail>(
    `/handover-docs/${docId}/sections/${sectionKey}/regenerate`,
    { method: "POST" },
  );

export const generateDomainHandover = (domainId: string, force?: boolean) =>
  request<{ doc_id: string; fs_path: string; title: string; used_fallback: boolean }>(
    `/handover-docs/domains/${domainId}/generate${forceQuery(force)}`,
    { method: "POST" },
  );

export const estimateDomainHandoverCost = (domainId: string) =>
  request<HandoverCostEstimate>(`/handover-docs/domains/${domainId}/estimate`);

export const generateTeamHandover = (squad: string, force?: boolean) =>
  request<{ doc_id: string; fs_path: string; title: string; used_fallback: boolean }>(
    `/handover-docs/teams/${encodeURIComponent(squad)}/generate${forceQuery(force)}`,
    { method: "POST" },
  );

export const estimateTeamHandoverCost = (squad: string) =>
  request<HandoverCostEstimate>(
    `/handover-docs/teams/${encodeURIComponent(squad)}/estimate`,
  );

export const generateRegionHandover = (region: string, force?: boolean) =>
  request<{ doc_id: string; fs_path: string; title: string; used_fallback: boolean }>(
    `/handover-docs/regions/${encodeURIComponent(region)}/generate${forceQuery(force)}`,
    { method: "POST" },
  );

export const estimateRegionHandoverCost = (region: string) =>
  request<HandoverCostEstimate>(
    `/handover-docs/regions/${encodeURIComponent(region)}/estimate`,
  );

// ── Handover audit trail ──────────────────────────────

export type HandoverAuditEventType =
  | "generated"
  | "archived"
  | "restored"
  | "edited"
  | "promoted";

export interface HandoverAuditEntry {
  id: string;
  doc_id: string;
  event_type: HandoverAuditEventType;
  actor: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export const getHandoverAudits = (docId: string, limit = 50) =>
  request<HandoverAuditEntry[]>(
    `/handover-docs/${docId}/audit?limit=${limit}`,
  );

// ── Handover body history ───────────────────────────

export type HandoverBodyHistoryEventType =
  | "pre-regen"
  | "pre-edit"
  | "pre-promote";

export interface HandoverBodyHistoryEntry {
  id: string;
  doc_id: string;
  event_type: HandoverBodyHistoryEventType;
  body_md: string;
  created_at: string;
}

export const getHandoverBodyHistory = (docId: string, limit = 20) =>
  request<HandoverBodyHistoryEntry[]>(
    `/handover-docs/${docId}/history?limit=${limit}`,
  );

// ── Handover collaboration workflow ───────────────────
//
// Wrappers for the team-collaboration affordances layered on top of
// handover docs: cross-references from collab sessions, per-section
// feedback, body restoration, owner inbox, and section metadata.
// Backend routes live in `src/bifrost/api/routes/handover_docs.py`
// (alongside the existing handover endpoints).

/**
 * A collab session that referenced this handover during clarification
 * — surfaced in the detail page's "Referencing sessions" panel so an
 * owner can see who's actively asking about the doc.
 */
export interface ReferencingSessionEntry {
  id: string;
  requester: string;
  requirement_text: string;
  state: "open" | "clarifying" | "resolved" | "briefed" | "closed";
  doubt_count_open: number;
  created_at: string;
  last_activity_at: string;
}

/**
 * A flag a reader can drop on a single handover section ("this part
 * is wrong / outdated / unclear") that the owner sees in their inbox.
 * Resolution timestamps + actor are tracked so the audit log can
 * answer "who fixed what, when".
 */
export interface HandoverFeedbackEntry {
  id: string;
  doc_id: string;
  section_key: string;
  kind: "outdated" | "incorrect" | "unclear" | "missing_info" | "other";
  note: string | null;
  actor: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
}

/**
 * One row of the owner inbox: a handover that needs attention because
 * it's stale, has open feedback, has active referencing sessions, or
 * has surfaced doubt candidates above the promotion threshold.
 */
export interface HandoverInboxEntry {
  id: string;
  scope_kind: string;
  scope_value: string;
  freshness_state: string;
  open_feedback_count: number;
  referencing_sessions_count: number;
  doubt_candidates_count: number;
  last_touched_at: string;
}

/**
 * Per-section editorial state — used by the detail page to badge a
 * section header with "feedback open" / "regenerated 2h ago" hints
 * without re-fetching the full audit log.
 */
export interface SectionMetadata {
  last_regenerated_at: string | null;
  feedback_open_count: number;
  last_feedback_at: string | null;
}

export const getHandoverReferencingSessions = (docId: string) =>
  request<ReferencingSessionEntry[]>(
    `/handover-docs/${docId}/referencing-sessions`,
  );

export const submitHandoverSectionFeedback = (
  docId: string,
  body: {
    section_key: string;
    kind: HandoverFeedbackEntry["kind"];
    note?: string | null;
  },
) =>
  request<HandoverFeedbackEntry>(
    `/handover-docs/${docId}/section-feedback`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );

export const listHandoverSectionFeedback = (
  docId: string,
  opts: { include_resolved?: boolean } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.include_resolved) qs.set("include_resolved", "true");
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<HandoverFeedbackEntry[]>(
    `/handover-docs/${docId}/section-feedback${suffix}`,
  );
};

export const resolveHandoverSectionFeedback = (
  docId: string,
  feedbackId: string,
) =>
  request<HandoverFeedbackEntry>(
    `/handover-docs/${docId}/section-feedback/${feedbackId}/resolve`,
    { method: "POST" },
  );

export const restoreHandoverVersion = (docId: string, history_id: string) =>
  request<HandoverDocDetail>(`/handover-docs/${docId}/restore-version`, {
    method: "POST",
    body: JSON.stringify({ history_id }),
  });

export const getHandoverSectionMetadata = (docId: string) =>
  request<Record<string, SectionMetadata>>(
    `/handover-docs/${docId}/section-metadata`,
  );

export const getHandoverInbox = () =>
  request<HandoverInboxEntry[]>(`/handover-docs/inbox`);

// ── Benchmarks (Tier 3) ─────────────────────────────────────

export interface BenchmarkRunKindStats {
  n: number;
  avg_score: number;
  avg_recall: number;
  avg_precision: number;
  avg_specificity: number;
  total_cost_usd: number;
  avg_cost_usd: number;
  avg_duration_ms: number;
  cache_hit_rate: number;
  edges_confirmed: number;
  new_edges_inserted: number;
}

export interface BenchmarkBuild {
  build_label: string | null;
  by_run_kind: Record<string, BenchmarkRunKindStats>;
}

export interface BenchmarkSummary {
  domain: string;
  n_questions: number;
  builds: BenchmarkBuild[];
}

export interface BenchmarkQuestionRow {
  id: string;
  question_key: string;
  question_text: string;
  paraphrase: string | null;
  tier_floor: number;
  expects_diagram: boolean;
  expected_format: string | null;
  max_words: number | null;
  n_entities: number;
  n_facts: number;
  metadata: Record<string, unknown>;
  expected_score: number | null;
}

export interface BenchmarkRunRow {
  id: string;
  question_id: string;
  question_key: string;
  run_kind: string;
  build_label: string | null;
  score_total: number;
  score_recall: number;
  score_precision: number;
  score_specificity: number;
  score_coverage: number;
  score_tier_integrity: number;
  score_evidence_consistency: number;
  score_format_adherence: number;
  cost_usd: number;
  duration_ms: number;
  agents_dispatched: number;
  agents_cache_hit: number;
  facts_injected: number;
  edges_confirmed: number;
  edges_contradicted: number;
  new_edges_inserted: number;
  answer_confidence: string | null;
  cited_entity_urns: string[];
  missing_entities: string[];
  missing_key_facts: Array<{ needle: string; why?: string }>;
  started_at: string | null;
  finished_at: string | null;
  conversation_id: string | null;
}

export interface BenchmarkDomain {
  domain: string;
  n_questions: number;
  n_runs: number;
  last_run_at: string | null;
  sequenced?: boolean;
}

export const getBenchmarksDomains = () =>
  request<BenchmarkDomain[]>(`/admin/benchmarks/domains`);

export const getBenchmarksSummary = (domain: string = "acme") =>
  request<BenchmarkSummary>(
    `/admin/benchmarks/summary?domain=${encodeURIComponent(domain)}`,
  );

export const getBenchmarksQuestions = (domain: string = "acme") =>
  request<BenchmarkQuestionRow[]>(
    `/admin/benchmarks/questions?domain=${encodeURIComponent(domain)}`,
  );

export const getBenchmarksRuns = (params: {
  domain?: string;
  buildLabel?: string;
  runKind?: string;
  limit?: number;
} = {}) => {
  const q = new URLSearchParams();
  q.set("domain", params.domain ?? "acme");
  if (params.buildLabel) q.set("build_label", params.buildLabel);
  if (params.runKind) q.set("run_kind", params.runKind);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  return request<BenchmarkRunRow[]>(`/admin/benchmarks/runs?${q.toString()}`);
};

// ── Ownership fast-path ──────────────────────────────────────

export interface OwnershipResult {
  repo_name: string;
  squad: string | null;
  slack_channel: string | null;
  primary_contact: string | null;
  last_commit_at: string | null;
  description: string | null;
  service_tier: string | null;
  jira_project_key: string | null;
  confluence_space_key: string | null;
  url: string | null;
  source: "registered" | "catalog";
}

export interface OwnershipResponse {
  results: OwnershipResult[];
  query: string;
  total: number;
}

/** Fast ownership lookup — pure DB query, <200ms, no LLM.
 *  Returns matching repos with squad/channel/contact metadata. */
export const lookupOwnership = (q: string) => {
  const params = new URLSearchParams({ q });
  return request<OwnershipResponse>(`/ownership?${params.toString()}`);
};

/** Create a real Slack *draft* (not a sent message) in `channel` via
 *  the operator's authenticated Slack MCP. The draft lands in their
 *  Slack "Drafts & Sent". Best-effort: `ok=false` + `error` on any
 *  failure (auth missing, channel not found) instead of throwing, so
 *  callers can fall back to clipboard copy. */
export interface SlackDraftResult {
  ok: boolean;
  channel_link: string | null;
  error: string | null;
}

export const createSlackDraft = (channel: string, message: string) =>
  request<SlackDraftResult>(`/collaboration/slack-draft`, {
    method: "POST",
    body: JSON.stringify({ channel: channel.replace(/^#/, ""), message }),
  });

/** Return true if the question text looks like an ownership lookup. */
export function isOwnershipQuestion(text: string): boolean {
  return /who\s+(owns?|maintains?|is\s+responsible\s+for|works?\s+on|runs?)|what\s+(team|squad|group)\s+(owns?|maintains?|is\s+responsible\s+for|works?\s+on)|which\s+(team|squad|group)\s+(owns?|maintains?|is\s+responsible\s+for)|ownership\s+of|owner\s+of|point\s+of\s+contact\s+for|who\s+do\s+I\s+(contact|talk\s+to|ping)\s+about/i.test(text);
}

// ── Integrations health ───────────────────────────────────────

export interface IntegrationSourceHealth {
  status: "ok" | "degraded" | "not_configured" | "error";
  last_synced_at: string | null;
  error_count: number;
}

export interface IntegrationsHealth {
  jira: IntegrationSourceHealth;
  github: IntegrationSourceHealth;
  confluence: IntegrationSourceHealth;
  extraction: {
    repos_fresh: number;
    last_indexed_at: string | null;
  };
}

export const getIntegrationsHealth = () =>
  request<IntegrationsHealth>(`/admin/integrations/health`);

// ── Cross-repo dependencies ───────────────────────────────────

export interface CrossRepoDep {
  repo_id: string;
  repo_name: string;
  rel_types: string[];
  edge_count: number;
}

export interface CrossRepoDepsResponse {
  calls: CrossRepoDep[];
  called_by: CrossRepoDep[];
  total_cross_repo_edges: number;
}

export const getCrossRepoDeps = (repoId: string) =>
  request<CrossRepoDepsResponse>(`/knowledge/repos/${repoId}/cross-repo-deps`);

// ── Blast radius (dependency walk) ───────────────────────────

export type BlastDirection = "downstream" | "upstream" | "both";

export interface BlastNode {
  id: string;
  urn: string;
  name: string;
  entity_type: string;
  repo_id: string;
  distance_from_seed: number;
  is_seed: boolean;
  min_path_confidence: number;
  source_file?: string | null;
  line?: number | null;
  qualified_name?: string | null;
}

export interface BlastEdge {
  source_urn: string;
  target_urn: string;
  rel_type: string;
  is_cross_repo: boolean;
  confidence: number;
  direction_from_seed: string;
}

/** One edge on the seed→impacted path, grounded in code. */
export interface BlastHop {
  from_urn: string;
  from_name: string;
  to_urn: string;
  to_name: string;
  rel_type: string;
  confidence: number;
  is_cross_repo: boolean;
  to_source_file?: string | null;
  to_line?: number | null;
  to_qualified_name?: string | null;
}

/** One impacted entity ranked by directness, carrying its WHY. */
export interface BlastImpact {
  urn: string;
  name: string;
  entity_type: string;
  repo_id: string;
  is_cross_repo: boolean;
  distance_from_seed: number;
  path_confidence: number;
  source_file?: string | null;
  line?: number | null;
  qualified_name?: string | null;
  path: BlastHop[];
  reason: string;
}

export interface BlastRadiusResponse {
  seed_urn: string;
  seed_name: string;
  seed_source_file?: string | null;
  direction: string;
  depth: number;
  impacted_count: number;
  impacts: BlastImpact[];
  tail_count: number;
  nodes: BlastNode[];
  edges: BlastEdge[];
  confidence_buckets: Record<string, string[]>;
  soft_cap_exceeded: boolean;
  stats: Record<string, number>;
}

export const getBlastRadius = (
  urn: string,
  opts: {
    direction?: BlastDirection;
    depth?: number;
    confidence_floor?: number;
  } = {},
) =>
  request<BlastRadiusResponse>(`/dependencies/blast-radius`, {
    method: "POST",
    body: JSON.stringify({ urn, ...opts }),
  });

