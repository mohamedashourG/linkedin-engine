"use client";

/**
 * Manual-comment posting page.
 *
 * UX contract (operator-instructed 2026-05-13):
 *   - Upload a CSV with (post_url, comment) columns
 *   - Select which Unipile-connected LinkedIn account to post from
 *   - Validate via dry-run by default (NO real Unipile call). Toggle
 *     "Live posting" + confirm modal to send for real.
 *   - After live posting, manually refresh engagement (APIDirect call)
 *     to capture likes/comments/shares snapshots.
 *
 * Nothing posts to LinkedIn until the operator explicitly flips the
 * Live-posting toggle AND clicks the modal confirm. Default flow is
 * always dry-run.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  ExternalLink,
  FilePlus2,
  Loader2,
  RefreshCw,
  Send,
  ShieldAlert,
  Trash2,
  UserPlus,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import {
  manualCommentsApi,
  LINKEDIN_INVITE_NOTE_MAX_CHARS,
  type CampaignDetail,
  type InvitationPublic,
  type ManualCommentJob,
} from "@/lib/manual-comments";

/**
 * Strip the credentials/titles off a captured LinkedIn display name so the
 * reply pre-fill matches LinkedIn's native auto-mention shape.
 *
 *   "Tarpan Patel, MD, FACC, RPVI"  →  "Tarpan Patel"
 *   "Raj Khandwalla MD MA FACC"     →  "Raj Khandwalla"
 *   "Kamal Sewaralthahab, MD, NEP"  →  "Kamal Sewaralthahab"
 *
 * Strategy: split on the first comma (handles the majority — "name, CRED, CRED"),
 * then strip trailing space-uppercase-only credential tokens
 * ("MD MBA FACC") on what's left for the no-comma case.
 * Conservative — when in doubt we leave the name alone so the user can
 * edit it themselves in the textarea.
 */
