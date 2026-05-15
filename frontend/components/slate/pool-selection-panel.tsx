"use client";

/**
 * Pool-account selection panel.
 *
 * Used on Today's slate to let the operator pick exactly which Unipile
 * accounts can participate in upcoming discovery runs. The selection is
 * persisted on the operator's user document and snapshotted onto each
 * new slate_run at creation — past runs keep the snapshot they started
 * with, so changes here only affect FUTURE runs.
 *
 * Anti-leakage guarantee: the selection is enforced at the
 * ``UnipileAccountPool.acquire()`` Mongo filter level inside the
 * backend's atomic find_one_and_update. Accounts outside the allowlist
 * literally cannot be claimed, even if a call site forgets to pass the
 * filter through. The UI here is just the operator-facing surface.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, AlertCircle } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { unipilePoolApi, type PoolAccount } from "@/lib/unipile-pool";

export function PoolSelectionPanel() {
  const qc = useQueryClient();

  const poolQ = useQuery({
    queryKey: ["unipile-pool"],
    queryFn: unipilePoolApi.list,
    refetchInterval: 8000,
  });
  const selectionQ = useQuery({
    queryKey: ["unipile-pool-selection"],
    queryFn: unipilePoolApi.getSelection,
  });

  // Track unsaved local selection so the user can stage changes before
  // saving. ``null`` here means "use saved value as the source of truth";
  // once the user clicks a checkbox we lift the staging set out of saved
  // state and treat it as dirty.
  const [pendingSelection, setPendingSelection] = useState<string[] | null>(
    null,
  );

  const discoveryAccounts = useMemo(() => {
    return (poolQ.data?.accounts || []).filter(
      (a) => a.role === "discovery",
    );
  }, [poolQ.data]);

  // The actual currently-saved allowlist. ``null`` from backend means
  // "no filter (all discovery accounts)". Default the UI to all-selected
  // in that case so the operator sees explicit state rather than an
  // ambiguous "none vs all" indicator.
  const savedSet = useMemo(() => {
    const saved = selectionQ.data?.account_ids;
    if (saved === null || saved === undefined) {
      return new Set(discoveryAccounts.map((a) => a.account_id));
    }
    return new Set(saved);
  }, [selectionQ.data, discoveryAccounts]);

  const liveSet = pendingSelection !== null
    ? new Set(pendingSelection)
    : savedSet;

  const dirty = pendingSelection !== null;
  const eligibleSelectedCount = useMemo(() => {
    return discoveryAccounts.filter(
      (a) => liveSet.has(a.account_id) && a.status === "OK",
    ).length;
  }, [discoveryAccounts, liveSet]);

  const saveMut = useMutation({
    mutationFn: () =>
      unipilePoolApi.updateSelection(
        pendingSelection !== null ? pendingSelection : null,
      ),
    onSuccess: () => {
      toast.success(
        `Pool selection saved — next run will use ${eligibleSelectedCount} healthy account${eligibleSelectedCount === 1 ? "" : "s"}.`,
      );
      setPendingSelection(null);
      qc.invalidateQueries({ queryKey: ["unipile-pool-selection"] });
    },
    onError: (err: ApiError | Error) =>
      toast.error(err?.message || "Save failed"),
  });

  function toggleAccount(accountId: string) {
    setPendingSelection((cur) => {
      const base = cur !== null ? new Set(cur) : new Set(savedSet);
      if (base.has(accountId)) base.delete(accountId);
      else base.add(accountId);
      return [...base];
    });
  }

  function selectAll() {
    setPendingSelection(discoveryAccounts.map((a) => a.account_id));
  }

  function clearAll() {
    setPendingSelection([]);
  }

  if (poolQ.isLoading || selectionQ.isLoading) {
    return <Skeleton className="h-32 w-full" />;
  }

  if (discoveryAccounts.length === 0) {
    return (
      <div className="rounded-lg border bg-amber-50 p-4 text-sm text-amber-900">
        <strong>No discovery accounts in the pool yet.</strong>
        <div className="mt-1">
          Go to <strong>Unipile pool</strong> in the sidebar, click{" "}
          <em>Sync from Unipile</em>, then come back here to select which
          accounts this operator can use for discovery.
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Pool selection for the next run
          </h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Only the checked accounts will be used for discovery (keyword
            search, RULE 24, profile fallback). The selection is snapshotted
            onto each new run so past runs keep their original allowlist.{" "}
            <strong>
              Accounts outside this list cannot be acquired by the engine —
              enforced at the database-claim level.
            </strong>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={selectAll}
            disabled={saveMut.isPending}
          >
            Select all
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={clearAll}
            disabled={saveMut.isPending}
          >
            Clear
          </Button>
          <Button
            size="sm"
            onClick={() => saveMut.mutate()}
            disabled={!dirty || saveMut.isPending}
          >
            {saveMut.isPending ? "Saving…" : dirty ? "Save selection" : "Saved"}
          </Button>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between text-xs">
        <div className="text-muted-foreground">
          {liveSet.size} of {discoveryAccounts.length} accounts selected
          {" · "}
          <span
            className={
              eligibleSelectedCount === 0
                ? "font-semibold text-red-700"
                : "font-medium text-emerald-700"
            }
          >
            {eligibleSelectedCount} healthy
          </span>
        </div>
        {dirty && (
          <div className="text-amber-700">
            unsaved changes — click <em>Save selection</em> to apply
          </div>
        )}
      </div>

      {eligibleSelectedCount === 0 && (
        <div className="mt-2 flex items-start gap-2 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-900">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <strong>Warning:</strong> zero healthy accounts in the current
            selection. Saving this would block all discovery acquires for
            future runs.
          </div>
        </div>
      )}

      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {discoveryAccounts.map((a) => (
          <AccountCheckbox
            key={a.account_id}
            account={a}
            checked={liveSet.has(a.account_id)}
            onToggle={() => toggleAccount(a.account_id)}
            disabled={saveMut.isPending}
          />
        ))}
      </div>
    </div>
  );
}

function AccountCheckbox({
  account: a,
  checked,
  onToggle,
  disabled,
}: {
  account: PoolAccount;
  checked: boolean;
  onToggle: () => void;
  disabled: boolean;
}) {
  const isHealthy = a.status === "OK" && !a.cooldown_seconds_remaining;

  return (
    <label
      className={`flex items-start gap-2 rounded border p-2 text-xs ${
        checked
          ? isHealthy
            ? "border-emerald-300 bg-emerald-50/50"
            : "border-amber-300 bg-amber-50/50"
          : "border-border bg-card"
      } ${disabled ? "opacity-60" : "cursor-pointer hover:bg-muted/30"}`}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        disabled={disabled}
        className="mt-0.5 h-4 w-4"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <div className="truncate font-medium">{a.display_name}</div>
          {isHealthy ? (
            <Badge className="bg-emerald-600 text-[9px]">OK</Badge>
          ) : a.status === "CREDENTIALS" ? (
            <Badge variant="destructive" className="text-[9px]">
              re-auth
            </Badge>
          ) : a.cooldown_seconds_remaining ? (
            <Badge
              variant="outline"
              className="border-amber-400 text-[9px] text-amber-700"
            >
              cooldown
            </Badge>
          ) : (
            <Badge variant="outline" className="text-[9px]">
              {a.status.toLowerCase()}
            </Badge>
          )}
        </div>
        <div className="mt-0.5 text-[10px] text-muted-foreground">
          proxy {a.proxy_country}
          {" · "}
          {a.total_calls.toLocaleString()} calls total
        </div>
      </div>
    </label>
  );
}
