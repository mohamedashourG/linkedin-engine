"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Check,
  ExternalLink,
  MessageSquare,
  Copy,
  Share2,
  X as XIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { repliesApi } from "@/lib/replies";
import type { TrackerCandidate, TrackerReply } from "@/lib/slate";

/**
 * LinkedIn's six standard reaction types mapped to the same emoji you
 * see in the LinkedIn UI under each post. APIdirect returns the keys
 * verbatim in `post_reactions`; anything outside this set falls back to
 * a generic 👍 so we still render the count rather than swallow it.
 */
/**
 * Map of LinkedIn reaction-type names to the emoji LinkedIn renders in
 * the UI. APIdirect returns LinkedIn's internal reaction keys verbatim,
 * which don't always match the UI label (e.g. LinkedIn's "support"
 * reaction comes back as "empathy", "celebrate" as "praise", "insightful"
 * as "interest"). We alias every observed variant to the same emoji so
 * the chip renders correctly regardless of which key the API returns.
 */
const REACTION_EMOJI: Record<string, string> = {
  like: "👍",
  celebrate: "👏",
  praise: "👏",
  appreciation: "👏",
  empathy: "🤝",
  support: "🤝",
  love: "❤️",
  insightful: "💡",
  interest: "💡",
  funny: "😄",
  entertainment: "😄",
  curious: "🤔",
  maybe: "🤔",
};

function reactionEmoji(kind: string): string {
  // Fallback for any unmapped key: ✨ (rare in practice but visible).
  return REACTION_EMOJI[kind.toLowerCase()] ?? "✨";
}

function compactNumber(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
  if (n < 1_000_000) return Math.round(n / 1000) + "K";
  return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
}

/** Sum of all reaction-type counts. Falls back to post_likes when the
 *  breakdown is missing (APIdirect quota / fetch failure). */
function totalReactions(c: TrackerCandidate): number {
  if (c.post_reactions) {
    return Object.values(c.post_reactions).reduce((s, n) => s + n, 0);
  }
  return c.post_likes;
}

/**
 * Renders the engagement-and-replies block for a list of tracker
 * candidates. Used by both the past-run detail page and the pipeline
 * lead-detail panel so the look + interactions stay consistent.
 *
 * Reply actions (mark sent / dismissed / edited) call the existing
 * `PUT /api/replies/{id}/action` endpoint via `repliesApi.action`.
 */
