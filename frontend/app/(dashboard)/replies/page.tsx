"use client";

import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Check,
  Copy,
  ExternalLink,
  Inbox,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/shared/empty-state";
import { repliesApi, type Reply } from "@/lib/replies";

const TYPE_LABELS: Record<string, string> = {
  A: "Substantive",
  B: "War story",
  C: "Reframe",
  D: "Encouragement",
  E: "Question",
  F: "Punchy",
};

export default function RepliesPage() {
  const qc = useQueryClient();
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["replies"],
    queryFn: () => repliesApi.list(14),
    refetchInterval: 60_000,
  });

  const pollNow = useMutation({
    mutationFn: repliesApi.pollNow,
    onSuccess: () => {
      toast.success("Reply poll queued — refreshing in a moment");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["replies"] }), 1500);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const replies = data ?? [];
  const pending = replies.filter((r) => r.user_action === "pending");
  const handled = replies.filter((r) => r.user_action !== "pending");

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Replies</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            New comments on your shipped posts. Polled every 2h. Force-poll
            with the button if needed.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="icon"
            className="h-9 w-9"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
          </Button>
          <Button onClick={() => pollNow.mutate()} disabled={pollNow.isPending}>
            {pollNow.isPending ? "Polling…" : "Poll now"}
          </Button>
        </div>
      </div>

      {isLoading && (
        <div className="space-y-3">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {!isLoading && replies.length === 0 && (
        <EmptyState
          icon={Inbox}
          title="No replies yet"
          body="When someone comments on a post you've shipped, the reply (and a drafted follow-up in your voice) shows up here."
        />
      )}

      <Section title={`Needs response (${pending.length})`} replies={pending} />
      {handled.length > 0 && (
        <Section
          title={`Handled (${handled.length})`}
          replies={handled}
          muted
        />
      )}
    </div>
  );
}

function Section({
  title,
  replies,
  muted,
}: {
  title: string;
  replies: Reply[];
  muted?: boolean;
}) {
  if (replies.length === 0) return null;
  return (
    <section className={`space-y-3 ${muted ? "opacity-60" : ""}`}>
      <h2 className="text-base font-semibold border-b pb-2">{title}</h2>
      <div className="grid gap-3">
        {replies.map((r) => (
          <ReplyCard key={r.id} reply={r} />
        ))}
      </div>
    </section>
  );
}

function ReplyCard({ reply }: { reply: Reply }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState(reply.suggested_reply);
  const [copied, setCopied] = useState(false);

  const action = useMutation({
    mutationFn: ({
      action,
      edited_text,
    }: {
      action: "sent" | "dismissed" | "edited";
      edited_text?: string;
    }) => repliesApi.action(reply.id, action, edited_text),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["replies"] });
      qc.invalidateQueries({ queryKey: ["replies-unread"] });
      if (vars.action === "sent") toast.success("Marked sent");
      else if (vars.action === "dismissed") toast.message("Reply dismissed");
      else if (vars.action === "edited") toast.success("Edit saved");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const onCopy = async () => {
    await navigator.clipboard.writeText(reply.suggested_reply);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="rounded-xl border bg-background p-5 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 flex-wrap">
          <Badge variant="default" className="rounded-full">
            {reply.suggested_reply_type} ·{" "}
            {TYPE_LABELS[reply.suggested_reply_type] ?? "—"}
          </Badge>
          {reply.reply_author_is_post_owner && (
            <Badge variant="info" className="rounded-full">
              post author
            </Badge>
          )}
          {reply.cofounder_name && (
            <Badge variant="muted" className="rounded-full">
              {reply.cofounder_name}'s thread
            </Badge>
          )}
          {reply.user_action === "sent" && (
            <Badge variant="success" className="rounded-full">
              <Check className="h-3 w-3" />
              sent
            </Badge>
          )}
          {reply.user_action === "dismissed" && (
            <Badge variant="muted" className="rounded-full">
              dismissed
            </Badge>
          )}
        </div>
        {reply.candidate_post_url && (
          <a
            href={reply.candidate_post_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
          >
            <ExternalLink className="h-3 w-3" />
            Open thread
          </a>
        )}
      </div>

      <div className="text-xs">
        <span className="font-semibold">{reply.reply_author_name ?? "Unknown"}</span>
        {reply.reply_published_at && (
          <span className="ml-2 text-muted-foreground">
            {new Date(reply.reply_published_at).toLocaleString()}
          </span>
        )}
      </div>

      {reply.candidate_post_text && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            Show context (original post + our comment)
          </summary>
          <div className="mt-2 space-y-2">
            <div className="rounded-md bg-muted/50 p-2 text-muted-foreground line-clamp-4">
              <span className="font-semibold text-foreground">Original: </span>
              {reply.candidate_post_text}
            </div>
            {reply.our_comment && (
              <div className="rounded-md bg-foreground/5 p-2">
                <span className="font-semibold">Our comment: </span>
                {reply.our_comment}
              </div>
            )}
          </div>
        </details>
      )}

      <div className="rounded-lg border bg-muted/30 p-3 text-sm whitespace-pre-wrap">
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground mb-1">
          Their reply
        </div>
        {reply.reply_text}
      </div>

      <div className="rounded-lg bg-foreground p-4 text-[13.5px] leading-relaxed text-background whitespace-pre-wrap">
        <div className="text-[11px] uppercase tracking-wide text-background/60 mb-1">
          Suggested follow-up
        </div>
        {editing ? (
          <Textarea
            rows={4}
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            className="bg-background text-foreground"
          />
        ) : (
          reply.suggested_reply || "(no suggestion — voice template missing)"
        )}
      </div>

      <div className="flex items-center justify-between gap-2 flex-wrap pt-1">
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            onClick={onCopy}
            disabled={!reply.suggested_reply}
            className="h-8"
          >
            {copied ? (
              <Check className="mr-1.5 h-3.5 w-3.5" />
            ) : (
              <Copy className="mr-1.5 h-3.5 w-3.5" />
            )}
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-8"
            onClick={() => action.mutate({ action: "sent" })}
            disabled={reply.user_action === "sent"}
          >
            Mark sent
          </Button>
        </div>
        <div className="flex items-center gap-0.5">
          {editing ? (
            <>
              <Button
                size="sm"
                variant="ghost"
                className="h-8"
                onClick={() => setEditing(false)}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                className="h-8"
                onClick={() => {
                  action.mutate({ action: "edited", edited_text: editText });
                  setEditing(false);
                }}
              >
                Save edit
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              className="h-8"
              onClick={() => setEditing(true)}
            >
              Edit
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-8 text-muted-foreground hover:text-destructive"
            onClick={() => action.mutate({ action: "dismissed" })}
            disabled={reply.user_action === "dismissed"}
          >
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            Dismiss
          </Button>
        </div>
      </div>
    </div>
  );
}
