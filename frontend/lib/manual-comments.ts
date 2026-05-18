/**
 * Client for the manual-comment posting feature.
 *
 * Operator workflow:
 *   1. POST /upload  (multipart) — parse CSV, create a campaign + jobs
 *   2. GET  /accounts — pick which Unipile account to post from
 *   3. POST /campaigns/{id}/send  with `{ unipile_account_id, dry_run }`
 *   4. POST /campaigns/{id}/refresh-engagement — manual engagement snapshot
 */

import { api } from "@/lib/api";

export type ManualCommentJob = {
  id: string;
  campaign_id: string;
  row_index: number;
  post_url: string;
  post_id: string | null;
  draft_comment: string;
  /** queued | dry_run | posted | failed | deleted */
  status: "queued" | "dry_run" | "posted" | "failed" | "deleted";
  unipile_account_id: string | null;
  comment_id: string | null;
  posted_at: string | null;
  deleted_at: string | null;
  delete_error: string | null;
  error: string | null;
  last_engagement_at: string | null;
  /** Post-level aggregates (everyone's likes/comments/shares on the post). */
  latest_likes: number | null;
  latest_comments: number | null;
  latest_shares: number | null;
  /** Comment-level (reactions + replies on OUR specific comment). */
  latest_my_comment_reactions: number | null;
  latest_my_comment_replies: number | null;
  /** Actual reply text + author name under our comment, captured on the
   *  last refresh when reply_count > 0. Rendered as a flat list — no
   *  per-author profile links by product decision.
   *
   *  ``author_provider_id`` is the ACoAAA-form LinkedIn member URN —
   *  required server-side to construct a real @-mention (not just plain
   *  text) when Yair replies back to this person. When null, the reply
   *  flow falls through to plain-text rendering and shows a hint in the
   *  composer that the mention won't tag. */
  latest_my_comment_replies_thread: Array<{
    comment_id: string;
    author_name: string | null;
    author_provider_id: string | null;
    text: string;
    published_at: string | null;
  }>;
  /** Replies we've posted back to specific incoming replies, captured at
   *  send time so the UI shows them immediately without waiting for the
   *  next engagement-refresh cycle. Each entry pairs to one incoming
   *  reply via `parent_reply_comment_id`. */
  my_outgoing_replies: Array<{
    comment_id: string | null;        // null on dry-run / failure
    parent_reply_comment_id: string;
    text: string;
    posted_at: string | null;
    dry_run: boolean;
    error?: string | null;
  }>;
  engagement_snapshots_count: number;
};

export type ManualCommentCampaign = {
  id: string;
  name: string;
  source_filename: string | null;
  n_rows: number;
  n_dry_run: number;
  n_posted: number;
  n_failed: number;
  created_at: string;
  updated_at: string;
  last_send_attempt_at: string | null;
  last_send_was_dry_run: boolean | null;
};

export type CampaignDetail = {
  campaign: ManualCommentCampaign;
  jobs: ManualCommentJob[];
};

export type AccountOption = {
  id: string;
  name: string;
  type: string;
  status: string;
};

