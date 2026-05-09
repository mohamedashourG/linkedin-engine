"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Copy, Check, ExternalLink, Trash2, Edit3, Send } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { type Candidate, slateApi } from "@/lib/slate";

const COMMENT_TYPE_LABELS: Record<string, string> = {
  A: "Substantive",
  B: "War story",
  C: "Reframe",
  D: "Encouragement",
  E: "Question",
  F: "Punchy",
};

const SOURCE_LABELS: Record<string, string> = {
  tier_1_kw: "tier-1 keyword",
  tier_2_kw: "tier-2 keyword",
  tier_3_kw: "tier-3 keyword",
  embedded_harvest: "harvested name",
  manual_seed: "target contact",
};

export function SlateCard({ candidate }: { candidate: Candidate }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState(candidate.comment_text ?? "");
  const [copied, setCopied] = useState(false);

  const action = useMutation({
    mutationFn: ({
      action,
      edited_text,
    }: {
      action: "copied" | "shipped" | "dropped" | "edited";
      edited_text?: string;
    }) => slateApi.action(candidate.id, action, edited_text),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["slate-today"] });
      if (vars.action === "shipped") {
        toast.success("Marked shipped — lead advanced to S2");
      } else if (vars.action === "dropped") {
        toast.message("Comment dropped");
      } else if (vars.action === "edited") {
        toast.success("Edit saved");
      }
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const onCopy = async () => {
    if (!candidate.comment_text) return;
    await navigator.clipboard.writeText(candidate.comment_text);
    setCopied(true);
    action.mutate({ action: "copied" });
    setTimeout(() => setCopied(false), 1500);
  };

  const isShipped = candidate.user_action === "shipped";
  const isDropped = candidate.user_action === "dropped";

  return (
    <div
      className={`rounded-xl border bg-background p-5 space-y-4 transition ${
        isShipped ? "border-emerald-200 bg-emerald-50/30" : ""
      } ${isDropped ? "opacity-50" : ""}`}
    >
      {/* Header chips */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 flex-wrap">
          {candidate.comment_type && (
            <Badge variant="default" className="rounded-full">
              {candidate.comment_type} · {COMMENT_TYPE_LABELS[candidate.comment_type] ?? candidate.comment_type}
            </Badge>
          )}
          {candidate.icp_score !== null && (
            <Badge
              variant={candidate.icp_score >= 10 ? "success" : "secondary"}
              className="rounded-full"
            >
              ICP {candidate.icp_score}
            </Badge>
          )}
          <Badge variant="outline" className="rounded-full">
            via {SOURCE_LABELS[candidate.source] ?? candidate.source}
          </Badge>
          {isShipped && (
            <Badge variant="success" className="rounded-full">
              <Check className="h-3 w-3" />
              shipped
            </Badge>
          )}
        </div>
        <a
          href={candidate.post_url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
        >
          <ExternalLink className="h-3 w-3" />
          Open post
        </a>
      </div>

      {/* Author + original post */}
      <div className="rounded-lg border bg-muted/30 p-3">
        <div className="text-xs font-semibold mb-1">
          {candidate.author_name ?? "Unknown author"}
        </div>
        <p className="line-clamp-4 text-sm text-muted-foreground whitespace-pre-wrap">
          {candidate.post_text}
        </p>
      </div>

      {/* Drafted comment */}
      {editing ? (
        <Textarea
          rows={5}
          value={editText}
          onChange={(e) => setEditText(e.target.value)}
          className="text-sm leading-relaxed"
        />
      ) : (
        <div className="rounded-lg bg-foreground p-4 text-[13.5px] leading-relaxed text-background whitespace-pre-wrap">
          {candidate.comment_text}
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center justify-between gap-2 flex-wrap pt-1">
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant={isShipped ? "outline" : "default"}
            onClick={onCopy}
            disabled={!candidate.comment_text}
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
            onClick={() => action.mutate({ action: "shipped" })}
            disabled={isShipped}
            className="h-8"
          >
            <Send className="mr-1.5 h-3.5 w-3.5" />
            Mark shipped
          </Button>
        </div>
        <div className="flex items-center gap-0.5">
          {editing ? (
            <>
              <Button
                size="sm"
                variant="ghost"
                className="h-8"
                onClick={() => {
                  setEditing(false);
                  setEditText(candidate.comment_text ?? "");
                }}
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
              <Edit3 className="mr-1.5 h-3.5 w-3.5" />
              Edit
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-8 text-muted-foreground hover:text-destructive"
            onClick={() => action.mutate({ action: "dropped" })}
            disabled={isDropped}
          >
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            Drop
          </Button>
        </div>
      </div>
    </div>
  );
}
