"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ExternalLink,
  KanbanSquare,
  MessageSquare,
  CalendarCheck,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { pipelineApi, type LeadCard } from "@/lib/pipeline";

const STAGES: { key: string; label: string; sub: string }[] = [
  { key: "S1", label: "S1", sub: "candidate" },
  { key: "S2", label: "S2", sub: "commented" },
  { key: "S3", label: "S3", sub: "reciprocated" },
  { key: "S4", label: "S4", sub: "CR sent" },
  { key: "S5", label: "S5", sub: "CR accepted" },
  { key: "S6", label: "S6", sub: "DM sent" },
  { key: "S7", label: "S7", sub: "booked" },
  { key: "S8", label: "S8", sub: "meeting held" },
  { key: "S9", label: "S9", sub: "deal status" },
];

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "destructive" | "muted" | "success" | "warning"
> = {
  ACTIVE: "default",
  STALLED: "warning",
  CONVERTED: "success",
  DROPPED: "destructive",
};

export default function PipelinePage() {
  const { data, isLoading } = useQuery({
    queryKey: ["pipeline"],
    queryFn: pipelineApi.get,
    refetchInterval: 30_000,
  });
  const [selected, setSelected] = useState<string | null>(null);

  if (isLoading || !data) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const totalLeads = STAGES.reduce(
    (n, s) => n + (data.by_stage[s.key]?.length ?? 0),
    0,
  );

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Pipeline</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {totalLeads} {totalLeads === 1 ? "lead" : "leads"} across S1–S9.
            Click any lead for the full timeline.
          </p>
        </div>
      </div>

      {totalLeads === 0 && (
        <EmptyState
          icon={KanbanSquare}
          title="No leads yet"
          body="When you mark a comment as shipped on /today, the post author becomes a lead at S2. Replies, CRs, and bookings auto-advance them."
          action={{ label: "Open today's slate", href: "/today" }}
        />
      )}

      {totalLeads > 0 && (
        <div className="-mx-2 overflow-x-auto pb-2">
          <div className="flex gap-3 px-2 min-w-max">
            {STAGES.map((stage) => {
              const cards = data.by_stage[stage.key] ?? [];
              return (
                <div
                  key={stage.key}
                  className="w-72 shrink-0 rounded-xl border bg-muted/20 p-2"
                >
                  <div className="flex items-center justify-between px-2 py-1.5">
                    <div className="flex items-baseline gap-1.5">
                      <span className="text-sm font-semibold">{stage.label}</span>
                      <span className="text-xs text-muted-foreground">
                        {stage.sub}
                      </span>
                    </div>
                    <Badge variant="muted" className="rounded-full">
                      {cards.length}
                    </Badge>
                  </div>
                  <div className="space-y-2">
                    {cards.length === 0 && (
                      <div className="rounded-lg border border-dashed bg-background/40 py-7 text-center text-xs text-muted-foreground">
                        empty
                      </div>
                    )}
                    {cards.map((lead) => (
                      <Card
                        key={lead.id}
                        lead={lead}
                        onSelect={() => setSelected(lead.id)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {selected && (
        <LeadDetailPanel
          leadId={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function Card({
  lead,
  onSelect,
}: {
  lead: LeadCard;
  onSelect: () => void;
}) {
  const variant = STATUS_VARIANT[lead.status] ?? "secondary";
  return (
    <button
      onClick={onSelect}
      className="w-full rounded-lg border bg-background p-3 text-left transition hover:border-foreground/40 hover:shadow-sm"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium leading-tight truncate">
          {lead.name ?? "Unknown"}
        </span>
        <Badge variant={variant} className="rounded-full shrink-0">
          {lead.status}
        </Badge>
      </div>
      <div className="mt-1 truncate text-xs text-muted-foreground">
        {[lead.title, lead.company].filter(Boolean).join(" · ") || "—"}
      </div>
      {(lead.reply_count > 0 || lead.booking_count > 0) && (
        <div className="mt-2 flex items-center gap-3 text-[11px] text-muted-foreground">
          {lead.reply_count > 0 && (
            <span className="inline-flex items-center gap-1">
              <MessageSquare className="h-3 w-3" />
              {lead.reply_count}
            </span>
          )}
          {lead.booking_count > 0 && (
            <span className="inline-flex items-center gap-1">
              <CalendarCheck className="h-3 w-3" />
              {lead.booking_count}
            </span>
          )}
        </div>
      )}
    </button>
  );
}

function LeadDetailPanel({
  leadId,
  onClose,
}: {
  leadId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["lead", leadId],
    queryFn: () => pipelineApi.lead(leadId),
  });
  const setStage = useMutation({
    mutationFn: (stage: string) => pipelineApi.setStage(leadId, stage),
    onSuccess: (_, stage) => {
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      qc.invalidateQueries({ queryKey: ["lead", leadId] });
      toast.success(`Lead moved to ${stage}`);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/30 backdrop-blur-[1px]">
      <div className="h-full w-full max-w-md overflow-y-auto bg-background border-l shadow-2xl">
        <div className="flex items-center justify-between border-b p-4">
          <span className="text-sm font-semibold">Lead detail</span>
          <Button variant="ghost" size="icon" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>

        {!data && (
          <div className="space-y-3 p-4">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        )}
        {data && (
          <div className="space-y-4 p-4">
            <div>
              <h2 className="text-lg font-semibold">
                {data.lead.name ?? "Unknown"}
              </h2>
              <p className="text-xs text-muted-foreground">
                {[data.lead.title, data.lead.company].filter(Boolean).join(" · ") ||
                  "—"}
              </p>
              {data.lead.linkedin_url && (
                <a
                  href={data.lead.linkedin_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-1 inline-flex items-center gap-1 text-xs underline"
                >
                  <ExternalLink className="h-3 w-3" />
                  Open LinkedIn
                </a>
              )}
            </div>

            <div className="rounded-lg border bg-muted/20 p-3 space-y-2 text-sm">
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">stage</span>
                <Badge variant="secondary" className="rounded-full">
                  {data.lead.current_stage}
                </Badge>
                <Badge
                  variant={STATUS_VARIANT[data.lead.status] ?? "muted"}
                  className="rounded-full"
                >
                  {data.lead.status}
                </Badge>
              </div>
              <div className="text-xs text-muted-foreground">
                {data.lead.reply_count} replies · {data.lead.booking_count} bookings
              </div>
              <div className="flex flex-wrap gap-1 pt-1">
                {STAGES.map((s) => (
                  <Button
                    key={s.key}
                    size="sm"
                    variant={
                      data.lead.current_stage === s.key ? "default" : "ghost"
                    }
                    className="h-7 text-xs px-2"
                    onClick={() => setStage.mutate(s.key)}
                    disabled={setStage.isPending}
                  >
                    {s.key}
                  </Button>
                ))}
              </div>
            </div>

            <div className="space-y-2">
              <h3 className="text-sm font-semibold">Timeline</h3>
              {data.timeline.length === 0 && (
                <p className="text-xs text-muted-foreground">
                  No events yet for this lead.
                </p>
              )}
              <ol className="space-y-2">
                {data.timeline.map((entry, i) => (
                  <li
                    key={i}
                    className="rounded-lg border bg-muted/20 p-3 text-xs"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{entry.title}</span>
                      <span className="text-muted-foreground">
                        {new Date(entry.at).toLocaleString()}
                      </span>
                    </div>
                    {entry.body && (
                      <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
                        {entry.body}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
