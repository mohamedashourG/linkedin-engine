/**
 * Client for the Unipile account pool dashboard + admin actions.
 *
 * The pool routes every discovery read (keyword search, RULE 24,
 * /users/{slug} fallback) and every engagement-refresh read across a
 * rotating set of LinkedIn-connected accounts. Each account has its
 * own humanlike throttle, daily cap, and cooldown state — this lib
 * exposes that state for the dashboard UI and a couple of admin
 * actions (sync from Unipile, reset cooldown, change role).
 */

import { api } from "@/lib/api";

export type PoolRole = "discovery" | "posting" | "stats" | "disabled";
export type PoolStatus = "OK" | "COOLDOWN" | "CREDENTIALS" | "DISABLED";

export type PoolAccount = {
  account_id: string;
  display_name: string;
  operator_id: string | null;
  role: PoolRole;
  capabilities: string[];
  proxy_country: string;
  status: PoolStatus;
  cooldown_until: string | null;
  cooldown_seconds_remaining: number | null;
  daily_caps: Record<string, number>;
  daily_usage: Record<string, number | string>;  // includes reset_at as string
  last_used_at: string | null;
  last_429_at: string | null;
  consecutive_errors: number;
  consecutive_429s: number;
  total_calls: number;
  total_errors: number;
};

export type PoolListResponse = {
  accounts: PoolAccount[];
  summary: {
    total: number;
    ok: number;
    cooldown: number;
    credentials: number;
    disabled: number;
    discovery: number;
    posting: number;
    stats: number;
  };
};

export type PoolSelection = {
  /** null = no filter (all discovery accounts); [] = no accounts (pool
   *  effectively disabled); [...] = strict allowlist. */
  account_ids: string[] | null;
  saved_at: string | null;
};

export type RunPoolState = {
  slate_run_id: string;
  /** What was on the slate_run.pool_account_ids when the run started.
   *  null = no allowlist set; otherwise the exact account_ids that
   *  could be acquired during this run. */
  pool_account_ids_snapshot: string[] | null;
  accounts: PoolAccount[];
};

export const unipilePoolApi = {
  /** Full pool snapshot — every account with live state + summary KPIs. */
  list: () => api.get<PoolListResponse>("/api/unipile-pool/accounts"),

  /** Refresh accounts from Unipile. Idempotent — safe to spam. */
  sync: () =>
    api.post<{ created: number; updated: number }>(
      "/api/unipile-pool/sync",
    ),

  /** Manually clear an account's cooldown_until + 429 counters. Use
   *  when a 429 was obviously transient and you want the account back
   *  in rotation immediately. */
  resetCooldown: (accountId: string) =>
    api.post<{ account_id: string; reset: boolean }>(
      `/api/unipile-pool/accounts/${encodeURIComponent(accountId)}/reset-cooldown`,
    ),

  /** Change an account's role (discovery / posting / stats / disabled).
   *  Promoting out of disabled also restores status=OK. */
  setRole: (accountId: string, role: PoolRole) =>
    api.post<{ account_id: string; role: string; updated: boolean }>(
      `/api/unipile-pool/accounts/${encodeURIComponent(accountId)}/role`,
      { role },
    ),

  /** Operator's saved selection — which accounts FUTURE runs will use. */
  getSelection: () => api.get<PoolSelection>("/api/unipile-pool/selection"),

  /** Update the operator's selection. null = clear filter (all accounts);
   *  [] = block all; [...] = strict allowlist. */
  updateSelection: (account_ids: string[] | null) =>
    api.put<PoolSelection>("/api/unipile-pool/selection", { account_ids }),

  /** Pool state snapshot for ONE slate_run — shows which accounts
   *  participated in that specific run + their current state. */
  runState: (slateRunId: string) =>
    api.get<RunPoolState>(
      `/api/unipile-pool/runs/${encodeURIComponent(slateRunId)}`,
    ),
};
