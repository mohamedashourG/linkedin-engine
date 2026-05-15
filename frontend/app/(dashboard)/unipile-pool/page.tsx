"use client";

/**
 * Unipile account pool dashboard.
 *
 * Lets the operator see every connected LinkedIn account, its role
 * (discovery / posting / stats / disabled), its live health (OK /
 * COOLDOWN / CREDENTIALS), per-capability daily usage vs cap, and
 * recent error counts. Two admin actions inline: reset a cooldown
 * window manually, change an account's role.
 *
 * Polls every 4s so the dashboard updates in near-real-time as the
 * engine rotates through the pool. KPIs at the top let you see at a
 * glance how many accounts are healthy vs need re-auth.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, AlertCircle, Activity, Clock } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import {
  unipilePoolApi,
  type PoolAccount,
  type PoolRole,
  type PoolStatus,
} from "@/lib/unipile-pool";

function statusBadge(status: PoolStatus, cooldownSec: number | null) {
  if (cooldownSec && cooldownSec > 0) {
    return (
      <Badge variant="outline" className="border-amber-400 text-amber-700">
        <Clock className="mr-1 h-3 w-3" />
        cooldown {fmtSeconds(cooldownSec)}
      </Badge>
    );
  }
  if (status === "OK") {
    return <Badge variant="default" className="bg-emerald-600">OK</Badge>;
  }
  if (status === "CREDENTIALS") {
    return (
      <Badge variant="destructive">
        <AlertCircle className="mr-1 h-3 w-3" />
        needs re-auth
      </Badge>
    );
  }
  if (status === "DISABLED") {
    return <Badge variant="outline">disabled</Badge>;
  }
  return <Badge variant="outline">{status}</Badge>;
}

function roleBadge(role: PoolRole) {
  const map: Record<PoolRole, { label: string; cls: string }> = {
    discovery: { label: "discovery", cls: "bg-blue-100 text-blue-800" },
    posting:   { label: "posting",   cls: "bg-purple-100 text-purple-800" },
    stats:     { label: "stats",     cls: "bg-slate-100 text-slate-800" },
    disabled:  { label: "disabled",  cls: "bg-gray-100 text-gray-600" },
  };
  const m = map[role] || map.discovery;
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${m.cls}`}>
      {m.label}
    </span>
  );
}

function fmtSeconds(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (!t) return "never";
  const delta = Math.floor((Date.now() - t) / 1000);
  if (delta < 30) return "just now";
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

/** Inline progress bar — daily-usage / daily-cap for one capability. */
function UsageBar({
  label,
  used,
  cap,
}: {
  label: string;
  used: number;
  cap: number;
}) {
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  const tone =
    pct >= 90
      ? "bg-red-500"
      : pct >= 70
      ? "bg-amber-500"
      : "bg-emerald-500";
  return (
    <div>
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>{label}</span>
        <span className="tabular-nums">
          {used} / {cap}
        </span>
      </div>
      <div className="h-1.5 w-full rounded bg-muted">
        <div
          className={`h-full rounded ${tone}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function UnipilePoolPage() {
  const qc = useQueryClient();
  const [pendingRoleFor, setPendingRoleFor] = useState<string | null>(null);

  const poolQ = useQuery({
    queryKey: ["unipile-pool"],
    queryFn: unipilePoolApi.list,
    refetchInterval: 4000,  // live updates as the engine rotates
  });

  const syncMut = useMutation({
    mutationFn: unipilePoolApi.sync,
    onSuccess: (res) => {
      toast.success(
        `Synced from Unipile — ${res.created} added, ${res.updated} updated`,
      );
      qc.invalidateQueries({ queryKey: ["unipile-pool"] });
    },
    onError: (err: ApiError | Error) =>
      toast.error(err?.message || "Sync failed"),
  });

  const resetCooldownMut = useMutation({
    mutationFn: (accountId: string) =>
      unipilePoolApi.resetCooldown(accountId),
    onSuccess: () => {
      toast.success("Cooldown cleared");
      qc.invalidateQueries({ queryKey: ["unipile-pool"] });
    },
    onError: (err: ApiError | Error) =>
      toast.error(err?.message || "Failed to reset cooldown"),
  });

  const setRoleMut = useMutation({
    mutationFn: (vars: { accountId: string; role: PoolRole }) =>
      unipilePoolApi.setRole(vars.accountId, vars.role),
    onSuccess: (res) => {
      toast.success(`Role updated to ${res.role}`);
      setPendingRoleFor(null);
      qc.invalidateQueries({ queryKey: ["unipile-pool"] });
    },
    onError: (err: ApiError | Error) => {
      toast.error(err?.message || "Failed to update role");
      setPendingRoleFor(null);
    },
  });

  const data = poolQ.data;
  const accounts = data?.accounts || [];
  const summary = data?.summary;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Unipile account pool
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Multi-account rotation for LinkedIn discovery + engagement reads.
            Each account has its own humanlike throttle and daily cap;
            requests rotate LRU across healthy accounts so no individual
            session looks bot-paced.
          </p>
        </div>
        <Button
          onClick={() => syncMut.mutate()}
          disabled={syncMut.isPending}
          variant="outline"
        >
          <RefreshCw
            className={`mr-2 h-4 w-4 ${syncMut.isPending ? "animate-spin" : ""}`}
          />
          Sync from Unipile
        </Button>
      </div>

      {/* KPI tiles */}
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <KPI label="Total accounts" value={summary.total} />
          <KPI label="Healthy" value={summary.ok} tone="emerald" />
          <KPI label="In cooldown" value={summary.cooldown} tone="amber" />
          <KPI label="Need re-auth" value={summary.credentials} tone="red" />
        </div>
      )}

      {summary && (
        <div className="rounded-lg border bg-card p-3 text-sm">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-muted-foreground">
            <span>
              <strong className="text-foreground">{summary.discovery}</strong>{" "}
              discovery
            </span>
            <span>
              <strong className="text-foreground">{summary.posting}</strong>{" "}
              posting (per-operator)
            </span>
            <span>
              <strong className="text-foreground">{summary.stats}</strong>{" "}
              stats (engagement reads)
            </span>
            <span>
              <strong className="text-foreground">{summary.disabled}</strong>{" "}
              disabled
            </span>
          </div>
        </div>
      )}

      {/* Account table */}
      {poolQ.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : accounts.length === 0 ? (
        <div className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          No accounts in the pool yet. Click <strong>Sync from Unipile</strong>{" "}
          to import every connected LinkedIn account.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full min-w-[900px] text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Account</th>
                <th className="px-3 py-2 text-left">Role</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-left">Daily usage</th>
                <th className="px-3 py-2 text-right">Calls / errors</th>
                <th className="px-3 py-2 text-right">Last used</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {accounts.map((a) => (
                <AccountRow
                  key={a.account_id}
                  account={a}
                  isMenuOpen={pendingRoleFor === a.account_id}
                  onToggleMenu={() =>
                    setPendingRoleFor((cur) =>
                      cur === a.account_id ? null : a.account_id,
                    )
                  }
                  onSetRole={(role) =>
                    setRoleMut.mutate({ accountId: a.account_id, role })
                  }
                  onResetCooldown={() => resetCooldownMut.mutate(a.account_id)}
                  isResetPending={
                    resetCooldownMut.isPending &&
                    resetCooldownMut.variables === a.account_id
                  }
                  isRolePending={setRoleMut.isPending}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function KPI({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "emerald" | "amber" | "red";
}) {
  const toneCls = {
    emerald: "text-emerald-700",
    amber: "text-amber-700",
    red: "text-red-700",
  };
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          tone ? toneCls[tone] : ""
        }`}
      >
        {value}
      </div>
    </div>
  );
}

