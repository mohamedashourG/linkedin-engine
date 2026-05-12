"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { History, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { slateApi, type RunListItem } from "@/lib/slate";

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function StatusBadge({ status }: { status: RunListItem["status"] }) {
  const variant =
    status === "sealed"
      ? "default"
      : status === "force_aborted"
      ? "destructive"
      : "secondary";
  return <Badge variant={variant}>{status}</Badge>;
}

export default function PastRunsPage() {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["slate-runs"],
    queryFn: () => slateApi.runs({ limit: 50 }),
    refetchInterval: 60_000,
  });

  const runs = data?.runs ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <History className="h-6 w-6" />
            Past runs
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Every slate this operator has produced. Click a row to see its full
            funnel, drop reasons, and the drafted comments.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw
            className={`mr-2 h-4 w-4 ${isFetching ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <EmptyState
          icon={<History className="h-10 w-10" />}
          title="No runs yet"
          description="Run history will appear here after the engine has sealed a slate."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Date</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
                <th className="px-3 py-2 text-right font-medium">Discovered</th>
                <th className="px-3 py-2 text-right font-medium">Verified</th>
                <th className="px-3 py-2 text-right font-medium">Drafted</th>
                <th className="px-3 py-2 text-right font-medium">Slated</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {runs.map((r) => (
                <tr
                  key={r.id}
                  className="hover:bg-muted/30 cursor-pointer"
                >
                  <td className="px-3 py-2">
                    <Link
                      href={`/runs/${r.id}`}
                      className="block w-full text-foreground hover:underline"
                    >
                      {fmtDate(r.sealed_at || r.created_at)}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.total_discovered}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.total_verified}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.total_drafted}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums font-semibold">
                    {r.total_slated}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