function authorNameForMention(name: string | null | undefined): string {
  if (!name) return "";
  const noCreds = name.split(",")[0].trim();
  // After comma-strip, drop trailing all-uppercase tokens (credentials w/o
  // a separating comma). Stop the first time we hit a token that's mixed
  // case so we don't eat the surname.
  const parts = noCreds.split(/\s+/);
  while (parts.length > 1) {
    const last = parts[parts.length - 1];
    // 2-5 char all-uppercase = likely a credential like MD, MBA, PhD, FACC
    if (/^[A-Z]{2,5}$/.test(last)) {
      parts.pop();
      continue;
    }
    break;
  }
  return parts.join(" ").trim();
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * Small badge for a LinkedIn invitation's status. Same look as the row
 * status badge so they read consistently inside the engagement column.
 */
function InviteStatusBadge({ status }: { status: InvitationPublic["status"] }) {
  const map: Record<
    InvitationPublic["status"],
    { label: string; variant: "default" | "secondary" | "outline" | "destructive" }
  > = {
    dry_run: { label: "invite dry-run ✓", variant: "outline" },
    queued: { label: "invite queued", variant: "secondary" },
    sent: { label: "invite sent", variant: "default" },
    accepted: { label: "connected ✓", variant: "default" },
    declined: { label: "invite declined", variant: "outline" },
    withdrawn: { label: "invite withdrawn", variant: "outline" },
    failed: { label: "invite failed", variant: "destructive" },
  };
  const v = map[status] ?? map.queued;
  return <Badge variant={v.variant}>{v.label}</Badge>;
}

function StatusBadge({ status }: { status: ManualCommentJob["status"] }) {
  const map = {
    queued: { label: "queued", variant: "secondary" as const },
    dry_run: { label: "dry-run ✓", variant: "outline" as const },
    posted: { label: "posted ✓", variant: "default" as const },
    failed: { label: "failed", variant: "destructive" as const },
    deleted: { label: "deleted", variant: "outline" as const },
  };
  const v = map[status] ?? map.queued;
  return <Badge variant={v.variant}>{v.label}</Badge>;
}

/**
 * Per-reply LinkedIn-invite controls. Encapsulates the fetch of current
 * invitation state for one (job, reply) pair so each reply can render its
 * own badge / "Connect with note" button without the parent re-rendering
 * the whole campaign on every state change.
 *
 * Wiring:
 *  - Disabled when `replyHasProviderId` is false (without provider_id
 *    we have no recipient URN to invite).
 *  - Disabled when no account is selected in the page-level dropdown.
 *  - The "Connect" click opens the composer at the page level — the
 *    actual send + live-confirm flow lives in the parent so a single
 *    confirmation modal handles both reply and invite sends.
 */
function ReplyInviteControls({
  jobId,
  replyCommentId,
  replyAuthorName,
  replyHasProviderId,
  selectedAccount,
  livePostingEnabled,
  onOpenComposer,
  enabled,
}: {
  jobId: string;
  replyCommentId: string;
  replyAuthorName: string | null;
  replyHasProviderId: boolean;
  selectedAccount: string;
  livePostingEnabled: boolean;
  onOpenComposer: (args: {
    jobId: string;
    replyCommentId: string;
    replyAuthorName: string | null;
    existingNote: string;
  }) => void;
  /** Parent's auth context — when false the query is short-circuited so
   *  closed/unmounted rows don't spam the API. */
  enabled: boolean;
}) {
  const inviteQ = useQuery({
    queryKey: ["mc-invite", jobId, replyCommentId],
    queryFn: () => manualCommentsApi.getInvite(jobId, replyCommentId),
    enabled: enabled && replyHasProviderId,
    staleTime: 30_000,
  });
  const invite = inviteQ.data?.invitation ?? null;

  // If an open invite already exists, render its status badge. Operator
  // can still click "Edit note" to reopen the composer pre-filled with
  // the existing draft (useful when a failed invite needs a retry).
  const inviteOpen =
    invite &&
    (invite.status === "queued" ||
      invite.status === "sent" ||
      invite.status === "accepted");

  if (!replyHasProviderId) {
    // No URN captured for this reply author — invite endpoint requires
    // it, so the button is dead. Same defensive pattern the @-mention
    // logic uses elsewhere on this page.
    return (
      <span
        className="text-[10px] italic text-muted-foreground"
        title="Reply has no author URN captured — can't send an invite. Refresh engagement to retry."
      >
        invite unavailable
      </span>
    );
  }

  return (
    <div className="flex items-center gap-1">
      {invite && <InviteStatusBadge status={invite.status} />}
      <button
        type="button"
        onClick={() =>
          onOpenComposer({
            jobId,
            replyCommentId,
            replyAuthorName,
            existingNote: invite?.note_text || "",
          })
        }
        className="inline-flex items-center gap-0.5 text-[10px] uppercase tracking-wide text-emerald-700 hover:underline"
        title={
          !selectedAccount
            ? "Composer will open — pick an account in the modal to enable Send"
            : !livePostingEnabled
              ? "Dry-run available now; live send requires the page-level checkbox"
              : invite
                ? "Edit note (will create a new invite if the prior is non-open)"
                : "Send a LinkedIn connection request to this person"
        }
      >
        <UserPlus className="h-3 w-3" />
        {invite ? "Edit note" : "Connect"}
      </button>
    </div>
  );
}

export default function ManualCommentsPage() {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [campaignName, setCampaignName] = useState("");
  const [activeCampaignId, setActiveCampaignId] = useState<string | null>(null);
  const [selectedAccount, setSelectedAccount] = useState<string>("");
  const [livePostingEnabled, setLivePostingEnabled] = useState(false);
  const [confirmModalOpen, setConfirmModalOpen] = useState(false);

  // Reply-to-reply composer state.
  // - replyComposerFor: { jobId, replyCommentId } the user is replying to.
  //   `null` = composer closed. Only one reply composer open at a time —
  //   simplest UX, matches how LinkedIn's UI works.
  // - replyDraft: the text being typed.
  // - liveReplyConfirm: { jobId, replyCommentId, text } pending live send
  //   confirmation. Mirrors the per-row send modal pattern so live posting
  //   always passes through an explicit confirmation step.
  const [replyComposerFor, setReplyComposerFor] = useState<
    { jobId: string; replyCommentId: string } | null
  >(null);
  const [replyDraft, setReplyDraft] = useState("");
  const [liveReplyConfirm, setLiveReplyConfirm] = useState<
    { jobId: string; replyCommentId: string; text: string } | null
  >(null);

  // Connection-invite composer state.
  // Modeled after the reply composer/confirm pair so the safety pattern
  // matches: typed note + dry-run default + explicit live-confirm modal.
  //   inviteComposer: which reply we're inviting (modal-style overlay)
  //   inviteNoteDraft: the operator-typed note (≤200 chars)
  //   liveInviteConfirm: { jobId, replyCommentId, note_text } — pending
  //     live-send confirmation. Same flow as liveReplyConfirm.
  const [inviteComposer, setInviteComposer] = useState<
    { jobId: string; replyCommentId: string; replyAuthorName: string | null } | null
  >(null);
  const [inviteNoteDraft, setInviteNoteDraft] = useState("");
  const [liveInviteConfirm, setLiveInviteConfirm] = useState<
    { jobId: string; replyCommentId: string; note_text: string } | null
  >(null);

  // Unipile accounts dropdown source
  const accountsQ = useQuery({
    queryKey: ["mc-accounts"],
    queryFn: manualCommentsApi.accounts,
    staleTime: 60_000,
  });

  // Operator's campaigns (left rail)
  const campaignsQ = useQuery({
    queryKey: ["mc-campaigns"],
    queryFn: manualCommentsApi.campaigns,
  });

  // Active campaign detail (right pane)
  const detailQ = useQuery({
    queryKey: ["mc-campaign", activeCampaignId],
    queryFn: () => manualCommentsApi.campaign(activeCampaignId!),
    enabled: !!activeCampaignId,
  });

  // CSV upload
  const uploadMut = useMutation({
    mutationFn: () => manualCommentsApi.upload(file!, campaignName),
    onSuccess: (res: CampaignDetail) => {
      toast.success(
        `Parsed ${res.campaign.n_rows} row(s). No posting happened — pick an account and dry-run when you're ready.`,
      );
      setActiveCampaignId(res.campaign.id);
      setFile(null);
      setCampaignName("");
      qc.invalidateQueries({ queryKey: ["mc-campaigns"] });
    },
    onError: (err: ApiError | Error) =>
      toast.error(err?.message || "Upload failed"),
  });

  // Send (dry-run by default, live posting only after confirm modal)
  const sendMut = useMutation({
    mutationFn: (vars: { dry_run: boolean }) =>
      manualCommentsApi.send(activeCampaignId!, {
        unipile_account_id: selectedAccount,
        dry_run: vars.dry_run,
      }),
    onSuccess: (res) => {
      if (res.dry_run) {
        toast.success(
          `Dry-run complete: ${res.n_dry_run} validated, ${res.n_failed} failed. No Unipile calls made.`,
        );
      } else {
        toast.success(
          `Live posting complete: ${res.n_posted} posted, ${res.n_failed} failed.`,
        );
      }
      qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
      qc.invalidateQueries({ queryKey: ["mc-campaigns"] });
      setConfirmModalOpen(false);
    },
    onError: (err: ApiError | Error) =>
      toast.error(err?.message || "Send failed"),
  });

  // Engagement refresh (fire-and-forget background task on the backend).
  // The route returns instantly so we never hit a proxy timeout on slow
  // APIDirect / Unipile calls — the actual refresh runs detached and
  // snapshots stream into Mongo over the next ~30s. We auto-poll the
  // campaign detail until the latest snapshot timestamp on every row has
  // advanced past the refresh-start time, so the UI shows the new
  // numbers without the operator clicking again.
  const refreshMut = useMutation({
    mutationFn: () => manualCommentsApi.refreshEngagement(activeCampaignId!),
    onSuccess: () => {
      toast.success(
        "Refresh started — snapshots will update over the next ~30s. Numbers refresh automatically.",
        { duration: 6000 },
      );
      // Trigger a faster repoll cadence for the next minute by invalidating
      // immediately + after a few delays so the UI catches the snapshots
      // as they land. Each tick is cheap (single Mongo read) and stops
      // automatically when the user navigates away.
      qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
      for (const t of [5_000, 12_000, 25_000, 45_000]) {
        setTimeout(() => {
          qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
        }, t);
      }
    },
    onError: (err: ApiError | Error) =>
      toast.error(err?.message || "Engagement refresh failed"),
  });

  // Per-row send. Same dry-run-default semantics as campaign-wide /send.
  // Operator clicks "Dry-run this row" or, after ticking the live-posting
  // checkbox above, "Send this row live" → confirmation modal.
  const [liveSendJobId, setLiveSendJobId] = useState<string | null>(null);
  const sendSingleMut = useMutation({
    mutationFn: (vars: { jobId: string; dry_run: boolean }) =>
      manualCommentsApi.sendSingleJob(vars.jobId, {
        unipile_account_id: selectedAccount,
        dry_run: vars.dry_run,
      }),
    onSuccess: (res) => {
      if (res.status === "dry_run") {
        toast.success(`Row dry-run ✓ — no Unipile call made.`);
      } else if (res.status === "posted") {
        toast.success(
          `Row posted ✓  comment_id=${(res.comment_id || "").slice(0, 24)}…`,
        );
      } else if (res.status === "failed") {
        toast.error(res.error || "Unipile rejected this row");
      }
      setLiveSendJobId(null);
      qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
      qc.invalidateQueries({ queryKey: ["mc-campaigns"] });
    },
    onError: async (err: ApiError | Error) => {
      // ── Treat ambiguous failures as ambiguous ──────────────────────
      // A 500/504 from the Next proxy or a network blip doesn't mean
      // the comment didn't post — it could mean the proxy timed out
      // while Unipile was still completing. Refetch the campaign
      // before showing the error so the row's actual server-side
      // status (which may already be "posted") is visible to the
      // user. Then either:
      //   - status==='posted'  → show success, no retry needed
      //   - status==='posting' → tell them to wait, don't retry
      //   - other              → show the actual error and allow retry
      const status = (err as ApiError)?.status;
      const ambiguous =
        status === undefined ||           // network / TypeError fetch failed
        status === 0 ||
        status === 408 ||                 // upstream timeout
        status === 502 ||
        status === 503 ||
        status === 504 ||
        status === 500;                   // Next proxy timeout surfaces as 500
      if (ambiguous && activeCampaignId) {
        // refetchQueries (not invalidateQueries) returns a Promise that
        // resolves after the fresh data lands in the cache — otherwise
        // we'd read stale state below and miss the "actually posted"
        // case the user just hit.
        await qc.refetchQueries({ queryKey: ["mc-campaign", activeCampaignId] });
        const fresh = qc.getQueryData<CampaignDetail | undefined>([
          "mc-campaign",
          activeCampaignId,
        ]);
        const row = fresh?.jobs.find((x) => x.id === liveSendJobId);
        if (row?.status === "posted") {
          toast.success(
            `Row already posted ✓  comment_id=${(row.comment_id || "").slice(0, 24)}… ` +
              `(the request appeared to fail but Unipile actually received it — no duplicate sent).`,
          );
          setLiveSendJobId(null);
          return;
        }
        if (row?.status === "posting") {
          toast.warning(
            "Request appeared to fail but the comment may still be in flight. " +
              "Wait 10-15s then refresh the page — DO NOT click Send again, or you'll race a duplicate.",
          );
          setLiveSendJobId(null);
          return;
        }
      }
      // Real failure (4xx other than 408, or the row really didn't post).
      toast.error(err?.message || "Send failed");
      setLiveSendJobId(null);
    },
  });

  // "Mark as deleted" — local-only state flip. Unipile does NOT expose
  // a comment-delete endpoint (verified via 11 URL/method probes on
  // 2026-05-13, all 404), so the workflow is: operator clicks "Open on
  // LinkedIn" → deletes the comment manually in the LinkedIn UI →
  // returns to our UI and clicks "Mark as deleted" to update our records.
  const [markDeletedJobId, setMarkDeletedJobId] = useState<string | null>(null);
  const markDeletedMut = useMutation({
    mutationFn: (jobId: string) => manualCommentsApi.markJobDeleted(jobId),
    onSuccess: (res) => {
      toast.success(
        `Marked as deleted in our records. ` +
          (res.comment_id
            ? `LinkedIn comment_id ${res.comment_id.slice(0, 18)}… preserved for audit.`
            : ""),
      );
      setMarkDeletedJobId(null);
      qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
      qc.invalidateQueries({ queryKey: ["mc-campaigns"] });
    },
    onError: (err: ApiError | Error) => {
      toast.error(err?.message || "Failed to mark as deleted");
      setMarkDeletedJobId(null);
    },
  });

  // Reply-to-reply mutation. Used by both the dry-run flow (direct click)
  // and the live flow (after passing through the liveReplyConfirm modal).
  // onSuccess closes the composer and invalidates the campaign query so
  // the inline `my_outgoing_replies` entry shows up immediately.
  const replyToReplyMut = useMutation({
    mutationFn: (vars: {
      jobId: string;
      replyCommentId: string;
      text: string;
      dry_run: boolean;
    }) =>
      manualCommentsApi.replyToReply(vars.jobId, vars.replyCommentId, {
        text: vars.text,
        unipile_account_id: selectedAccount,
        dry_run: vars.dry_run,
      }),
    onSuccess: (res) => {
      if (res.status === "dry_run") {
        toast.success("Reply dry-run ✓ — no Unipile call made. Tick the live-posting checkbox to actually send.");
      } else if (res.status === "posted") {
        toast.success(
          `Reply posted ✓  comment_id=${(res.new_comment_id || "").slice(0, 24)}…`,
        );
      } else if (res.status === "failed") {
        toast.error(res.error || "Unipile rejected this reply");
      }
      // Close composer + clear modal + refetch campaign so the new
      // outgoing reply shows in the thread.
      setReplyComposerFor(null);
      setReplyDraft("");
      setLiveReplyConfirm(null);
      qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
    },
    onError: async (err: ApiError | Error, vars) => {
      // Mirrors sendSingleMut.onError — refetch on ambiguous failures
      // and check if the reply actually landed (backend's dedup window
      // means a follow-up with same parent+text in the next 10min will
      // return the existing comment_id, but the SAFEST UX is to surface
      // the actual state and prevent the user from clicking Send again).
      const status = (err as ApiError)?.status;
      const ambiguous =
        status === undefined ||
        status === 0 ||
        status === 408 ||
        status === 500 ||
        status === 502 ||
        status === 503 ||
        status === 504;
      if (ambiguous && activeCampaignId) {
        await qc.refetchQueries({ queryKey: ["mc-campaign", activeCampaignId] });
        const fresh = qc.getQueryData<CampaignDetail | undefined>([
          "mc-campaign",
          activeCampaignId,
        ]);
        const row = fresh?.jobs.find((x) => x.id === vars.jobId);
        const landed = (row?.my_outgoing_replies || []).find(
          (r) =>
            r.parent_reply_comment_id === vars.replyCommentId &&
            r.text === vars.text &&
            r.dry_run !== true &&
            r.comment_id,
        );
        if (landed) {
          toast.success(
            `Reply already posted ✓  comment_id=${(landed.comment_id || "").slice(0, 24)}… ` +
              `(the request appeared to fail but Unipile actually received it — no duplicate sent).`,
          );
          setReplyComposerFor(null);
          setReplyDraft("");
          setLiveReplyConfirm(null);
          return;
        }
      }
      toast.error(err?.message || "Reply failed");
      setLiveReplyConfirm(null);
    },
  });

  // Connection-invite mutation. Same shape as replyToReplyMut: dry_run
  // defaults true on the server, live send always passes through the
  // liveInviteConfirm modal. onSuccess invalidates BOTH the per-reply
  // invite query (so the inline badge updates immediately) and the
  // campaign query (so any future field on the job doc that surfaces
  // invite state stays consistent).
  const sendInviteMut = useMutation({
    mutationFn: (vars: {
      jobId: string;
      replyCommentId: string;
      note_text: string;
      dry_run: boolean;
    }) =>
      manualCommentsApi.sendInvite(vars.jobId, vars.replyCommentId, {
        note_text: vars.note_text,
        unipile_account_id: selectedAccount,
        dry_run: vars.dry_run,
      }),
    onSuccess: (res, vars) => {
      if (res.status === "dry_run") {
        toast.success(
          "Invite dry-run ✓ — no Unipile call made. Flip live-posting on to actually send the connection request.",
        );
      } else if (res.status === "sent" || res.status === "queued") {
        toast.success(
          `Connection invite sent ✓  invitation_id=${(res.invitation_id || "").slice(0, 24)}…`,
        );
      } else if (res.status === "failed") {
        toast.error(res.error || "Unipile rejected this invite");
      } else if (res.status === "accepted") {
        toast.success("Already connected ✓");
      }
      setInviteComposer(null);
      setInviteNoteDraft("");
      setLiveInviteConfirm(null);
      qc.invalidateQueries({
        queryKey: ["mc-invite", vars.jobId, vars.replyCommentId],
      });
      qc.invalidateQueries({ queryKey: ["mc-campaign", activeCampaignId] });
    },
    onError: (err: ApiError | Error) => {
      toast.error(err?.message || "Invite failed");
      setLiveInviteConfirm(null);
    },
  });

  const accounts = accountsQ.data?.accounts ?? [];
  const campaigns = campaignsQ.data?.campaigns ?? [];
  const detail = detailQ.data;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Manual comments</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Upload a CSV of (post_url, comment) rows, pick a Unipile account, dry-run
          validate, then optionally post for real.{" "}
          <strong>Default is dry-run — no comment posts until you flip the switch.</strong>
        </p>
      </div>

      {/* Upload */}
      <section className="rounded-lg border bg-card p-5">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          1. Upload CSV
        </h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Required columns: <code>post_url</code> (or <code>comment_link_post_url</code>) and{" "}
          <code>comment</code> (or <code>comment_text</code> / <code>drafted_comment</code>). Cap: 200 rows
          per file, 1,200 chars per comment.
        </p>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <label className="mb-1 block text-xs text-muted-foreground">
              CSV file
            </label>
            <Input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs text-muted-foreground">
              Campaign name (optional)
            </label>
            <Input
              type="text"
              placeholder="e.g. Cardiowell CLEAN drafts — May 13"
              value={campaignName}
              onChange={(e) => setCampaignName(e.target.value)}
            />
          </div>
          <Button
            onClick={() => uploadMut.mutate()}
            disabled={!file || uploadMut.isPending}
          >
            <FilePlus2 className="mr-2 h-4 w-4" />
            {uploadMut.isPending ? "Parsing…" : "Upload + parse"}
          </Button>
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
        {/* Campaign list */}
        <section className="rounded-lg border bg-card p-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Campaigns
          </h2>
          {campaignsQ.isLoading ? (
            <Skeleton className="mt-3 h-16 w-full" />
          ) : campaigns.length === 0 ? (
            <p className="mt-3 text-sm text-muted-foreground">
              No campaigns yet. Upload a CSV above.
            </p>
          ) : (
            <ul className="mt-3 space-y-1">
              {campaigns.map((c) => (
                <li key={c.id}>
                  <button
                    onClick={() => setActiveCampaignId(c.id)}
                    className={`w-full rounded-md p-2 text-left text-sm hover:bg-muted ${
                      activeCampaignId === c.id ? "bg-muted font-semibold" : ""
                    }`}
                  >
                    <div className="truncate">{c.name}</div>
                    <div className="text-xs text-muted-foreground">
                      {c.n_rows} row(s) · {fmtDate(c.created_at)}
                    </div>
                    {(c.n_posted > 0 || c.n_dry_run > 0 || c.n_failed > 0) && (
                      <div className="mt-1 flex flex-wrap gap-1 text-xs">
                        {c.n_posted > 0 && (
                          <span className="rounded bg-green-100 px-1.5 py-0.5 text-green-800">
                            {c.n_posted} posted
                          </span>
                        )}
                        {c.n_dry_run > 0 && (
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
                            {c.n_dry_run} dry-run
                          </span>
                        )}
                        {c.n_failed > 0 && (
                          <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-800">
                            {c.n_failed} failed
                          </span>
                        )}
                      </div>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Campaign detail */}
        <section className="rounded-lg border bg-card p-5">
          {!activeCampaignId ? (
            <p className="text-sm text-muted-foreground">
              Pick a campaign from the left, or upload a new CSV above.
            </p>
          ) : detailQ.isLoading || !detail ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <>
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold">{detail.campaign.name}</h2>
                  <p className="text-xs text-muted-foreground">
                    {detail.campaign.n_rows} rows · uploaded {fmtDate(detail.campaign.created_at)}
                    {detail.campaign.last_send_attempt_at &&
                      ` · last attempt ${fmtDate(detail.campaign.last_send_attempt_at)} ${
                        detail.campaign.last_send_was_dry_run ? "(dry-run)" : "(LIVE)"
                      }`}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => refreshMut.mutate()}
                    disabled={refreshMut.isPending || detail.campaign.n_posted === 0}
                    title={
                      detail.campaign.n_posted === 0
                        ? "Nothing posted yet — engagement refresh has nothing to query"
                        : "Call APIDirect for each posted job and store a likes/comments/shares snapshot"
                    }
                  >
                    <RefreshCw
                      className={`mr-2 h-3.5 w-3.5 ${
                        refreshMut.isPending ? "animate-spin" : ""
                      }`}
                    />
                    Refresh engagement
                  </Button>
                </div>
              </div>

              {/* Step 2: pick account */}
              <div className="mt-5 rounded-md border bg-muted/30 p-4">
                <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                  2. Pick a Unipile account to post from
                </h3>
                {accountsQ.isLoading ? (
                  <Skeleton className="mt-3 h-9 w-full" />
                ) : accounts.length === 0 ? (
                  <p className="mt-2 text-sm text-muted-foreground">
                    No Unipile-connected LinkedIn accounts found on this tenant.
                  </p>
                ) : (
                  <select
                    className="mt-2 w-full rounded-md border bg-background p-2 text-sm"
                    value={selectedAccount}
                    onChange={(e) => setSelectedAccount(e.target.value)}
                  >
                    <option value="">— select account —</option>
                    {accounts.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}  ·  {a.id.slice(0, 12)}…  ·  {a.status}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              {/* Step 3: send */}
              <div className="mt-5 rounded-md border bg-muted/30 p-4">
                <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                  3. Send
                </h3>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <Button
                    variant="secondary"
                    onClick={() => sendMut.mutate({ dry_run: true })}
                    disabled={!selectedAccount || sendMut.isPending}
                    title="Validate URLs + comment lengths, mark each row status='dry_run', NO Unipile call"
                  >
                    {sendMut.isPending && sendMut.variables?.dry_run === true ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <CheckCircle2 className="mr-2 h-4 w-4" />
                    )}
                    Dry-run validate
                  </Button>

                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={livePostingEnabled}
                      onChange={(e) => setLivePostingEnabled(e.target.checked)}
                      className="h-4 w-4"
                    />
                    <span>
                      <strong>I want to actually post</strong> to LinkedIn (turns on the live-send button)
                    </span>
                  </label>

                  <Button
                    variant="destructive"
                    onClick={() => setConfirmModalOpen(true)}
                    disabled={
                      !selectedAccount ||
                      !livePostingEnabled ||
                      sendMut.isPending
                    }
                    title={
                      !livePostingEnabled
                        ? "Tick the checkbox above to unlock live posting"
                        : "Open the confirmation modal to actually post via Unipile"
                    }
                  >
                    <Send className="mr-2 h-4 w-4" />
                    Send live via Unipile
                  </Button>
                </div>
              </div>

              {/* Jobs table */}
              <div className="mt-6">
                <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                  Rows ({detail.jobs.length})
                </h3>
                <div className="mt-2 overflow-x-auto rounded-lg border">
                  <table className="w-full min-w-[900px] text-sm">
                    <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 text-left">#</th>
                        <th className="px-3 py-2 text-left">Status</th>
                        <th className="px-3 py-2 text-left">Post</th>
                        <th className="px-3 py-2 text-left">Comment</th>
                        <th className="px-3 py-2 text-left">Engagement</th>
                        <th className="px-3 py-2 text-left">Posted at</th>
                        <th className="px-3 py-2 text-left">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.jobs.map((j) => (
                        <tr key={j.id} className="border-t align-top">
                          <td className="px-3 py-2 tabular-nums text-muted-foreground">
                            {j.row_index + 1}
                          </td>
                          <td className="px-3 py-2">
                            <StatusBadge status={j.status} />
                            {j.error && (
                              <div className="mt-1 text-xs text-destructive">
                                {j.error}
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-2">
                            <a
                              href={j.post_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                            >
                              <span className="max-w-[280px] truncate">
                                {j.post_url}
                              </span>
                              <ExternalLink className="h-3 w-3 shrink-0" />
                            </a>
                            {j.post_id && (
                              <div className="mt-0.5 text-xs text-muted-foreground">
                                post_id {j.post_id.slice(0, 26)}…
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-2">
                            {/* whitespace-pre-wrap preserves \n the operator
                                included in the CSV, so multi-line comments
                                render as the LinkedIn comment will look.
                                The wrapping `group` div + the absolutely-
                                positioned `group-hover` panel below give us
                                a custom hover tooltip that shows the full
                                multi-line comment (the native `title` attribute
                                strips newlines and is unreliable). */}
                            <div className="group relative">
                              <div
                                className="max-w-[420px] cursor-help whitespace-pre-wrap text-xs"
                                style={{
                                  display: "-webkit-box",
                                  WebkitLineClamp: 6,
                                  WebkitBoxOrient: "vertical",
                                  overflow: "hidden",
                                }}
                              >
                                {j.draft_comment}
                              </div>
                              {/* Full-text hover popover.
                                  - invisible/opacity-0 by default, fades in on group-hover
                                  - z-40 so it sits above table rows and borders
                                  - pointer-events-none so it never blocks clicks below
                                  - max-h with overflow-auto in case the operator
                                    pasted a very long comment */}
                              <div
                                className="pointer-events-none invisible absolute left-0 top-full z-40 mt-1 max-h-96 w-[min(560px,calc(100vw-3rem))] overflow-auto rounded-md border bg-card p-3 text-xs text-card-foreground opacity-0 shadow-lg transition-opacity duration-100 group-hover:visible group-hover:opacity-100"
                                role="tooltip"
                              >
                                <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                                  Full comment ({j.draft_comment.length} chars
                                  {j.draft_comment.includes("\n") &&
                                    ` · ${j.draft_comment.split("\n").length} line(s)`}
                                  )
                                </div>
                                <div className="whitespace-pre-wrap leading-relaxed">
                                  {j.draft_comment}
                                </div>
                              </div>
                            </div>
                            <div className="mt-0.5 text-[10px] text-muted-foreground">
                              {j.draft_comment.length} chars
                              {j.draft_comment.includes("\n") &&
                                ` · ${j.draft_comment.split("\n").length} line(s)`}
                              <span className="ml-1 italic">· hover to see full</span>
                            </div>
                          </td>
                          <td className="px-3 py-2">
                            {j.engagement_snapshots_count > 0 ? (
                              <div className="space-y-1 text-xs">
                                <div>
                                  <div className="font-semibold text-muted-foreground">
                                    On my comment
                                  </div>
                                  <div>
                                    👍{" "}
                                    {j.latest_my_comment_reactions ?? "—"}
                                  </div>
                                  <div>
                                    💬 {j.latest_my_comment_replies ?? "—"} reply(ies)
                                  </div>
                                </div>
                                {/* Reply thread — author name + text, no
                                    profile-view links (product decision).
                                    Rendered only when we actually have
                                    captured replies.

                                    Each incoming reply gets a "Reply"
                                    button. Click → inline textarea appears
                                    below the reply. Any of Yair's outgoing
                                    replies to THIS specific reply (via
                                    parent_reply_comment_id) render
                                    underneath the source reply, indented,
                                    so the thread structure is obvious
                                    without leaving the page. */}
                                {(() => {
                                  // Unipile's get_comment_replies returns EVERY comment threaded
                                  // under our top-level comment — including the replies WE
                                  // posted (reply-to-reply). Without filtering they'd show up
                                  // twice: once as a fake "incoming reply" in the blue thread,
                                  // and once correctly under the green "You" outgoing-reply
                                  // panel. Dedupe by comment_id against my_outgoing_replies.
                                  const myOutgoingCids = new Set(
                                    (j.my_outgoing_replies || [])
                                      .map((o) => o.comment_id)
                                      .filter((cid): cid is string => !!cid),
                                  );
                                  // Sort chronologically so the thread reads top-to-bottom in
                                  // the order it happened (matches LinkedIn's native UX).
                                  const cleanedThread = (j.latest_my_comment_replies_thread || [])
                                    .filter((r) => !myOutgoingCids.has(r.comment_id))
                                    .slice()
                                    .sort((a, b) => {
                                      const ta = a.published_at ? new Date(a.published_at).getTime() : 0;
                                      const tb = b.published_at ? new Date(b.published_at).getTime() : 0;
                                      return ta - tb;
                                    });
                                  if (cleanedThread.length === 0) return null;
                                  return (
                                    <div className="mt-1 space-y-1.5 border-l-2 border-blue-300 pl-2">
                                      {cleanedThread.map((r) => {
                                        const outgoingForThis = (j.my_outgoing_replies || [])
                                          .filter((o) => o.parent_reply_comment_id === r.comment_id);
                                        const composerOpen =
                                          replyComposerFor?.jobId === j.id &&
                                          replyComposerFor?.replyCommentId === r.comment_id;
                                        return (
                                          <div key={r.comment_id} className="space-y-1">
                                            <div className="rounded bg-blue-50/50 p-1.5">
                                              <div className="flex items-center justify-between gap-2">
                                                <div className="font-medium text-foreground">
                                                  {r.author_name || "Unknown"}
                                                </div>
                                                <div className="flex items-center gap-2">
                                                <ReplyInviteControls
                                                  jobId={j.id}
                                                  replyCommentId={r.comment_id}
                                                  replyAuthorName={r.author_name}
                                                  replyHasProviderId={!!r.author_provider_id}
                                                  selectedAccount={selectedAccount}
                                                  livePostingEnabled={livePostingEnabled}
                                                  enabled={!!activeCampaignId}
                                                  onOpenComposer={({ jobId, replyCommentId, replyAuthorName, existingNote }) => {
                                                    setInviteComposer({ jobId, replyCommentId, replyAuthorName });
                                                    setInviteNoteDraft(existingNote);
                                                  }}
                                                />
                                                <button
                                                  type="button"
                                                  onClick={() => {
                                                    if (composerOpen) {
                                                      setReplyComposerFor(null);
                                                      setReplyDraft("");
                                                    } else {
                                                      setReplyComposerFor({
                                                        jobId: j.id,
                                                        replyCommentId: r.comment_id,
                                                      });
                                                      // Pre-fill with the author's clean name (no
                                                      // credentials), matching what LinkedIn auto-
                                                      // inserts when you click Reply natively. The
                                                      // trailing space gives the cursor a clean
                                                      // landing spot to start typing.
                                                      const mention = authorNameForMention(r.author_name);
                                                      setReplyDraft(mention ? `${mention} ` : "");
                                                    }
                                                  }}
                                                  className="text-[10px] uppercase tracking-wide text-blue-700 hover:underline"
                                                >
                                                  {composerOpen ? "Cancel" : "Reply"}
                                                </button>
                                                </div>
                                              </div>
                                              <div className="whitespace-pre-wrap text-muted-foreground">
                                                {r.text}
                                              </div>
                                              {r.published_at && (
                                                <div className="mt-0.5 text-[10px] text-muted-foreground">
                                                  {fmtDate(r.published_at)}
                                                </div>
                                              )}
                                            </div>

                                            {/* Outgoing replies WE'VE posted under THIS incoming reply.
                                                Indented one notch so the thread structure is visually
                                                obvious. Each shows the text + a marker for dry-run vs
                                                live + posted timestamp. */}
                                            {outgoingForThis.map((o, oi) => (
                                              <div
                                                key={o.comment_id || `dry-${oi}`}
                                                className="ml-3 rounded border-l-2 border-emerald-400 bg-emerald-50/60 p-1.5"
                                              >
                                                <div className="flex items-center justify-between gap-2">
                                                  <div className="font-medium text-foreground">
                                                    You {o.dry_run && (
                                                      <span className="ml-1 rounded bg-slate-200 px-1 py-0.5 text-[9px] uppercase text-slate-700">
                                                        dry-run
                                                      </span>
                                                    )}
                                                    {o.error && (
                                                      <span className="ml-1 rounded bg-red-200 px-1 py-0.5 text-[9px] uppercase text-red-800">
                                                        failed
                                                      </span>
                                                    )}
                                                  </div>
                                                  {o.posted_at && (
                                                    <div className="text-[10px] text-muted-foreground">
                                                      {fmtDate(o.posted_at)}
                                                    </div>
                                                  )}
                                                </div>
                                                <div className="whitespace-pre-wrap text-muted-foreground">
                                                  {o.text}
                                                </div>
                                                {o.error && (
                                                  <div className="mt-1 text-[10px] text-destructive">
                                                    {o.error}
                                                  </div>
                                                )}
                                              </div>
                                            ))}

                                            {/* Inline composer for this specific reply. Same safety
                                                pattern as posting a top-level comment: dry-run by
                                                default, live-send requires the page-level checkbox AND
                                                routes through the liveReplyConfirm modal. */}
                                            {composerOpen && (
                                              <div className="ml-3 space-y-1 rounded border bg-card p-2">
                                                <textarea
                                                  value={replyDraft}
                                                  onChange={(e) => setReplyDraft(e.target.value.slice(0, 1200))}
                                                  placeholder={`Reply to ${r.author_name || "this person"}…`}
                                                  className="w-full resize-y rounded border bg-background p-1.5 text-xs"
                                                  rows={3}
                                                  autoFocus
                                                />
                                                {/* Hint row — sits on its own line above the
                                                    buttons so it doesn't get squeezed when the
                                                    engagement column is narrow. */}
                                                <div className="text-[10px] text-muted-foreground">
                                                  <span className="tabular-nums">
                                                    {replyDraft.length} / 1200 chars
                                                  </span>
                                                  {(() => {
                                                    const mention = authorNameForMention(r.author_name);
                                                    const willTag =
                                                      !!r.author_provider_id &&
                                                      !!mention &&
                                                      replyDraft.startsWith(mention);
                                                    if (!r.author_provider_id) {
                                                      return (
                                                        <span className="ml-2 italic">
                                                          · @-tag unavailable
                                                        </span>
                                                      );
                                                    }
                                                    return willTag ? (
                                                      <span className="ml-2 italic text-emerald-700">
                                                        · @{mention} will tag ✓
                                                      </span>
                                                    ) : (
                                                      <span className="ml-2 italic text-amber-700">
                                                        · plain text (name edited)
                                                      </span>
                                                    );
                                                  })()}
                                                </div>
                                                {/* Action row — flat-left, full-width on narrow
                                                    columns. Warnings render BELOW so they don't
                                                    wrap vertically next to buttons. */}
                                                <div className="flex flex-wrap gap-1">
                                                  <Button
                                                    size="sm"
                                                    variant="secondary"
                                                    disabled={
                                                      !selectedAccount ||
                                                      !replyDraft.trim() ||
                                                      replyToReplyMut.isPending
                                                    }
                                                    onClick={() =>
                                                      replyToReplyMut.mutate({
                                                        jobId: j.id,
                                                        replyCommentId: r.comment_id,
                                                        text: replyDraft.trim(),
                                                        dry_run: true,
                                                      })
                                                    }
                                                    title="Validate this reply text without calling Unipile"
                                                  >
                                                    <CheckCircle2 className="mr-1 h-3 w-3" />
                                                    Dry-run
                                                  </Button>
                                                  <Button
                                                    size="sm"
                                                    variant="destructive"
                                                    disabled={
                                                      !selectedAccount ||
                                                      !livePostingEnabled ||
                                                      !replyDraft.trim() ||
                                                      replyToReplyMut.isPending
                                                    }
                                                    onClick={() =>
                                                      setLiveReplyConfirm({
                                                        jobId: j.id,
                                                        replyCommentId: r.comment_id,
                                                        text: replyDraft.trim(),
                                                      })
                                                    }
                                                    title={
                                                      !livePostingEnabled
                                                        ? "Tick 'I want to actually post' above to unlock"
                                                        : "Post this reply via Unipile"
                                                    }
                                                  >
                                                    <Send className="mr-1 h-3 w-3" />
                                                    Send reply
                                                  </Button>
                                                </div>
                                                {!selectedAccount && (
                                                  <div className="text-[10px] text-amber-700">
                                                    ⚠ pick an account above first
                                                  </div>
                                                )}
                                                {!livePostingEnabled && selectedAccount && (
                                                  <div className="text-[10px] text-amber-700">
                                                    ⚠ tick &quot;I want to actually post&quot; above to enable live send
                                                  </div>
                                                )}
                                              </div>
                                            )}
                                          </div>
                                        );
                                      })}
                                    </div>
                                  );
                                })()}
                                <div className="border-t pt-1">
                                  <div className="font-semibold text-muted-foreground">
                                    On the post (total)
                                  </div>
                                  <div>👍 {j.latest_likes ?? 0}</div>
                                  <div>💬 {j.latest_comments ?? 0}</div>
                                  <div>🔁 {j.latest_shares ?? 0}</div>
                                </div>
                                <div className="text-muted-foreground">
                                  {j.engagement_snapshots_count} snapshot(s)
                                </div>
                              </div>
                            ) : (
                              <span className="text-xs text-muted-foreground">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-xs text-muted-foreground">
                            {fmtDate(j.posted_at)}
                            {j.status === "deleted" && j.deleted_at && (
                              <div className="text-[10px] text-muted-foreground">
                                deleted {fmtDate(j.deleted_at)}
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-2">
                            {/* Per-row actions: dry-run, live-send, delete */}
                            <div className="flex flex-col gap-1">
                              {(j.status === "queued" ||
                                j.status === "dry_run" ||
                                j.status === "failed" ||
                                j.status === "deleted") && (
                                <>
                                  <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={() =>
                                      sendSingleMut.mutate({
                                        jobId: j.id,
                                        dry_run: true,
                                      })
                                    }
                                    disabled={
                                      !selectedAccount ||
                                      sendSingleMut.isPending
                                    }
                                    title="Validate this row only (no Unipile call)"
                                  >
                                    <CheckCircle2 className="mr-1 h-3.5 w-3.5" />
                                    Dry-run
                                  </Button>
                                  <Button
                                    variant="destructive"
                                    size="sm"
                                    onClick={() => setLiveSendJobId(j.id)}
                                    disabled={
                                      !selectedAccount ||
                                      !livePostingEnabled ||
                                      sendSingleMut.isPending
                                    }
                                    title={
                                      !livePostingEnabled
                                        ? "Tick 'I want to actually post' above to unlock"
                                        : "Post THIS row only via Unipile"
                                    }
                                  >
                                    <Send className="mr-1 h-3.5 w-3.5" />
                                    Send this row
                                  </Button>
                                </>
                              )}
                              {j.status === "posted" && (
                                <>
                                  {/* Unipile doesn't expose a DELETE endpoint
                                      for comments (vendor-side limitation).
                                      So: open LinkedIn, delete there manually,
                                      then come back and mark this row deleted. */}
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    asChild
                                    title="Open the post on LinkedIn to delete your comment manually (Unipile API doesn't expose deletion)"
                                  >
                                    <a
                                      href={j.post_url}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                    >
                                      <ExternalLink className="mr-1 h-3.5 w-3.5" />
                                      Open on LinkedIn
                                    </a>
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setMarkDeletedJobId(j.id)}
                                    title="After you delete the comment on LinkedIn, click this to update our records"
                                  >
                                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                                    Mark as deleted
                                  </Button>
                                </>
                              )}
                              {j.status === "deleted" && (
                                <span className="text-xs text-muted-foreground">
                                  ✓ deleted
                                </span>
                              )}
                            </div>
                            {j.delete_error && (
                              <div className="mt-1 max-w-[260px] break-words text-xs text-destructive">
                                {j.delete_error}
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </section>
      </div>

      {/* Live-send confirmation modal */}
      {confirmModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-w-md rounded-lg bg-card p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <ShieldAlert className="h-6 w-6 shrink-0 text-destructive" />
              <div>
                <h3 className="text-lg font-semibold">
                  Confirm: post for real?
                </h3>
                <p className="mt-2 text-sm text-muted-foreground">
                  This will call Unipile and actually post{" "}
                  <strong>
                    {detail?.jobs.filter((j) => j.status !== "posted").length ??
                      0}{" "}
                    comments
                  </strong>{" "}
                  to LinkedIn from the selected account{" "}
                  <strong>
                    {accounts.find((a) => a.id === selectedAccount)?.name}
                  </strong>
                  . Comments are not retractable from the engine — only via
                  LinkedIn UI per post.
                </p>
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setConfirmModalOpen(false)}
                disabled={sendMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => sendMut.mutate({ dry_run: false })}
                disabled={sendMut.isPending}
              >
                {sendMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <ArrowRight className="mr-2 h-4 w-4" />
                )}
                Yes, post live now
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Single-row live-send confirmation modal */}
      {liveSendJobId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-w-md rounded-lg bg-card p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <ShieldAlert className="h-6 w-6 shrink-0 text-destructive" />
              <div>
                <h3 className="text-lg font-semibold">
                  Confirm: post this single row?
                </h3>
                {(() => {
                  const job = detail?.jobs.find((j) => j.id === liveSendJobId);
                  if (!job) return null;
                  return (
                    <div className="mt-2 space-y-2 text-sm text-muted-foreground">
                      <p>
                        Calls Unipile and posts <strong>only this row</strong> from{" "}
                        <strong>
                          {accounts.find((a) => a.id === selectedAccount)?.name}
                        </strong>
                        . Other rows in this campaign stay where they are.
                      </p>
                      <div className="rounded border bg-muted/30 p-2 text-xs">
                        <div className="font-mono">
                          on post:{" "}
                          <a
                            href={job.post_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-600 underline"
                          >
                            {job.post_url.slice(0, 60)}…
                          </a>
                        </div>
                        <div className="mt-2 whitespace-pre-wrap italic">
                          {job.draft_comment}
                        </div>
                      </div>
                    </div>
                  );
                })()}
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setLiveSendJobId(null)}
                disabled={sendSingleMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() =>
                  sendSingleMut.mutate({
                    jobId: liveSendJobId,
                    dry_run: false,
                  })
                }
                disabled={sendSingleMut.isPending}
              >
                {sendSingleMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Send className="mr-2 h-4 w-4" />
                )}
                Yes, post this row now
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* "Mark as deleted" confirmation modal */}
      {markDeletedJobId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-w-md rounded-lg bg-card p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <Trash2 className="h-6 w-6 shrink-0 text-muted-foreground" />
              <div>
                <h3 className="text-lg font-semibold">
                  Mark this row as deleted?
                </h3>
                {(() => {
                  const job = detail?.jobs.find(
                    (j) => j.id === markDeletedJobId,
                  );
                  if (!job) return null;
                  return (
                    <div className="mt-2 space-y-2 text-sm text-muted-foreground">
                      <div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                        <strong>Unipile API limitation:</strong> Unipile doesn&apos;t
                        expose a comment-delete endpoint, so we can&apos;t remove
                        the LinkedIn comment from here. If you haven&apos;t
                        already, delete it directly on LinkedIn first (the
                        &ldquo;Open on LinkedIn&rdquo; button does that).
                        Clicking confirm only flips this row to{" "}
                        <code>deleted</code> in our records.
                      </div>
                      <div className="rounded border bg-muted/30 p-2 text-xs">
                        <div className="font-mono">
                          comment_id: {job.comment_id?.slice(0, 36)}…
                        </div>
                        <div className="font-mono">
                          on post:{" "}
                          <a
                            href={job.post_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-600 underline"
                          >
                            {job.post_url.slice(0, 60)}…
                          </a>
                        </div>
                        <div className="mt-1 whitespace-pre-wrap italic">
                          {job.draft_comment}
                        </div>
                      </div>
                    </div>
                  );
                })()}
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setMarkDeletedJobId(null)}
                disabled={markDeletedMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="default"
                onClick={() => markDeletedMut.mutate(markDeletedJobId)}
                disabled={markDeletedMut.isPending}
              >
                {markDeletedMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="mr-2 h-4 w-4" />
                )}
                Mark as deleted
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Live reply-to-reply confirmation modal — mirrors the per-row
          live-send modal so threaded-reply posting goes through the same
          two-step safety pattern. Tick checkbox → click Send reply →
          modal → confirm → fire. */}
      {liveReplyConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-w-md rounded-lg bg-card p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <ShieldAlert className="h-6 w-6 shrink-0 text-destructive" />
              <div>
                <h3 className="text-lg font-semibold">
                  Confirm: post this reply?
                </h3>
                <div className="mt-2 space-y-2 text-sm text-muted-foreground">
                  <p>
                    Calls Unipile and posts your reply as a threaded comment
                    from{" "}
                    <strong>
                      {accounts.find((a) => a.id === selectedAccount)?.name}
                    </strong>
                    . This is irreversible from the engine — only deletable
                    via LinkedIn UI.
                  </p>
                  <div className="rounded border bg-muted/30 p-2 text-xs">
                    <div className="font-semibold text-muted-foreground">Reply text:</div>
                    <div className="mt-1 whitespace-pre-wrap italic">
                      {liveReplyConfirm.text}
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setLiveReplyConfirm(null)}
                disabled={replyToReplyMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() =>
                  replyToReplyMut.mutate({
                    jobId: liveReplyConfirm.jobId,
                    replyCommentId: liveReplyConfirm.replyCommentId,
                    text: liveReplyConfirm.text,
                    dry_run: false,
                  })
                }
                disabled={replyToReplyMut.isPending}
              >
                {replyToReplyMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Send className="mr-2 h-4 w-4" />
                )}
                Yes, post reply now
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Invite composer — opens centered over the page when the operator
          clicks "Connect" on a reply row. Manual textarea (no LLM
          suggestion), 200-char counter (LinkedIn's hard cap), dry-run
          default. Live send goes through liveInviteConfirm below. */}
      {inviteComposer && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-lg space-y-3 rounded-lg bg-card p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <UserPlus className="h-6 w-6 shrink-0 text-emerald-600" />
              <div className="flex-1">
                <h3 className="text-lg font-semibold">
                  Connection request
                  {inviteComposer.replyAuthorName && (
                    <span className="ml-1 text-muted-foreground">
                      to {inviteComposer.replyAuthorName}
                    </span>
                  )}
                </h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  Sends from{" "}
                  <strong>
                    {accounts.find((a) => a.id === selectedAccount)?.name || "—"}
                  </strong>
                  . Per-account daily cap is enforced server-side. Empty
                  note is allowed — sends a note-less invite.
                </p>
              </div>
            </div>

            <textarea
              value={inviteNoteDraft}
              onChange={(e) =>
                setInviteNoteDraft(
                  e.target.value.slice(0, LINKEDIN_INVITE_NOTE_MAX_CHARS),
                )
              }
              placeholder="Optional invite note (≤200 chars)…"
              className="w-full resize-y rounded border bg-background p-2 text-sm"
              rows={4}
              autoFocus
            />
            <div className="flex items-center justify-between text-[11px] text-muted-foreground">
              <span className="tabular-nums">
                {inviteNoteDraft.length} / {LINKEDIN_INVITE_NOTE_MAX_CHARS}
              </span>
              {!livePostingEnabled && selectedAccount && (
                <span className="italic text-amber-700">
                  Live invite locked — tick &quot;I want to actually post&quot; above to enable
                </span>
              )}
            </div>
            {!selectedAccount && (
              <div className="rounded border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-800">
                ⚠ No Unipile account is selected in the dropdown above the
                table. Pick one (e.g. &ldquo;Yair Lurie LinkedIn&rdquo; or
                &ldquo;Nico Test&rdquo;) before clicking Send invite —
                otherwise Send is disabled.
              </div>
            )}

            <div className="flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => {
                  setInviteComposer(null);
                  setInviteNoteDraft("");
                }}
                disabled={sendInviteMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="secondary"
                disabled={!selectedAccount || sendInviteMut.isPending}
                onClick={() =>
                  sendInviteMut.mutate({
                    jobId: inviteComposer.jobId,
                    replyCommentId: inviteComposer.replyCommentId,
                    note_text: inviteNoteDraft,
                    dry_run: true,
                  })
                }
                title="Validate this invite without calling Unipile"
              >
                <CheckCircle2 className="mr-1 h-3.5 w-3.5" />
                Dry-run
              </Button>
              <Button
                variant="destructive"
                disabled={
                  !selectedAccount ||
                  !livePostingEnabled ||
                  sendInviteMut.isPending
                }
                onClick={() =>
                  setLiveInviteConfirm({
                    jobId: inviteComposer.jobId,
                    replyCommentId: inviteComposer.replyCommentId,
                    note_text: inviteNoteDraft,
                  })
                }
              >
                <Send className="mr-1 h-3.5 w-3.5" />
                Send invite
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Live invite confirmation — last gate before a real Unipile
          /users/invite call. Mirrors liveReplyConfirm so the operator
          always sees one final "yes, do it" step after flipping the
          page-level live-posting switch. */}
      {liveInviteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-w-md rounded-lg bg-card p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <ShieldAlert className="h-6 w-6 shrink-0 text-destructive" />
              <div>
                <h3 className="text-lg font-semibold">
                  Confirm: send connection request?
                </h3>
                <div className="mt-2 space-y-2 text-sm text-muted-foreground">
                  <p>
                    Calls Unipile and sends a LinkedIn invitation from{" "}
                    <strong>
                      {accounts.find((a) => a.id === selectedAccount)?.name}
                    </strong>
                    . Counts against the per-account daily invite cap.
                  </p>
                  {liveInviteConfirm.note_text.trim().length > 0 && (
                    <div className="rounded border bg-muted/30 p-2 text-xs">
                      <div className="font-semibold text-muted-foreground">Note:</div>
                      <div className="mt-1 whitespace-pre-wrap italic">
                        {liveInviteConfirm.note_text}
                      </div>
                    </div>
                  )}
                  {liveInviteConfirm.note_text.trim().length === 0 && (
                    <div className="rounded border bg-muted/30 p-2 text-xs italic">
                      No note attached — LinkedIn will send a default
                      &ldquo;wants to connect&rdquo; invite.
                    </div>
                  )}
                </div>
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setLiveInviteConfirm(null)}
                disabled={sendInviteMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() =>
                  sendInviteMut.mutate({
                    jobId: liveInviteConfirm.jobId,
                    replyCommentId: liveInviteConfirm.replyCommentId,
                    note_text: liveInviteConfirm.note_text,
                    dry_run: false,
                  })
                }
                disabled={sendInviteMut.isPending}
              >
                {sendInviteMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <UserPlus className="mr-2 h-4 w-4" />
                )}
                Yes, send invite now
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
