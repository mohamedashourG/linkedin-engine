"use client";

import { ChevronDown, ExternalLink } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { PipelineBreakdown, PipelinePostRef } from "@/lib/slate";

const STEPS: { key: keyof PipelineBreakdown; title: string; hint?: string }[] = [
  { key: "discovery", title: "Discovery", hint: "Posts pulled from search" },
  { key: "verification", title: "Verification" },
  { key: "gates", title: "4-gate filter" },
  { key: "allocator", title: "Allocation" },
  { key: "drafter", title: "Drafting" },
  { key: "rule_23", title: "RULE 23" },
  {
    key: "email_delivery",
    title: "Sending",
    hint: "Included in the slate email when sent",
  },
];

function PostRows({
  label,
  tone,
  rows,
}: {
  label: string;
  tone: "ok" | "bad" | "wait";
  rows: PipelinePostRef[];
}) {
  if (rows.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <div
        className={cn(
          "text-[11px] font-medium uppercase tracking-wide",
          tone === "ok" && "text-emerald-700",
          tone === "bad" && "text-destructive",
          tone === "wait" && "text-amber-800",
        )}
      >
        {label}{" "}
        <span className="font-mono font-normal text-muted-foreground">({rows.length})</span>
      </div>
      <ul className="space-y-1.5 max-h-56 overflow-y-auto rounded-md border bg-muted/20 p-2">
        {rows.map((p) => (
          <li key={p.id} className="text-sm leading-snug">
            <div className="flex items-start gap-2">
              {p.post_url ? (
                <a
                  href={p.post_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 shrink-0 text-muted-foreground hover:text-foreground"
                  aria-label="Open post"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              ) : (
                <span className="w-3.5 shrink-0" />
              )}
              <div className="min-w-0 flex-1">
                <div className="font-medium text-foreground truncate">
                  {p.author_name?.trim() || "Unknown author"}
                </div>
                {p.post_preview ? (
                  <p className="text-xs text-muted-foreground line-clamp-2">{p.post_preview}</p>
                ) : null}
                {tone === "bad" && p.drop_reason ? (
                  <p className="mt-0.5 text-[11px] text-destructive/90 font-mono break-words">
                    {p.drop_reason}
                  </p>
                ) : null}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PipelineBreakdownView({
  pipeline,
  emailSent,
}: {
  pipeline: PipelineBreakdown;
  emailSent: boolean;
}) {
  return (
    <div className="rounded-xl border bg-background">
      <div className="border-b px-4 py-3">
        <h2 className="text-sm font-semibold">Pipeline — posts per step</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Passed and failed for each engine stage. Stays here after the run finishes.
          {!emailSent && (
            <span className="block mt-1 text-amber-800">
              Email not sent yet: sealed posts appear under Sending → failed until delivery succeeds.
            </span>
          )}
        </p>
      </div>
      <div className="divide-y">
        {STEPS.map(({ key, title, hint }) => {
          const step = pipeline[key];
          const hasAny =
            step.passed.length > 0 ||
            step.failed.length > 0 ||
            (step.pending?.length ?? 0) > 0;
          return (
            <details key={key} className="group" open>
              <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-sm hover:bg-muted/40 [&::-webkit-details-marker]:hidden">
                <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
                <span className="font-medium">{title}</span>
                {hint ? (
                  <span className="hidden text-xs text-muted-foreground sm:inline">{hint}</span>
                ) : null}
                <span className="ml-auto flex flex-wrap items-center justify-end gap-1">
                  <Badge variant="success" className="rounded-full font-mono text-[10px]">
                    ok {step.passed_total}
                  </Badge>
                  <Badge variant="destructive" className="rounded-full font-mono text-[10px]">
                    fail {step.failed_total}
                  </Badge>
                  {step.pending_total > 0 ? (
                    <Badge variant="warning" className="rounded-full font-mono text-[10px]">
                      wait {step.pending_total}
                    </Badge>
                  ) : null}
                </span>
              </summary>
              <div className="space-y-3 border-t bg-muted/10 px-4 py-3">
                {step.truncated ? (
                  <p className="text-[11px] text-muted-foreground">
                    Lists capped at 250 — totals above reflect the full run.
                  </p>
                ) : null}
                <PostRows label="Passed" tone="ok" rows={step.passed} />
                <PostRows label="Failed" tone="bad" rows={step.failed} />
                <PostRows label="Waiting" tone="wait" rows={step.pending ?? []} />
                {!hasAny && (
                  <p className="text-xs text-muted-foreground">No rows for this step.</p>
                )}
              </div>
            </details>
          );
        })}
      </div>
    </div>
  );
}