export const manualCommentsApi = {
  /** Fetch the list of LinkedIn Unipile accounts available on the tenant. */
  accounts: () =>
    api.get<{ accounts: AccountOption[] }>("/api/manual-comments/accounts"),

  /** Upload a CSV with (post_url, comment) columns. Creates a campaign + jobs. */
  upload: async (file: File, name?: string): Promise<CampaignDetail> => {
    const form = new FormData();
    form.append("file", file);
    if (name && name.trim()) form.append("name", name.trim());
    const res = await fetch("/api/manual-comments/upload", {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new Error(
        (data?.detail as string) || `Upload failed (${res.status})`,
      );
    }
    return res.json();
  },

  /** List the operator's campaigns. */
  campaigns: () =>
    api.get<{ campaigns: ManualCommentCampaign[] }>(
      "/api/manual-comments/campaigns",
    ),

  /** Fetch one campaign + every job inside it. */
  campaign: (id: string) =>
    api.get<CampaignDetail>(
      `/api/manual-comments/campaigns/${encodeURIComponent(id)}`,
    ),

  /**
   * Post the campaign. SAFETY: `dry_run` defaults to true on the server
   * side too — if the caller forgets the field, we land in dry-run mode.
   * To actually post via Unipile, set dry_run: false.
   */
  send: (
    id: string,
    body: { unipile_account_id: string; dry_run: boolean },
  ) =>
    api.post<{
      campaign_id: string;
      dry_run: boolean;
      n_total: number;
      n_posted: number;
      n_dry_run: number;
      n_failed: number;
      n_skipped: number;
    }>(
      `/api/manual-comments/campaigns/${encodeURIComponent(id)}/send`,
      body,
    ),

  /**
   * For each `status='posted'` job in the campaign, ask APIDirect for the
   * current post details and append a `{captured_at, likes, comments, shares}`
   * snapshot. Manual trigger only — no background polling.
   */
  refreshEngagement: (id: string) =>
    api.post<{
      campaign_id: string;
      n_refreshed: number;
      n_failed: number;
      n_skipped: number;
      /** True when LinkedIn rate-limited Yair's account during this refresh.
       *  Per-comment stats may be missing on some/all rows; post-level
       *  (likes/comments/shares from APIDirect) is unaffected. Wait ~30–60min
       *  before re-clicking refresh to let the LinkedIn cooldown clear. */
      provider_rate_limited: boolean;
    }>(
      `/api/manual-comments/campaigns/${encodeURIComponent(id)}/refresh-engagement`,
    ),

  /**
   * Delete a previously-posted comment via Unipile.
   *
   * IMPORTANT: Unipile does NOT expose comment deletion in their public
   * API (probed 11 URL+method combinations on 2026-05-13, all return
   * router-level 404). This endpoint will respond 501 Not Implemented.
   * Use `markJobDeleted` instead after deleting on LinkedIn manually.
   *
   * Kept here for future use if Unipile ever ships the endpoint.
   */
  deleteJobComment: (jobId: string) =>
    api.post<{
      job_id: string;
      deleted: boolean;
      comment_id: string | null;
      error: string | null;
    }>(`/api/manual-comments/jobs/${encodeURIComponent(jobId)}/delete-comment`),

  /**
   * Mark a job as `deleted` WITHOUT calling Unipile. Use after the
   * operator manually deleted the comment from LinkedIn (since Unipile
   * doesn't expose a delete endpoint). Updates Mongo state only;
   * preserves the original comment_id for audit.
   */
  markJobDeleted: (jobId: string) =>
    api.post<{
      job_id: string;
      deleted: boolean;
      comment_id: string | null;
      error: string | null;
    }>(`/api/manual-comments/jobs/${encodeURIComponent(jobId)}/mark-deleted`),

  /**
   * Post a SINGLE job (or dry-run validate it). Sibling of /campaigns/{id}/send
   * — same safety semantics (dry_run defaults to true) but scoped to one row
   * so the operator can review + fire comments one at a time. Refuses to
   * re-post a job already at status='posted'; delete first if you want to
   * resend.
   */
  sendSingleJob: (
    jobId: string,
    body: { unipile_account_id: string; dry_run: boolean },
  ) =>
    api.post<{
      job_id: string;
      status: "dry_run" | "posted" | "failed";
      dry_run: boolean;
      comment_id: string | null;
      posted_at: string | null;
      error: string | null;
    }>(`/api/manual-comments/jobs/${encodeURIComponent(jobId)}/send`, body),

  /**
   * Post a threaded reply to one of the incoming replies on Yair's comment.
   *
   * Threading: Yair's original comment has comment_id=A. Someone (e.g.
   * Tarpan) replied to A with comment_id=B. This posts Yair's response
   * to B via Unipile's POST /posts/{post_id}/comments with
   * `parent_comment_id=B` — same endpoint as a top-level comment, the
   * extra field is what makes it a reply-to-reply.
   *
   * Safety: dry_run defaults true on the server. Returns immediately
   * with the new comment_id (live) or a `dry_run: true` marker (dry-run).
   * Either way the reply lands in the job's `my_outgoing_replies` array
   * so the UI can show it inline next to the incoming reply it answers.
   */
  replyToReply: (
    jobId: string,
    replyCommentId: string,
    body: { text: string; unipile_account_id: string; dry_run: boolean },
  ) =>
    api.post<{
      job_id: string;
      parent_reply_comment_id: string;
      status: "dry_run" | "posted" | "failed";
      dry_run: boolean;
      new_comment_id: string | null;
      posted_at: string | null;
      error: string | null;
    }>(
      `/api/manual-comments/jobs/${encodeURIComponent(jobId)}/replies/${encodeURIComponent(replyCommentId)}/reply`,
      body,
    ),

  /**
   * Fetch the current invitation state for one (job, reply) pair.
   * `invitation === null` means no invite has ever been created for this
   * target — the UI should render the "Connect with note" button.
   */
  getInvite: (jobId: string, replyCommentId: string) =>
    api.get<GetInvitationResponse>(
      `/api/manual-comments/jobs/${encodeURIComponent(jobId)}/replies/${encodeURIComponent(replyCommentId)}/invite`,
    ),

  /**
   * Send (or dry-run validate) a LinkedIn connection request with a
   * manually-typed note to the author of a specific reply.
   *
   * Safety semantics mirror replyToReply: dry_run defaults TRUE on the
   * server, account-match is enforced (the invite goes from the same
   * Unipile account that posted Yair's comment), and a per-target unique
   * index in Mongo blocks double-invites. The note is operator-typed —
   * no LLM suggestion path exists by product decision.
   */
  sendInvite: (
    jobId: string,
    replyCommentId: string,
    body: { note_text: string; unipile_account_id: string; dry_run: boolean },
  ) =>
    api.post<SendInvitationResponse>(
      `/api/manual-comments/jobs/${encodeURIComponent(jobId)}/replies/${encodeURIComponent(replyCommentId)}/invite`,
      body,
    ),

  /**
   * Fetch the current DM state for one (job, reply) pair.
   * `dm === null` means no DM has ever been sent to this target — the
   * UI should render the "Send DM" button. Otherwise the response
   * shape carries the DmPublic so the UI can show a status badge.
   */
  getDm: (jobId: string, replyCommentId: string) =>
    api.get<GetDmResponse>(
      `/api/manual-comments/jobs/${encodeURIComponent(jobId)}/replies/${encodeURIComponent(replyCommentId)}/dm`,
    ),

  /**
   * Send (or dry-run validate) a first-touch DM to the author of a
   * specific reply. Used after the recipient accepts the connection
   * request — operator types the DM body manually, no LLM-suggested
   * text path (matches the operator's product decision for invites).
   *
   * Safety semantics mirror sendInvite: dry_run defaults TRUE on the
   * server, account-match is enforced (DM goes from the same Unipile
   * account that posted the comment + sent the invite), and a per-
   * target unique index in Mongo blocks duplicate DMs.
   */
  sendDm: (
    jobId: string,
    replyCommentId: string,
    body: { message_text: string; unipile_account_id: string; dry_run: boolean },
  ) =>
    api.post<SendDmResponse>(
      `/api/manual-comments/jobs/${encodeURIComponent(jobId)}/replies/${encodeURIComponent(replyCommentId)}/dm`,
      body,
    ),
};

// ─── Invitation types ───────────────────────────────────────────────────

/** Status vocabulary for a LinkedIn connection invite. Mirrors
 * backend `InvitationStatusValue`. */
export type InvitationStatus =
  | "dry_run"
  | "queued"
  | "sent"
  | "accepted"
  | "declined"
  | "withdrawn"
  | "failed";

export type InvitationPublic = {
  id: string;
  operator_id: string;
  source_job_id: string;
  source_reply_comment_id: string;
  target_provider_id: string;
  target_name: string;
  target_public_identifier: string | null;
  target_headline: string | null;
  /** Operator-typed note. Empty string is valid (note-less invite). */
  note_text: string;
  /** The pool account the invite was/will be sent from. */
  sent_via_account_id: string;
  status: InvitationStatus;
  /** Unipile's id — used by the status poller. */
  invitation_id: string | null;
  sent_at: string | null;
  accepted_at: string | null;
  status_polled_at: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type GetInvitationResponse = {
  job_id: string;
  parent_reply_comment_id: string;
  invitation: InvitationPublic | null;
};

export type SendInvitationResponse = {
  job_id: string;
  parent_reply_comment_id: string;
  target_provider_id: string;
  status: InvitationStatus;
  dry_run: boolean;
  invitation_id: string | null;
  sent_at: string | null;
  error: string | null;
};

/** LinkedIn-enforced cap for invitation notes. */
export const LINKEDIN_INVITE_NOTE_MAX_CHARS = 200;

// ─── DM types ─────────────────────────────────────────────────────────

/** Status vocabulary for a LinkedIn direct message (chat first-touch).
 * Mirrors backend `DmStatusValue`. Narrower than the invite vocabulary
 * because we don't poll for read/accept on DMs (operator watches the
 * chat thread directly). */
export type DmStatus =
  | "dry_run"
  | "queued"
  | "sent"
  | "failed";

export type DmPublic = {
  id: string;
  operator_id: string;
  source_job_id: string;
  source_reply_comment_id: string;
  target_provider_id: string;
  target_name: string;
  target_public_identifier: string | null;
  /** Operator-typed DM body. ≤1500 chars. */
  message_text: string;
  /** The pool account that did the send. */
  sent_via_account_id: string;
  status: DmStatus;
  /** Unipile's chat id — used for any follow-up sends on the same thread. */
  chat_id: string | null;
  /** Unipile's first-message id from the chat-creation response. */
  message_id: string | null;
  sent_at: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type GetDmResponse = {
  job_id: string;
  parent_reply_comment_id: string;
  dm: DmPublic | null;
};

export type SendDmResponse = {
  job_id: string;
  parent_reply_comment_id: string;
  target_provider_id: string;
  status: DmStatus;
  dry_run: boolean;
  chat_id: string | null;
  message_id: string | null;
  sent_at: string | null;
  error: string | null;
};

/** Local cap mirroring backend LINKEDIN_DM_TEXT_MAX_CHARS. UI uses
 * this for the textarea counter; backend enforces it again on the
 * Pydantic SendDmRequest. */
export const LINKEDIN_DM_TEXT_MAX_CHARS = 1500;
