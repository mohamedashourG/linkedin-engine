"use client";

/**
 * Per-run pool-state panel for the Run detail page.
 *
 * Shows the EXACT account allowlist that was active for this slate_run
 * (snapshotted at run start), along with each account's current live
 * health/usage. Lets the operator audit "which accounts participated
 * in this run" and spot anomalies — e.g. an account that should have
 * been used but never got rotated into.
 *
 * Polls at the same cadence as the live-costs panel so a run that's
 * still building updates in near-real-time.
 */

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Activity } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { unipilePoolApi, type PoolAccount } from "@/lib/unipile-pool";

function fmtSeconds(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function statusPill(a: PoolAccount) {
  if (a.cooldown_seconds_remaining && a.cooldown_seconds_remaining > 0) {
    return (
      <Badge variant="outline" className="border-amber-400 text-[10px] text-amber-700">
        cooldown {fmtSeconds(a.cooldown_seconds_remaining)}
      </Badge>
    );
  }
  if (a.status === "OK")
    return <Badge className="bg-emerald-600 text-[10px]">OK</Badge>;
  if (a.status === "CREDENTIALS")
    return (
      <Badge variant="destructive" className="text-[10px]">
        re-auth
      </Badge>
    );
  return <Badge variant="outline" className="text-[10px]">{a.status}</Badge>;
}

export function RunPoolPanel({
  slateRunId,
  isBuilding,
}: {
  slateRunId: string;
  isBuilding?: boolean;
}) {
  const stateQ = useQuery({
    queryKey: ["unipile-pool-run-state", slateRunId],
    queryFn: () => unipilePoolApi.runState(slateRunId),
    enabled: !!slateRunId,
    refetchInterval: isBuilding ? 4000 : 15000,
  });

  if (stateQ.isLoading) {
    return (
      <div className="space-y-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Pool accounts for this run
        </h2>
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  const data = stateQ.data;
  if (!data) return null;
  const snapshot = data.pool_account_ids_snapshot;
  const accounts = data.accounts || [];

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Pool accounts for this run
        </h2>
        <span className="text-xs text-muted-foreground">
          {snapshot === null ? (
            <span className="italic">no allowlist set — full discovery pool was eligible</span>
          ) : (
            <span>
              <strong className="text-foreground">{snapshot.length}</strong>{" "}
              account{snapshot.length === 1 ? "" : "s"} in the allowlist when this run started
            </span>
          )}
        </span>
      </div>

      {snapshot !== null && snapshot.length === 0 && (
        <div className="flex items-start gap-2 rounded border border-red-300 bg-red-50 p-3 text-xs text-red-900">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <strong>This run started with an empty allowlist.</strong> No pool
            accounts could be acquired for discovery — the engine would have
            fallen back to its non-pool legacy paths (operator's posting
            account only). Set a non-empty selection on Today's slate before
            the next run.
          </div>
        </div>
      )}

      {accounts.length === 0 && snapshot && snapshot.length > 0 && (
        <div className="rounded border bg-card p-3 text-xs text-muted-foreground">
          Snapshot referenced {snapshot.length} account
          {snapshot.length === 1 ? "" : "s"} but none of them exist in the
          pool collection anymore. Likely they were removed from Unipile
          after this run started.
        </div>
      )}

      {accounts.length > 0 && (
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-xs">
            <thead className="bg-muted/40 uppercase tracking-wide text-[10px] text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Account</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-left">Daily usage</th>
                <th className="px-3 py-2 text-right">Total calls</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {accounts.map((a) => {
                const usage = a.daily_usage || {};
                const caps = a.daily_caps || {};
                return (
                  <tr key={a.account_id} className="hover:bg-muted/30">
                    <td className="px-3 py-2 align-top">
                      <div className="font-medium">{a.display_name}</div>
                      <div className="text-[10px] text-muted-foreground">
                        proxy {a.proxy_country}
                      </div>
                    </td>
                    <td className="px-3 py-2 align-top">{statusPill(a)}</td>
                    <td className="px-3 py-2 align-top">
                      <div className="flex flex-col gap-0.5 text-[10px] tabular-nums">
                        <span>
                          search{" "}
                          <strong className="text-foreground">
                            {Number(usage.search) || 0}
                          </strong>
                          /{Number(caps.search) || 0}
                        </span>
                        <span>
                          profile{" "}
                          <strong className="text-foreground">
                            {Number(usage.profile_view) || 0}
                          </strong>
                          /{Number(caps.profile_view) || 0}
                        </span>
                        <span>
                          post{" "}
                          <strong className="text-foreground">
                            {Number(usage.post_fetch) || 0}
                          </strong>
                          /{Number(caps.post_fetch) || 0}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right align-top tabular-nums">
                      <div>{a.total_calls.toLocaleString()}</div>
                      {a.total_errors > 0 && (
                        <div className="text-[10px] text-amber-700">
                          {a.total_errors} errors
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
