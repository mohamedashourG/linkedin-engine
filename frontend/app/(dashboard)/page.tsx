"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Button } from "@/components/ui/button";
import { KpiTile } from "@/components/charts/kpi-tile";
import { RangePicker } from "@/components/charts/range-picker";
import { analyticsApi, type Range } from "@/lib/analytics";

export default function DashboardOverview() {
  const [range, setRange] = useState<Range>("30d");
  const overviewQ = useQuery({
    queryKey: ["analytics-overview", range],
    queryFn: () => analyticsApi.overview(range),
  });
  const funnelQ = useQuery({
    queryKey: ["analytics-funnel", range],
    queryFn: () => analyticsApi.funnel(range),
  });

  const overview = overviewQ.data;
  const funnel = funnelQ.data;
  const replyRatePct =
    overview && overview.shipped > 0
      ? `${Math.round(overview.reply_rate * 100)}%`
      : "—";

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Overview</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            How the engine is performing for you over the last {range}.
          </p>
        </div>
        <RangePicker value={range} onChange={setRange} />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <KpiTile
          label="Comments shipped"
          value={overview?.shipped ?? "—"}
          hint={`${overview?.sealed_runs ?? 0} sealed runs`}
        />
        <KpiTile
          label="Replies received"
          value={overview?.replies ?? "—"}
          hint={`${replyRatePct} reply rate`}
          accent="success"
        />
        <KpiTile
          label="CRs sent"
          value={overview?.crs_sent ?? "—"}
          hint={`${overview?.crs_accepted ?? 0} accepted`}
        />
        <KpiTile
          label="Bookings"
          value={overview?.bookings ?? "—"}
          hint={`${overview?.converted_leads ?? 0} CONVERTED leads`}
          accent="success"
        />
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <KpiTile
          label="Active leads"
          value={overview?.active_leads ?? "—"}
        />
        <KpiTile
          label="Stalled leads"
          value={overview?.stalled_leads ?? "—"}
          accent="warning"
          hint="ACTIVE → STALLED after 30d untouched"
        />
        <KpiTile
          label="Force-aborted runs"
          value={overview?.aborted_runs ?? "—"}
          accent={overview?.aborted_runs ? "warning" : "default"}
          hint="RULE 23 invariant breaches"
        />
      </div>

      <div className="rounded-lg border bg-background p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Funnel</h2>
          <Link href="/analytics" className="text-xs text-muted-foreground hover:underline">
            Open analytics →
          </Link>
        </div>
        <div className="mt-3 h-64 w-full">
          {!funnel ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Loading funnel…
            </div>
          ) : funnel.stages.every((s) => s.count === 0) ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              No activity in the last {range}.
            </div>
          ) : (
            <ResponsiveContainer>
              <BarChart
                data={funnel.stages}
                layout="vertical"
                margin={{ left: 20, right: 30, top: 5, bottom: 5 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.05)" />
                <XAxis type="number" allowDecimals={false} />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={140}
                  tick={{ fontSize: 12 }}
                />
                <Tooltip />
                <Bar dataKey="count" radius={[0, 4, 4, 0]}>
                  {funnel.stages.map((_, i) => (
                    <Cell
                      key={i}
                      fill={`hsl(222 47% ${15 + i * 8}%)`}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        <Button asChild variant="outline">
          <Link href="/today">Open today's slate →</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/pipeline">Pipeline →</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/eod">End of day →</Link>
        </Button>
      </div>
    </div>
  );
}
