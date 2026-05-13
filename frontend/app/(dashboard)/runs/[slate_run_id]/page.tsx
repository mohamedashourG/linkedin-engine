"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, FastForward, Mail, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { slateApi, type Candidate, type SourceStatusBucket } from "@/lib/slate";
import { ApiError } from "@/lib/api";

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** Pivot the flat (source, status, count) list into a sparse matrix the UI
 * can render as a table — one row per source, one column per status,
 * counts in cells with totals on the right. */
function pivotSourceStatus(buckets: SourceStatusBucket[]) {
  const sources = new Set<string>();
  const statuses = new Set<string>();
  const matrix = new Map<string, Map<string, number>>();
  for (const b of buckets) {
    sources.add(b.source);
    statuses.add(b.status);
    if (!matrix.has(b.source)) matrix.set(b.source, new Map());
    matrix.get(b.source)!.set(b.status, b.count);
  }
  // Stable status column order — preferred lexicon first, then anything else
  // alphabetically.
  const preferred = [
    "slated",
    "drafted",
    "shipped",
    "gate_passed",
    "gate_dropped",
    "rejected_inline",
    "rejected_url_mismatch",
    "raw",
  ];
  const statusList = [
    ...preferred.filter((s) => statuses.has(s)),
    ...[...statuses].filter((s) => !preferred.includes(s)).sort(),
  ];
  const sourceList = [...sources].sort();
  return { matrix, sourceList, statusList };
}

