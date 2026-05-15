import { api } from "./api";

export type Candidate = {
  id: string;
  cofounder_id: string;
  post_url: string;
  author_name: string | null;
  post_text: string;
  post_published_at: string | null;
  source: string;
  source_classification: string;
  status: string;
  comment_text: string | null;
  comment_type: string | null;
  icp_score: number | null;
  user_action: string;
  drop_reason: string | null;
};

export type SlateRun = {
  id: string;
  run_date: string;
  status:
    | "building"
    | "sealed"
    | "force_aborted";
  total_slated: number;
  per_cofounder_counts: Record<string, Record<string, number>>;
  sealed_at: string | null;
  email_sent: boolean;
  force_abort_reason: string | null;
  current_stage: string | null;
  stage_progress: { processed: number; total: number } | null;
  stage_started_at: string | null;
  stage_eta_seconds: number | null;
  stage_note: string | null;
  skip_remaining_discovery?: boolean;
  skip_remaining_discovery_at?: string | null;
  /** Source-id the discovery worker is iterating right now (one of
   *  "unipile_title_search" | "unipile_keyword" | "apidirect" | "exa")
   *  or null when discovery is between sources / not started / done. */
  current_source?: string | null;
  skip_current_source?: string | null;
  skip_current_source_consumed_at?: string | null;
  skip_current_source_consumed_for?: string | null;
};

export type SlateCofounder = {
  id: string;
  display_name: string;
  linkedin_url: string;
  daily_volume_target: number;
};

export type PipelinePostRef = {
  id: string;
  post_url: string;
  author_name: string | null;
  post_preview: string;
  status: string;
  drop_reason: string | null;
};

export type PipelineStepBreakdown = {
  passed: PipelinePostRef[];
  failed: PipelinePostRef[];
  pending: PipelinePostRef[];
  passed_total: number;
  failed_total: number;
  pending_total: number;
  truncated: boolean;
};

export type PipelineBreakdown = {
  discovery: PipelineStepBreakdown;
  inline_rubric: PipelineStepBreakdown;
  verification: PipelineStepBreakdown;
  gates: PipelineStepBreakdown;
  allocator: PipelineStepBreakdown;
  drafter: PipelineStepBreakdown;
  rule_23: PipelineStepBreakdown;
  email_delivery: PipelineStepBreakdown;
};

export type SlateTodayResponse = {
  slate_run: SlateRun | null;
  candidates: Candidate[];
  cofounders: SlateCofounder[];
  pipeline: PipelineBreakdown | null;
};

// ── Past-runs viewer types ──────────────────────────────────────────────

export type RunListItem = {
  id: string;
  run_date: string;
  status: "building" | "sealed" | "force_aborted";
  sealed_at: string | null;
  created_at: string;
  runtime_seconds: number | null;
  total_discovered: number;
  total_verified: number;
  total_gated: number;
  total_drafted: number;
  total_slated: number;
  email_sent: boolean;
  force_abort_reason: string | null;
};

export type RunsListResponse = {
  runs: RunListItem[];
  next_before: string | null;
};

export type SourceStatusBucket = {
  source: string;
  status: string;
  count: number;
};

export type DropReasonBucket = {
  reason: string;
  count: number;
};

/** Posts surfaced by discovery this run that were filtered as "already
 *  found in a previous run" (90-day exhaustion window). Null when the
 *  run started before this telemetry shipped. */
export type CrossRunDedupSkips = {
  /** Total times a re-surfaced URL was skipped (one URL may have been
   *  re-surfaced by multiple vendors in the same run — each is counted). */
  total_count: number;
  /** First-N unique skipped canonical post URLs (capped at sample_cap).
   *  Used by the UI to render a "show skipped" list without unbounded growth. */
  sample_urls: string[];
  sample_cap: number;
  /** Size of the seen-urls set when the run started (i.e. the operator's
   *  90-day post history). For context vs total_count. */
  seeded_urls_count: number;
  captured_at: string | null;
};

export type RunDetailResponse = {
  slate_run: SlateRun;
  runtime_seconds: number | null;
  total_discovered: number;
  total_verified: number;
  total_gated: number;
  total_drafted: number;
  total_slated: number;
  source_status: SourceStatusBucket[];
  top_drop_reasons: DropReasonBucket[];
  candidates: Candidate[];
  cofounders: { id: string; display_name: string; active: boolean }[];
  cross_run_dedup_skips: CrossRunDedupSkips | null;
};

/** Per-line-item bucket as written by the backend `cost_tracker`. */
export type CostLineItem = {
  count?: number;
  dollars?: number;
  /** LLM-only — prompt/completion token counters. */
  prompt_tokens?: number;
  completion_tokens?: number;
};

/** Per-provider section under `cost_breakdown`. */
export type CostProvider = {
  totals?: CostLineItem;
  line_items?: Record<string, CostLineItem>;
  last_at?: string | null;
};

export type RunCostsResponse = {
  slate_run_id: string;
  updated_at: string | null;
  /** Grand totals across every provider + LLM. */
  totals: {
    calls?: number;
    dollars?: number;
    /** Aggregate LLM token usage when present. */
    prompt_tokens?: number;
    completion_tokens?: number;
  };
  /** Per-provider sections — keys include: wiza, crustdata, apidirect,
   * unipile, llm. */
  providers: Record<string, CostProvider>;
};

