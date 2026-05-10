"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { SlateCard } from "@/components/slate/slate-card";
import { PipelineBreakdownView } from "@/components/slate/pipeline-breakdown";
import { RunProgress } from "@/components/slate/run-progress";
import { slateApi } from "@/lib/slate";

export default function TodayPage() {
  const qc = useQueryClient();
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["slate-today"],
    queryFn: slateApi.today,
    refetchInterval: 5_000,
  });

  const runNow = useMutation({
    mutationFn: slateApi.runNow,
    onSuccess: () => {
      toast.success("Daily run queued — slate will rebuild in 1-3 min");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["slate-today"] }), 1500);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Today&apos;s slate</h1>
          {data?.slate_run ? (
            <div className="mt-1 flex items-center gap-2 text-sm text-muted-foreground">
              <span>
                {new Date(data.slate_run.run_date).toLocaleDateString(undefined, {
                  weekday: "long",
                  month: "short",
                  day: "numeric",
                })}
              </span>
              <span>·</span>
              <SlateBadge slate={data.slate_run} />
              {data.slate_run.email_sent && (
                <>
                  <span>·</span>
                  <span>email sent</span>
                </>
              )}
            </div>
          ) : (
            <p className="mt-1 text-sm text-muted-foreground">
              No slate built yet today.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="icon"
            className="h-9 w-9"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw
              className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`}
            />
          </Button>
          <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
            <Play className="mr-2 h-4 w-4" />
            {runNow.isPending ? "Queueing…" : "Run now"}
          </Button>
        </div>
      </div>

      {/* Pipeline progress + stepper (stays visible after seal) */}
      {data?.slate_run && <RunProgress slate={data.slate_run} />}

      {/* Per-step post lists — same data after the run completes */}
      {!isLoading && data?.pipeline && data.slate_run && (
        <PipelineBreakdownView
          pipeline={data.pipeline}
          emailSent={data.slate_run.email_sent}
        />
      )}

      {/* Force-abort banner */}
      {data?.slate_run?.status === "force_aborted" && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
          <strong>RULE 23 force-abort:</strong>{" "}
          {data.slate_run.force_abort_reason ?? "unknown reason"}. The slate
          was not delivered.
        </div>
      )}

      {/* Loading skeleton */}
      {isLoading && (
        <div className="space-y-4">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      )}

      {/* Empty state */}
      {!isLoading && data && data.candidates.length === 0 && (
        <EmptyState
          icon={Sparkles}
          title={
            data.slate_run
              ? "No candidates passed the gates today"
              : "No slate built yet today"
          }
          body={
            data.slate_run
              ? "Tighten or broaden your keywords in Settings, or click Run now to retry."
              : "Click Run now to build today's slate. The first run takes 1-3 minutes — discover, gate, draft, validate, seal."
          }
          action={{
            label: "Run now",
            onClick: () => runNow.mutate(),
          }}
        />
      )}

      {/* Slate, grouped by cofounder */}
      {!isLoading && data && data.candidates.length > 0 && (
        <SlateByCofounder data={data} />
      )}
    </div>
  );
}

function SlateByCofounder({ data }: { data: ReturnType<typeof slateApi.today> extends Promise<infer T> ? T : never }) {
  const cofounderById = Object.fromEntries(
    data.cofounders.map((cf) => [cf.id, cf]),
  );
  const grouped: Record<string, typeof data.candidates> = {};
  for (const c of data.candidates) {
    (grouped[c.cofounder_id] ??= []).push(c);
  }

  return (
    <div className="space-y-10">
      {Object.entries(grouped).map(([cfId, items]) => {
        const cf = cofounderById[cfId];
        return (
          <section key={cfId} className="space-y-3">
            <div className="flex items-end justify-between border-b pb-3">
              <h2 className="text-lg font-semibold">
                {cf?.display_name ?? "Unknown cofounder"}
              </h2>
              <span className="text-xs text-muted-foreground">
                {items.length} comments · target {cf?.daily_volume_target}/day
              </span>
            </div>
            <div className="grid gap-3">
              {items.map((c, i) => (
                <SlateCard
                  key={c.id}
                  candidate={c}
                  rank={i + 1}
                  total={items.length}
                />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function SlateBadge({ slate }: { slate: { status: string } }) {
  if (slate.status === "sealed") return <Badge variant="success">sealed</Badge>;
  if (slate.status === "force_aborted")
    return <Badge variant="destructive">force aborted</Badge>;
  return <Badge variant="muted">building</Badge>;
}