export default function RunDetailPage() {
  const params = useParams<{ slate_run_id: string }>();
  const slateRunId = params?.slate_run_id ?? "";
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["slate-run", slateRunId],
    queryFn: () => slateApi.run(slateRunId),
    enabled: !!slateRunId,
    // Poll while the run is still building so the skip button + progress
    // bars update in near-real-time.
    refetchInterval: (q) =>
      q.state.data?.slate_run.status === "building" ? 3000 : false,
  });

  const skipDiscoveryMut = useMutation({
    mutationFn: () => slateApi.skipDiscovery(slateRunId),
    onSuccess: () => {
      toast.success(
        "Skip-discovery flag set. Worker will exit remaining sources within a few seconds; downstream gates and drafter keep running.",
      );
      queryClient.invalidateQueries({ queryKey: ["slate-run", slateRunId] });
    },
    onError: (err: ApiError) =>
      toast.error(err?.detail || "Failed to skip discovery"),
  });

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [emailTo, setEmailTo] = useState<string>("");
  const toggleSelected = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const emailMut = useMutation({
    mutationFn: () =>
      slateApi.emailSelected(slateRunId, {
        candidate_ids: Array.from(selected),
        to: emailTo.trim() || undefined,
      }),
    onSuccess: (res) => {
      toast.success(
        `Sent ${res.count} draft${res.count === 1 ? "" : "s"} to ${res.to}`,
      );
      setSelected(new Set());
    },
    onError: (err: ApiError) =>
      toast.error(err?.detail || "Failed to send email"),
  });

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-1/3" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (isError) {
    return (
      <EmptyState
        title="Couldn't load this run"
        description={(error as Error).message}
      />
    );
  }
  if (!data) return null;

  const { matrix, sourceList, statusList } = pivotSourceStatus(
    data.source_status,
  );
  const cofounderById = new Map(
    data.cofounders.map((cf) => [cf.id, cf.display_name]),
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <Link
          href="/runs"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back to runs
        </Link>
        <h1 className="mt-2 text-2xl font-bold tracking-tight">
          Run {fmtDate(data.slate_run.run_date)}
        </h1>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <Badge
            variant={
              data.slate_run.status === "sealed"
                ? "default"
                : data.slate_run.status === "force_aborted"
                ? "destructive"
                : "secondary"
            }
          >
            {data.slate_run.status}
          </Badge>
          <span>•</span>
          <span>sealed {fmtDate(data.slate_run.sealed_at)}</span>
          {data.slate_run.current_stage && (
            <>
              <span>•</span>
              <span>stage: {data.slate_run.current_stage}</span>
            </>
          )}
          {data.slate_run.force_abort_reason && (
            <>
              <span>•</span>
              <span className="text-destructive">
                aborted: {data.slate_run.force_abort_reason}
              </span>
            </>
          )}
        </div>
        {data.slate_run.status === "building" && (
          <div className="mt-3">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => skipDiscoveryMut.mutate()}
              disabled={
                skipDiscoveryMut.isPending ||
                data.slate_run.skip_remaining_discovery === true
              }
            >
              <FastForward className="mr-2 h-3.5 w-3.5" />
              {data.slate_run.skip_remaining_discovery
                ? "Discovery skip requested — worker exiting"
                : "Skip remaining discovery → gates"}
            </Button>
            {data.slate_run.skip_remaining_discovery_at && (
              <span className="ml-2 text-xs text-muted-foreground">
                requested {fmtDate(data.slate_run.skip_remaining_discovery_at)}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Funnel summary */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {[
          ["Discovered", data.total_discovered],
          ["Verified", data.total_verified],
          ["Gated", data.total_gated],
          ["Drafted", data.total_drafted],
          ["Slated", data.total_slated],
        ].map(([label, count]) => (
          <div
            key={label as string}
            className="rounded-lg border bg-card p-4"
          >
            <div className="text-xs uppercase text-muted-foreground tracking-wide">
              {label}
            </div>
            <div className="mt-1 text-2xl font-semibold tabular-nums">
              {count as number}
            </div>
          </div>
        ))}
      </div>

      {/* By source × status */}
      {sourceList.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            By source × status
          </h2>
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full min-w-[700px] text-sm">
              <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Source</th>
                  {statusList.map((s) => (
                    <th
                      key={s}
                      className="px-3 py-2 text-right font-medium whitespace-nowrap"
                    >
                      {s}
                    </th>
                  ))}
                  <th className="px-3 py-2 text-right font-medium">Total</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {sourceList.map((src) => {
                  const row = matrix.get(src) || new Map<string, number>();
                  const rowTotal = [...row.values()].reduce(
                    (a, b) => a + b,
                    0,
                  );
                  return (
                    <tr key={src} className="hover:bg-muted/30">
                      <td className="px-3 py-2 font-medium">{src}</td>
                      {statusList.map((s) => (
                        <td
                          key={s}
                          className="px-3 py-2 text-right tabular-nums text-muted-foreground"
                        >
                          {row.get(s) ?? 0}
                        </td>
                      ))}
                      <td className="px-3 py-2 text-right tabular-nums font-semibold">
                        {rowTotal}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Top drop reasons */}
      {data.top_drop_reasons.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Top drop reasons
          </h2>
          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-sm">
              <tbody className="divide-y">
                {data.top_drop_reasons.map((d) => (
                  <tr key={d.reason} className="hover:bg-muted/30">
                    <td className="px-3 py-2">{d.reason}</td>
                    <td className="px-3 py-2 text-right tabular-nums font-semibold w-20">
                      {d.count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Drafted / slated candidates */}
      {data.candidates.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Drafted candidates ({data.candidates.length})
            </h2>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <button
                type="button"
                onClick={() =>
                  setSelected(new Set(data.candidates.map((c) => c.id)))
                }
                className="hover:underline"
              >
                select all
              </button>
              <span>·</span>
              <button
                type="button"
                onClick={() => setSelected(new Set())}
                className="hover:underline"
              >
                clear
              </button>
            </div>
          </div>
          <div className="space-y-2 pb-24">
            {data.candidates.map((c: Candidate) => {
              const isSel = selected.has(c.id);
              return (
                <div
                  key={c.id}
                  className={`rounded-lg border p-4 space-y-1.5 transition-colors ${
                    isSel
                      ? "border-primary/50 bg-primary/[0.04]"
                      : "bg-card"
                  }`}
                >
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      checked={isSel}
                      onChange={() => toggleSelected(c.id)}
                      aria-label={`Select ${c.author_name || "candidate"}`}
                      className="mt-1 h-4 w-4 cursor-pointer accent-foreground"
                    />
                    <div className="flex-1 space-y-1.5">
                      <div className="flex items-center gap-2 flex-wrap text-xs text-muted-foreground">
                        <Badge variant="outline">{c.status}</Badge>
                        {c.comment_type && (
                          <Badge variant="secondary">type {c.comment_type}</Badge>
                        )}
                        {c.icp_score != null && (
                          <span>ICP {c.icp_score}/10</span>
                        )}
                        <span>•</span>
                        <span>{cofounderById.get(c.cofounder_id) || c.cofounder_id}</span>
                        <span>•</span>
                        <span>{c.source}</span>
                      </div>
                      <div className="text-sm">
                        <strong>{c.author_name || "Unknown author"}</strong>
                        {c.post_url && (
                          <Link
                            href={c.post_url}
                            target="_blank"
                            className="ml-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                          >
                            open post <ExternalLink className="h-3 w-3" />
                          </Link>
                        )}
                      </div>
                      {c.post_text && (
                        <p className="text-xs text-muted-foreground line-clamp-3">
                          {c.post_text}
                        </p>
                      )}
                      {c.comment_text && (
                        <div className="mt-2 rounded border bg-muted/30 p-2 text-xs">
                          <div className="font-medium mb-0.5">Drafted comment</div>
                          <p className="whitespace-pre-wrap">{c.comment_text}</p>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Sticky send bar — shows when at least one candidate is selected. */}
      {selected.size > 0 && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-20 w-[min(720px,calc(100vw-2rem))]">
          <div className="rounded-xl border bg-background/95 p-3 shadow-lg backdrop-blur flex items-center gap-2">
            <div className="flex items-center gap-2 text-sm">
              <Mail className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">{selected.size} selected</span>
            </div>
            <Input
              type="email"
              value={emailTo}
              onChange={(e) => setEmailTo(e.target.value)}
              placeholder="leave blank → your account email"
              className="flex-1 h-9 text-sm"
            />
            <Button
              size="sm"
              onClick={() => emailMut.mutate()}
              disabled={emailMut.isPending || selected.size === 0}
            >
              <Send className="mr-1.5 h-3.5 w-3.5" />
              {emailMut.isPending ? "Sending…" : "Send"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