function AccountRow({
  account: a,
  isMenuOpen,
  onToggleMenu,
  onSetRole,
  onResetCooldown,
  isResetPending,
  isRolePending,
}: {
  account: PoolAccount;
  isMenuOpen: boolean;
  onToggleMenu: () => void;
  onSetRole: (role: PoolRole) => void;
  onResetCooldown: () => void;
  isResetPending: boolean;
  isRolePending: boolean;
}) {
  // Compute capability availability for the UI's progress bars.
  // Posting accounts don't use the discovery cap, so we skip those bars
  // and just show how many comments / replies they've sent (post_comment
  // doesn't have a daily cap by design — it's throttled by gap, not volume).
  const isPosting = a.role === "posting";
  const usage = a.daily_usage || {};
  const caps = a.daily_caps || {};
  const hasCooldown = (a.cooldown_seconds_remaining ?? 0) > 0;

  return (
    <tr className="hover:bg-muted/30">
      <td className="px-3 py-2 align-top">
        <div className="font-medium">{a.display_name || "(unnamed)"}</div>
        <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
          {a.account_id.slice(0, 22)}…
        </div>
        <div className="mt-0.5 text-[10px] text-muted-foreground">
          proxy: <span className="font-medium">{a.proxy_country}</span>
          {" · "}
          caps: {a.capabilities.join(", ")}
        </div>
      </td>
      <td className="px-3 py-2 align-top">{roleBadge(a.role)}</td>
      <td className="px-3 py-2 align-top">
        {statusBadge(a.status, a.cooldown_seconds_remaining)}
        {a.consecutive_429s > 0 && (
          <div className="mt-1 text-[10px] text-amber-700">
            {a.consecutive_429s} recent 429
            {a.consecutive_429s === 1 ? "" : "s"}
          </div>
        )}
      </td>
      <td className="px-3 py-2 align-top">
        {isPosting ? (
          <div className="text-[10px] italic text-muted-foreground">
            no daily cap (gap-throttled)
          </div>
        ) : (
          <div className="space-y-1">
            <UsageBar
              label="search"
              used={Number(usage.search) || 0}
              cap={Number(caps.search) || 0}
            />
            <UsageBar
              label="profile view"
              used={Number(usage.profile_view) || 0}
              cap={Number(caps.profile_view) || 0}
            />
            <UsageBar
              label="post fetch"
              used={Number(usage.post_fetch) || 0}
              cap={Number(caps.post_fetch) || 0}
            />
          </div>
        )}
      </td>
      <td className="px-3 py-2 text-right align-top tabular-nums">
        <div className="text-foreground">{a.total_calls.toLocaleString()}</div>
        <div className="text-[10px] text-muted-foreground">
          {a.total_errors} errors
        </div>
      </td>
      <td className="px-3 py-2 text-right align-top text-xs text-muted-foreground">
        {fmtRelative(a.last_used_at)}
        {a.last_429_at && (
          <div className="text-[10px] text-amber-700">
            last 429 {fmtRelative(a.last_429_at)}
          </div>
        )}
      </td>
      <td className="px-3 py-2 text-right align-top">
        <div className="flex flex-col items-end gap-1">
          {hasCooldown && (
            <Button
              size="sm"
              variant="outline"
              onClick={onResetCooldown}
              disabled={isResetPending}
              title="Clear cooldown and put this account back in rotation"
            >
              <Activity className="mr-1 h-3 w-3" />
              Reset cooldown
            </Button>
          )}
          <div className="relative">
            <Button
              size="sm"
              variant="ghost"
              onClick={onToggleMenu}
              disabled={isRolePending}
            >
              {isMenuOpen ? "Close" : "Set role"}
            </Button>
            {isMenuOpen && (
              <div className="absolute right-0 z-10 mt-1 flex flex-col rounded border bg-card shadow-md">
                {(
                  ["discovery", "posting", "stats", "disabled"] as PoolRole[]
                ).map((r) => (
                  <button
                    key={r}
                    type="button"
                    onClick={() => onSetRole(r)}
                    disabled={r === a.role}
                    className={`px-3 py-1.5 text-left text-xs hover:bg-muted ${
                      r === a.role
                        ? "bg-muted font-semibold"
                        : ""
                    }`}
                  >
                    {r}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}
