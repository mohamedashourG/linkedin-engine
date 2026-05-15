"use client";

/**
 * Live cost breakdown for a slate run.
 *
 * Renders the ``slate_runs.cost_breakdown`` Mongo subdoc returned by
 * GET /api/slate/runs/{id}/costs. Re-renders on every parent poll, so the
 * grand $ total + per-provider counts climb in real time as Wiza /
 * Crustdata / APIDirect / Unipile / LLM events fire during a daily run.
 *
 * Caller controls polling cadence by passing an already-running
 * useQuery() result via ``costs`` / ``isLoading`` — keeps the component
 * stateless so it can sit on the Today page AND the runs detail page
 * without duplicating fetch logic.
 *
 * Layout decisions:
 *  - 4 grand-total tiles up top (Grand total $, Paid calls, LLM tokens in,
 *    LLM tokens out) — first thing the eye lands on.
 *  - Per-provider sub-cards in a responsive grid below. The Unipile card
 *    is special-cased to show count only (no $) with a "$0 / subscription"
 *    badge — operators asked specifically to see `search` vs `profile_view`
 *    counts because that's the LinkedIn-rate-limit-burning surface, even
 *    though Unipile bills flat.
 *  - LLM line items expose prompt + completion token counts alongside $.
 *  - Streaming indicator (pulsing dot) appears while the parent run is
 *    in `building` state so users know the totals are mid-flight.
 */

import { Skeleton } from "@/components/ui/skeleton";
import type { CostProvider, RunCostsResponse } from "@/lib/slate";

const PROVIDER_ORDER = ["wiza", "crustdata", "apidirect", "unipile", "llm"];
const PROVIDER_LABELS: Record<string, string> = {
  wiza: "Wiza",
  crustdata: "Crustdata",
  apidirect: "APIDirect",
  unipile: "Unipile",
  llm: "LLM",
};

function fmtUSD(n: number | undefined | null): string {
  const v = typeof n === "number" ? n : 0;
  if (Math.abs(v) >= 1) return `$${v.toFixed(2)}`;
  return `$${v.toFixed(4)}`;
}

function fmtInt(n: number | undefined | null): string {
  const v = typeof n === "number" ? n : 0;
  return v.toLocaleString();
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function LiveCostsPanel({
  costs,
  isLoading,
  isBuilding,
  /** When true, this is the top-of-page render on Today's slate. We
   * add an extra "live" affordance + slightly larger headline so users
   * can monitor spend without scrolling. */
  variant = "default",
}: {
  costs: RunCostsResponse | undefined;
  isLoading: boolean;
  isBuilding?: boolean;
  variant?: "default" | "today";
}) {
  if (isLoading && !costs) {
    return (
      <div className="space-y-2">
        <h2
          className={
            variant === "today"
              ? "text-base font-semibold"
              : "text-sm font-semibold uppercase tracking-wide text-muted-foreground"
          }
        >
          Live costs
        </h2>
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  const totals = costs?.totals ?? {};
  const providers = costs?.providers ?? {};
  const grandDollars = typeof totals.dollars === "number" ? totals.dollars : 0;
  const grandCalls = typeof totals.calls === "number" ? totals.calls : 0;
  const hasAny = grandCalls > 0;

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2
          className={
            variant === "today"
              ? "text-base font-semibold"
              : "text-sm font-semibold uppercase tracking-wide text-muted-foreground"
          }
        >
          Live costs
          {isBuilding && (
            <span className="ml-2 inline-flex items-center gap-1 text-xs normal-case font-normal text-blue-700">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
              </span>
              streaming
            </span>
          )}
        </h2>
        {costs?.updated_at && (
          <span className="text-xs text-muted-foreground">
            updated {fmtDate(costs.updated_at)}
          </span>
        )}
      </div>

      {/* Grand totals — 4 tiles. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Grand total
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {fmtUSD(grandDollars)}
          </div>
        </div>
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Paid calls
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {fmtInt(grandCalls)}
          </div>
        </div>
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            LLM tokens (in)
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {fmtInt(providers.llm?.totals?.prompt_tokens)}
          </div>
        </div>
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            LLM tokens (out)
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {fmtInt(providers.llm?.totals?.completion_tokens)}
          </div>
        </div>
      </div>

      {!hasAny ? (
        <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
          No paid calls recorded yet for this run.
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {PROVIDER_ORDER.filter((k) => providers[k]).map((k) => (
            <ProviderBreakdown
              key={k}
              label={PROVIDER_LABELS[k] ?? k}
              provider={providers[k]!}
              isLlm={k === "llm"}
              isUnipile={k === "unipile"}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ProviderBreakdown({
  label,
  provider,
  isLlm,
  isUnipile,
}: {
  label: string;
  provider: CostProvider;
  isLlm?: boolean;
  isUnipile?: boolean;
}) {
  const totalCount = provider.totals?.count ?? 0;
  const totalDollars = provider.totals?.dollars ?? 0;
  const lineItems = Object.entries(provider.line_items ?? {});

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-baseline justify-between gap-2">
        <div className="font-semibold">{label}</div>
        <div className="text-sm tabular-nums">
          {fmtInt(totalCount)}{" "}
          <span className="text-muted-foreground">
            call{totalCount === 1 ? "" : "s"}
          </span>
          {isUnipile ? (
            <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              $0 / subscription
            </span>
          ) : (
            <span className="ml-2 font-semibold">{fmtUSD(totalDollars)}</span>
          )}
        </div>
      </div>
      {lineItems.length > 0 && (
        <table className="mt-3 w-full text-xs">
          <thead className="text-muted-foreground">
            <tr className="border-b">
              <th className="py-1 text-left font-medium">Line item</th>
              <th className="py-1 text-right font-medium">Count</th>
              {isLlm && (
                <>
                  <th className="py-1 text-right font-medium">Tokens in</th>
                  <th className="py-1 text-right font-medium">Tokens out</th>
                </>
              )}
              {!isUnipile && (
                <th className="py-1 text-right font-medium">USD</th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y">
            {lineItems
              .sort(
                (a, b) =>
                  (b[1].dollars ?? 0) - (a[1].dollars ?? 0) ||
                  (b[1].count ?? 0) - (a[1].count ?? 0),
              )
              .map(([key, li]) => (
                <tr key={key}>
                  <td className="py-1.5 font-mono text-[11px] text-muted-foreground">
                    {key}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {fmtInt(li.count)}
                  </td>
                  {isLlm && (
                    <>
                      <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                        {fmtInt(li.prompt_tokens)}
                      </td>
                      <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                        {fmtInt(li.completion_tokens)}
                      </td>
                    </>
                  )}
                  {!isUnipile && (
                    <td className="py-1.5 text-right tabular-nums">
                      {fmtUSD(li.dollars)}
                    </td>
                  )}
                </tr>
              ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
