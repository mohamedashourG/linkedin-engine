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
