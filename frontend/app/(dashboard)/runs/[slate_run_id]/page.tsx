"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Activity, ExternalLink, FastForward, Mail, RefreshCw, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { CommentTracker } from "@/components/slate/comment-tracker";
import { LiveCostsPanel } from "@/components/slate/live-costs-panel";
import { RunPoolPanel } from "@/components/slate/run-pool-panel";
import {
  slateApi,
  type Candidate,
  type CrossRunDedupSkips,
  type SourceStatusBucket,
} from "@/lib/slate";
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

  // Live cost breakdown — polls every 3s while building, every 10s once
  // the run lands so users opening a finished run still get a fresh read
  // (in case more LLM/provider events arrive late from a retry).
  const isBuilding = data?.slate_run.status === "building";
  const costsQuery = useQuery({
    queryKey: ["slate-run-costs", slateRunId],
    queryFn: () => slateApi.runCosts(slateRunId),
    enabled: !!slateRunId,
    refetchInterval: isBuilding ? 3000 : 10000,
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

  // Tracker (engagement + replies) — pure read on mount; POST endpoint
  // polls on-demand when the operator hits "Track now".
  const trackerQ = useQuery({
    queryKey: ["slate-tracker", slateRunId],
    queryFn: () => slateApi.tracker(slateRunId),
    enabled: !!slateRunId,
    refetchOnWindowFocus: false,
  });

  // Skip ONLY the current discovery source (e.g. exit RULE 24 → start
  // Unipile keyword). Distinct from skipDiscoveryMut, which abandons all
  // remaining sources. Returns 409 from the API if no source is active.
  const skipCurrentSourceMut = useMutation({
    mutationFn: () => slateApi.skipCurrentSource(slateRunId),
    onSuccess: (res) => {
      const nextLabel = res.next_source ?? "discovery end";
      toast.success(
        `Skipping ${res.skipped_source} → continuing with ${nextLabel}.`,
      );
      queryClient.invalidateQueries({ queryKey: ["slate-run", slateRunId] });
    },
    onError: (err: ApiError) =>
      toast.error(err?.detail || "Failed to skip current source"),
  });

  // Human-readable label for the live source-id stamp on the slate_run doc.
  const SOURCE_LABELS: Record<string, string> = {
    unipile_title_search: "RULE 24 (Unipile people search)",
    unipile_keyword: "Unipile keyword post search",
    apidirect: "APIDirect keyword post search",
    exa: "Exa neural keyword search",
  };


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

  // "Track now" — POST to /track-selected with whichever candidates are
  // selected. If none selected, falls back to tracking every candidate
  // in the run (the backend filters to status=shipped anyway).
  const trackMut = useMutation({
    mutationFn: () => {
      const ids =
        selected.size > 0
          ? Array.from(selected)
          : (data?.candidates ?? []).map((c) => c.id);
      if (ids.length === 0) {
        return Promise.reject(new Error("No candidates to track"));
      }
      return slateApi.trackSelected(slateRunId, ids);
    },
    onSuccess: (res) => {
      queryClient.setQueryData(["slate-tracker", slateRunId], res);
      toast.success(`Tracked ${res.candidates.length} candidates`);
    },
    onError: (err: ApiError | Error) =>
      toast.error(("detail" in err && err.detail) || err.message || "Tracker failed"),
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
          <div className="mt-3 space-y-2">
            {data.slate_run.current_source && (
              <div className="text-xs text-muted-foreground">
                In source:{" "}
                <span className="font-mono">
                  {SOURCE_LABELS[data.slate_run.current_source] ??
                    data.slate_run.current_source}
                </span>
              </div>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => skipCurrentSourceMut.mutate()}
                disabled={
                  skipCurrentSourceMut.isPending ||
                  !data.slate_run.current_source ||
                  data.slate_run.skip_current_source ===
                    data.slate_run.current_source ||
                  data.slate_run.skip_remaining_discovery === true
                }
              >
                <FastForward className="mr-2 h-3.5 w-3.5" />
                {data.slate_run.skip_current_source &&
                data.slate_run.skip_current_source ===
                  data.slate_run.current_source
                  ? `Skipping ${data.slate_run.current_source}…`
                  : "Skip current source → next"}
              </Button>
              <Button
                variant="outline"
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
                  : "Skip all remaining discovery → gates"}
              </Button>
              {data.slate_run.skip_remaining_discovery_at && (
                <span className="text-xs text-muted-foreground">
                  requested {fmtDate(data.slate_run.skip_remaining_discovery_at)}
                </span>
              )}
            </div>
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

      {/* Live cost breakdown */}
      <LiveCostsPanel
        costs={costsQuery.data}
        isLoading={costsQuery.isLoading}
        isBuilding={isBuilding}
      />

      {/* Pool accounts that participated in THIS run — snapshot of the
          allowlist at run start + each account's current live state. */}
      <RunPoolPanel slateRunId={slateRunId} isBuilding={isBuilding} />

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

      {/* Cross-run dedup skips — posts re-surfaced by discovery that were
          filtered as "already found in a previous run" within the 90-day
          exhaustion window. Operator visibility into what the engine
          would-have-found-again-but-correctly-skipped. */}
      {data.cross_run_dedup_skips &&
        data.cross_run_dedup_skips.total_count > 0 && (
          <CrossRunDedupSkipsSection
            skips={data.cross_run_dedup_skips}
          />
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

      {/* Engagement & replies tracker — surfaces every-2h beat data plus
          an on-demand "Track now" trigger for selected candidates. */}
      {trackerQ.data && trackerQ.data.candidates.length > 0 && (
        <div className="space-y-3 pb-24">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                Engagement &amp; replies ({trackerQ.data.candidates.length})
              </h2>
              <p className="text-xs text-muted-foreground">
                Likes, replies and suggested follow-ups for shipped comments
                in this slate. Auto-refreshed every 2 hours; click "Track now"
                to refresh just the selected candidates.
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => trackMut.mutate()}
              disabled={trackMut.isPending}
            >
              <RefreshCw
                className={`mr-1.5 h-3.5 w-3.5 ${
                  trackMut.isPending ? "animate-spin" : ""
                }`}
              />
              {trackMut.isPending
                ? "Polling…"
                : selected.size > 0
                ? `Track ${selected.size} selected`
                : "Track all shipped"}
            </Button>
          </div>
          <CommentTracker
            items={
              selected.size > 0
                ? trackerQ.data.candidates.filter((c) =>
                    selected.has(c.candidate_id),
                  )
                : trackerQ.data.candidates
            }
            emptyHint="None of the selected candidates have shipped comments yet."
            onChanged={() => trackerQ.refetch()}
          />
        </div>
      )}

      {/* Sticky action bar — Track now + Send email, shown when ≥1 selected. */}
      {selected.size > 0 && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-20 w-[min(820px,calc(100vw-2rem))]">
          <div className="rounded-xl border bg-background/95 p-3 shadow-lg backdrop-blur flex items-center gap-2 flex-wrap">
            <div className="flex items-center gap-2 text-sm">
              <Mail className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">{selected.size} selected</span>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => trackMut.mutate()}
              disabled={trackMut.isPending}
            >
              <Activity className="mr-1.5 h-3.5 w-3.5" />
              {trackMut.isPending ? "Polling…" : "Track now"}
            </Button>
            <Input
              type="email"
              value={emailTo}
              onChange={(e) => setEmailTo(e.target.value)}
              placeholder="leave blank → your account email"
              className="flex-1 h-9 text-sm min-w-[180px]"
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

/**
 * Renders the "Cross-run dedup skips" section on the run detail page.
 * Posts that discovery re-surfaced this run but skipped because they
 * were already inserted as candidates in a prior run within the 90-day
 * exhaustion window. Operator-facing visibility into "what the engine
 * would have found again but correctly didn't process twice."
 *
 * Layout: counter row up top with seeded-set context, then a collapsed
 * list of canonical post URLs (click to expand). The list is capped at
 * sample_cap on the backend so it never grows unbounded — total_count
 * may exceed sample_urls.length.
 */
function CrossRunDedupSkipsSection({ skips }: { skips: CrossRunDedupSkips }) {
  const [expanded, setExpanded] = useState(false);
  const truncated = skips.total_count > skips.sample_urls.length;
  return (
    <div className="space-y-2">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Skipped — already found in a previous run
      </h2>
      <div className="rounded-lg border bg-muted/20 p-3 text-sm">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <div>
            <span className="text-xl font-semibold tabular-nums">
              {skips.total_count}
            </span>
            <span className="ml-1 text-muted-foreground">
              skip{skips.total_count === 1 ? "" : "s"} this run
            </span>
          </div>
          <div className="text-xs text-muted-foreground">
            from a 90-day history of {skips.seeded_urls_count.toLocaleString()} post URL
            {skips.seeded_urls_count === 1 ? "" : "s"}
          </div>
          {skips.sample_urls.length > 0 && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="ml-auto text-xs font-medium text-blue-700 hover:underline"
            >
              {expanded
                ? "Hide URLs"
                : `Show ${skips.sample_urls.length} URL${skips.sample_urls.length === 1 ? "" : "s"}`}
            </button>
          )}
        </div>
        {expanded && skips.sample_urls.length > 0 && (
          <div className="mt-3 space-y-1 border-t pt-3">
            <ul className="max-h-72 space-y-1 overflow-y-auto pr-1 text-xs">
              {skips.sample_urls.map((url) => (
                <li key={url} className="truncate">
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-700 hover:underline"
                  >
                    {url}
                  </a>
                </li>
              ))}
            </ul>
            {truncated && (
              <div className="pt-1 text-[11px] italic text-muted-foreground">
                Showing first {skips.sample_urls.length} of {skips.total_count} —
                list capped at {skips.sample_cap} per run to keep the slate doc small.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