export const slateApi = {
  today: () => api.get<SlateTodayResponse>("/api/slate/today"),
  action: (
    candidateId: string,
    action: "copied" | "shipped" | "dropped" | "edited",
    edited_text?: string,
  ) =>
    api.put<{ ok: string }>(`/api/slate/candidates/${candidateId}/action`, {
      action,
      edited_text,
    }),
  runNow: () =>
    api.post<{ task_id: string; status: string }>("/api/slate/run-now"),
  skipDiscovery: (slateRunId: string) =>
    api.post<{
      slate_run_id: string;
      skip_remaining_discovery: boolean;
      skip_remaining_discovery_at: string | null;
      status: string;
      current_stage: string | null;
    }>(`/api/slate/runs/${encodeURIComponent(slateRunId)}/skip-discovery`),
  /**
   * Skip ONLY the discovery source the worker is iterating right now,
   * then continue with the next source in order:
   *   unipile_title_search → unipile_keyword → apidirect → exa
   * Differs from skipDiscovery (which abandons ALL remaining sources).
   * Returns 409 if no source is currently active.
   */
  skipCurrentSource: (slateRunId: string) =>
    api.post<{
      slate_run_id: string;
      skipped_source: string;
      next_source: string | null;
      status: string;
      current_stage: string | null;
    }>(`/api/slate/runs/${encodeURIComponent(slateRunId)}/skip-current-source`),
  runs: (opts?: { limit?: number; before?: string }) => {
    const qs = new URLSearchParams();
    if (opts?.limit) qs.set("limit", String(opts.limit));
    if (opts?.before) qs.set("before", opts.before);
    const tail = qs.toString();
    return api.get<RunsListResponse>(
      `/api/slate/runs${tail ? "?" + tail : ""}`,
    );
  },
  run: (slateRunId: string) =>
    api.get<RunDetailResponse>(
      `/api/slate/runs/${encodeURIComponent(slateRunId)}`,
    ),
  /**
   * Live cost breakdown for an in-flight or completed slate run.
   * Returns running totals from `slate_runs.cost_breakdown` — every paid
   * provider call (Wiza / Crustdata / APIDirect) and LLM round-trip
   * (OpenAI / Anthropic) increments this subdoc as the run executes, so
   * polling mid-flight shows the dollars climb in real time.
   *
   * Shape:
   *   totals.calls / totals.dollars  — grand totals across everything
   *   providers.<name>.totals.{count, dollars}
   *   providers.<name>.line_items.<key>.{count, dollars, ...}
   *   providers.llm.line_items.<model:tier>.{prompt_tokens, completion_tokens, ...}
   */
  runCosts: (slateRunId: string) =>
    api.get<RunCostsResponse>(
      `/api/slate/runs/${encodeURIComponent(slateRunId)}/costs`,
    ),
  emailSelected: (
    slateRunId: string,
    body: { candidate_ids: string[]; to?: string; subject?: string },
  ) =>
    api.post<{ message_id: string; count: number; to: string }>(
      `/api/slate/runs/${encodeURIComponent(slateRunId)}/email-selected`,
      body,
    ),
  tracker: (slateRunId: string) =>
    api.get<TrackerResponse>(
      `/api/slate/runs/${encodeURIComponent(slateRunId)}/tracker`,
    ),
  trackSelected: (slateRunId: string, candidateIds: string[]) =>
    api.post<TrackerResponse>(
      `/api/slate/runs/${encodeURIComponent(slateRunId)}/track-selected`,
      { candidate_ids: candidateIds },
    ),
};

// ── Tracker types (shared by past-run page + pipeline lead detail) ─────

export type TrackerReply = {
  id: string;
  text: string;
  author_name: string | null;
  author_linkedin_url: string | null;
  author_is_post_owner: boolean;
  published_at: string | null;
  suggested_reply: string;
  suggested_reply_type: string;
  user_action: "pending" | "sent" | "dismissed" | "edited";
};

export type TrackerCandidate = {
  candidate_id: string;
  cofounder_id: string;
  status: string;
  shipped_at: string | null;
  post_url: string;
  post_text_preview: string;
  author_name: string | null;
  our_comment_text: string;
  our_comment_id: string | null;
  our_comment_status: string;
  latest_reaction_count: number;
  latest_reply_count: number;
  latest_polled_at: string | null;
  /** Parent-post engagement from APIdirect /v1/linkedin/post. */
  post_likes: number;
  post_comments_total: number;
  post_shares: number;
  /** LinkedIn reaction breakdown by type — like, celebrate, support,
   *  love, insightful, funny. Null when APIdirect hasn't returned data
   *  yet (pre-track or quota exhausted). */
  post_reactions: Record<string, number> | null;
  post_polled_at: string | null;
  replies: TrackerReply[];
};

export type TrackerResponse = {
  slate_run_id: string;
  candidates: TrackerCandidate[];
  polled_at: string | null;
};