export function CommentTracker({
  items,
  emptyHint,
  onChanged,
}: {
  items: TrackerCandidate[];
  emptyHint?: string;
  /** Called after a successful reply action so the parent can re-fetch. */
  onChanged?: () => void;
}) {
  if (items.length === 0) {
    return (
      <p className="text-xs text-muted-foreground italic">
        {emptyHint ??
          "Nothing to track yet. Mark a comment as shipped on /today to start tracking engagement."}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {items.map((c) => (
        <CandidateRow key={c.candidate_id} item={c} onChanged={onChanged} />
      ))}
    </div>
  );
}

function CandidateRow({
  item,
  onChanged,
}: {
  item: TrackerCandidate;
  onChanged?: () => void;
}) {
  const [expanded, setExpanded] = useState(item.replies.length > 0);
  const isShipped = item.status === "shipped";
  const polled = item.latest_polled_at
    ? new Date(item.latest_polled_at).toLocaleString()
    : null;

  return (
    <div className="rounded-lg border bg-card p-4 space-y-2">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
            <Badge variant={isShipped ? "success" : "secondary"}>
              {item.status}
            </Badge>
            {item.our_comment_status === "detected" && (
              <Badge variant="outline" className="text-emerald-700 border-emerald-200">
                <Check className="h-3 w-3" /> comment live on LinkedIn
              </Badge>
            )}
            {item.our_comment_status === "queued" && (
              <Badge variant="warning">queued</Badge>
            )}
            {item.our_comment_status === "sent" && !item.our_comment_id && (
              <Badge variant="warning">posting…</Badge>
            )}
            {item.our_comment_status === "not_found" && (
              <Badge variant="muted">not shipped yet</Badge>
            )}
            {item.shipped_at && (
              <span>shipped {new Date(item.shipped_at).toLocaleString()}</span>
            )}
          </div>
          <div className="mt-1 text-sm font-medium">
            {item.author_name || "Unknown author"}
          </div>
          {item.post_url && (
            <a
              href={item.post_url}
              target="_blank"
              rel="noreferrer"
              className="mt-0.5 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              open post <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
          <span
            className="inline-flex items-center gap-1"
            title="Likes/replies on our comment (Unipile)"
          >
            <span aria-hidden="true">👍</span>
            {item.latest_reaction_count}
          </span>
          <span
            className="inline-flex items-center gap-1"
            title="Replies to our comment (Unipile)"
          >
            <MessageSquare className="h-3.5 w-3.5" />
            {item.latest_reply_count}
          </span>
          {polled && <span title={`Our-comment polled ${polled}`}>· polled</span>}
        </div>
      </div>

      {/* LinkedIn-style original-post engagement (APIdirect):
            👍 👏 🤝  Jane Smith and 1,142 others · 23 comments · 8 shares  */}
      {(item.post_polled_at || totalReactions(item) > 0) && (
        <div className="rounded-lg border bg-muted/30 px-3 py-2 flex items-center justify-between gap-3 flex-wrap text-xs">
          <div className="flex items-center gap-2 min-w-0">
            {item.post_reactions &&
            Object.keys(item.post_reactions).length > 0 ? (
              <div className="flex -space-x-1 items-center">
                {Object.entries(item.post_reactions)
                  .sort(([, a], [, b]) => b - a)
                  .slice(0, 3)
                  .map(([kind, n]) => (
                    <span
                      key={kind}
                      className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-background bg-background text-[12px] shadow-sm"
                      title={`${kind}: ${n}`}
                    >
                      {reactionEmoji(kind)}
                    </span>
                  ))}
              </div>
            ) : (
              <span aria-hidden="true" className="text-base">
                👍
              </span>
            )}
            <span className="text-foreground/80 tabular-nums">
              {compactNumber(totalReactions(item))}
            </span>
            <span className="text-muted-foreground">on original post</span>
          </div>
          <div className="flex items-center gap-3 text-muted-foreground tabular-nums">
            <span
              className="inline-flex items-center gap-1"
              title="Total comments on the original post"
            >
              <MessageSquare className="h-3 w-3" />
              {compactNumber(item.post_comments_total)}
            </span>
            <span
              className="inline-flex items-center gap-1"
              title="Shares of the original post"
            >
              <Share2 className="h-3 w-3" />
              {compactNumber(item.post_shares)}
            </span>
          </div>
        </div>
      )}

      {/* Per-reaction-type detail row, only when we have a breakdown */}
      {item.post_reactions && Object.keys(item.post_reactions).length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          {Object.entries(item.post_reactions)
            .sort(([, a], [, b]) => b - a)
            .map(([kind, n]) => (
              <span
                key={kind}
                className="inline-flex items-center gap-1 rounded-full border bg-background px-2 py-0.5"
              >
                <span aria-hidden="true">{reactionEmoji(kind)}</span>
                <span className="font-medium text-foreground/80">{kind}</span>
                <span className="tabular-nums">{compactNumber(n)}</span>
              </span>
            ))}
        </div>
      )}

      {item.our_comment_text && (
        <div className="rounded border bg-muted/30 p-2 text-xs">
          <div className="font-medium mb-0.5">Our comment</div>
          <p className="whitespace-pre-wrap text-muted-foreground">
            {item.our_comment_text}
          </p>
        </div>
      )}

      {item.replies.length > 0 && (
        <div className="pt-1">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs font-medium text-foreground/80 hover:text-foreground"
          >
            {expanded ? "Hide" : "Show"} {item.replies.length}{" "}
            {item.replies.length === 1 ? "reply" : "replies"}
          </button>
          {expanded && (
            <div className="mt-2 space-y-2">
              {item.replies.map((r) => (
                <ReplyRow key={r.id} reply={r} onChanged={onChanged} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ReplyRow({
  reply,
  onChanged,
}: {
  reply: TrackerReply;
  onChanged?: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const act = useMutation({
    mutationFn: (action: "sent" | "dismissed" | "edited") =>
      repliesApi.action(reply.id, action),
    onSuccess: (_d, action) => {
      toast.success(`Reply marked ${action}`);
      onChanged?.();
    },
    onError: (err: ApiError) => toast.error(err.detail || "Failed"),
  });

  const onCopy = async () => {
    if (!reply.suggested_reply) return;
    await navigator.clipboard.writeText(reply.suggested_reply);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const isDone = reply.user_action !== "pending";
  return (
    <div
      className={`rounded-lg border p-2.5 ${
        isDone ? "bg-muted/30 opacity-70" : "bg-background"
      }`}
    >
      <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
        <span className="font-medium text-foreground">
          {reply.author_name || "Unknown"}
        </span>
        {reply.author_is_post_owner && (
          <Badge variant="success" className="rounded-full">
            post owner
          </Badge>
        )}
        {reply.published_at && (
          <span>{new Date(reply.published_at).toLocaleString()}</span>
        )}
        {isDone && (
          <Badge variant="muted" className="ml-auto">
            {reply.user_action}
          </Badge>
        )}
      </div>
      <p className="mt-1 text-sm whitespace-pre-wrap">{reply.text}</p>
      {reply.suggested_reply && (
        <div className="mt-2 rounded border bg-emerald-50/50 border-emerald-200 p-2">
          <div className="mb-1 text-[11px] font-medium text-emerald-900">
            Suggested follow-up
            {reply.suggested_reply_type && (
              <span className="ml-1 text-emerald-700/70">
                ({reply.suggested_reply_type})
              </span>
            )}
          </div>
          <p className="text-xs text-emerald-950 whitespace-pre-wrap">
            {reply.suggested_reply}
          </p>
          {!isDone && (
            <div className="mt-2 flex items-center gap-1.5">
              <Button
                size="sm"
                variant="outline"
                type="button"
                onClick={onCopy}
                className="h-7"
              >
                <Copy className="mr-1 h-3 w-3" />
                {copied ? "Copied" : "Copy"}
              </Button>
              <Button
                size="sm"
                type="button"
                onClick={() => act.mutate("sent")}
                disabled={act.isPending}
                className="h-7"
              >
                <Check className="mr-1 h-3 w-3" />
                Mark sent
              </Button>
              <Button
                size="sm"
                variant="ghost"
                type="button"
                onClick={() => act.mutate("dismissed")}
                disabled={act.isPending}
                className="h-7 text-muted-foreground hover:text-destructive"
              >
                <XIcon className="mr-1 h-3 w-3" />
                Dismiss
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
