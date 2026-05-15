"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FastForward, Play, RefreshCw, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { SlateCard } from "@/components/slate/slate-card";
import { PipelineBreakdownView } from "@/components/slate/pipeline-breakdown";
import { RunProgress } from "@/components/slate/run-progress";
import { LiveCostsPanel } from "@/components/slate/live-costs-panel";
import { PoolSelectionPanel } from "@/components/slate/pool-selection-panel";
import { slateApi } from "@/lib/slate";

// Human-readable labels for the live discovery source. Mirrors the
// constants used in backend/app/engine/stages/discovery.py.
const SOURCE_LABELS: Record<string, string> = {
  unipile_title_search: "RULE 24 (Unipile people search)",
  unipile_keyword: "Unipile keyword post search",
  apidirect: "APIDirect keyword post search",
  exa: "Exa neural keyword search",
};

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

  // Skip-buttons need a slate_run_id, only relevant while status=building.
  const slateRunId = data?.slate_run?.id;
  const isBuilding = data?.slate_run?.status === "building";

  // Live cost poll — separate query, scoped to the current slate_run.
  // Polls every 3s while the run is building (matches the worker's
  // wave cadence — each refresh catches a fresh batch of LLM /
  // provider events) and slows to 10s once the run lands so a user
  // who opens Today after the slate seals still sees fresh data
  // (late retries can still increment counters post-seal).
  const costsQuery = useQuery({
    queryKey: ["slate-run-costs", slateRunId],
    queryFn: () => slateApi.runCosts(slateRunId!),
    enabled: !!slateRunId,
    refetchInterval: isBuilding ? 3000 : 10000,
  });

  const skipCurrentSourceMut = useMutation({
    mutationFn: () => slateApi.skipCurrentSource(slateRunId!),
    onSuccess: (res) => {
      const nextLabel = res.next_source
        ? SOURCE_LABELS[res.next_source] ?? res.next_source
        : "discovery end";
      const skippedLabel =
        SOURCE_LABELS[res.skipped_source] ?? res.skipped_source;
      toast.success(`Skipping ${skippedLabel} → continuing with ${nextLabel}.`);
      qc.invalidateQueries({ queryKey: ["slate-today"] });
    },
    onError: (err: Error) =>
      toast.error(err?.message || "Failed to skip current source"),
  });

  const skipDiscoveryMut = useMutation({
    mutationFn: () => slateApi.skipDiscovery(slateRunId!),
    onSuccess: () => {
      toast.success(
        "Skip-discovery flag set. Worker will exit remaining sources within a few seconds; gates and drafter keep running.",
      );
      qc.invalidateQueries({ queryKey: ["slate-today"] });
    },
    onError: (err: Error) =>
      toast.error(err?.message || "Failed to skip discovery"),
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

      {/* Skip controls — visible only while a run is in progress. The
          worker reads the `skip_*` flags every per-query iteration, so a
          click takes effect within ~2 seconds. */}
      {isBuilding && slateRunId && (
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Discovery controls
          </div>
          {data!.slate_run!.current_source && (
            <div className="mt-1 text-sm">
              In source:{" "}
              <span className="font-mono">
                {SOURCE_LABELS[data!.slate_run!.current_source!] ??
                  data!.slate_run!.current_source}
              </span>
            </div>
          )}
          {!data!.slate_run!.current_source && (
            <div className="mt-1 text-sm text-muted-foreground">
              Worker is between sources or hasn&apos;t started discovery yet — buttons
              activate once a source is in flight.
            </div>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => skipCurrentSourceMut.mutate()}
              disabled={
                skipCurrentSourceMut.isPending ||
                !data!.slate_run!.current_source ||
                data!.slate_run!.skip_current_source ===
                  data!.slate_run!.current_source ||
                data!.slate_run!.skip_remaining_discovery === true
              }
              title="Exit the source the worker is iterating, then continue with the next source in order: unipile_title_search → unipile_keyword → apidirect → exa"
            >
              <FastForward className="mr-2 h-3.5 w-3.5" />
              {data!.slate_run!.skip_current_source &&
              data!.slate_run!.skip_current_source ===
                data!.slate_run!.current_source
                ? `Skipping ${data!.slate_run!.current_source}…`
                : "Skip current source → next"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => skipDiscoveryMut.mutate()}
              disabled={
                skipDiscoveryMut.isPending ||
                data!.slate_run!.skip_remaining_discovery === true
              }
              title="Exit ALL remaining discovery sources and let gates → allocator → drafter finish on what's already been found"
            >
              <FastForward className="mr-2 h-3.5 w-3.5" />
              {data!.slate_run!.skip_remaining_discovery
                ? "Discovery skip requested — worker exiting"
                : "Skip all remaining discovery → gates"}
            </Button>
          </div>
        </div>
      )}

      {/* Pipeline progress + stepper (stays visible after seal) */}
      {data?.slate_run && <RunProgress slate={data.slate_run} />}

      {/* Pool selection — operator picks which Unipile accounts are
          eligible for FUTURE discovery runs. Snapshotted onto each new
          slate_run at creation; enforced at the pool's atomic-claim
          level so no leakage to non-selected accounts is possible. */}
      <PoolSelectionPanel />

      {/* Live cost breakdown — appears as soon as we have a slate_run id,
          even before any paid call has fired (shows zeros + an empty
          state). Lets the operator monitor spend climb mid-run. */}
      {slateRunId && (
        <LiveCostsPanel
          costs={costsQuery.data}
          isLoading={costsQuery.isLoading}
          isBuilding={isBuilding}
          variant="today"
        />
      )}

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
