"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { RangePicker } from "@/components/charts/range-picker";
import { GateFunnel } from "@/components/charts/gate-funnel";
import { analyticsApi, type Range } from "@/lib/analytics";

const TYPE_LABELS: Record<string, string> = {
  A: "A · Substantive",
  B: "B · War story",
  C: "C · Reframe",
  D: "D · Encouragement",
  E: "E · Question",
  F: "F · Punchy",
};

export default function AnalyticsPage() {
  const [range, setRange] = useState<Range>("30d");
  const funnelQ = useQuery({
    queryKey: ["analytics-funnel", range],
    queryFn: () => analyticsApi.funnel(range),
  });
  const byTypeQ = useQuery({
    queryKey: ["analytics-by-type", range],
    queryFn: () => analyticsApi.byType(range),
  });
  const byScoreQ = useQuery({
    queryKey: ["analytics-by-score", range],
    queryFn: () => analyticsApi.byScore(range),
  });

  const byTypeRows = (byTypeQ.data?.rows ?? []).map((r) => ({
    label: TYPE_LABELS[r.type] ?? r.type,
    shipped: r.shipped,
    replies: r.replies,
    rate: Math.round(r.reply_rate * 100),
  }));
  const byScoreRows = byScoreQ.data?.rows ?? [];

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Analytics</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Funnel, comment-type performance, ICP-score performance.
          </p>
        </div>
        <RangePicker value={range} onChange={setRange} />
      </div>

      <GateFunnel />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Funnel</CardTitle>
          <CardDescription>
            Comments → replies → CRs → DMs → bookings, last {range}.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-72 w-full">
            {!funnelQ.data ? (
              <Loader />
            ) : (
              <ResponsiveContainer>
                <BarChart
                  data={funnelQ.data.stages}
                  layout="vertical"
                  margin={{ left: 30, right: 40, top: 5, bottom: 5 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.05)" />
                  <XAxis type="number" allowDecimals={false} />
                  <YAxis
                    type="category"
                    dataKey="name"
                    width={150}
                    tick={{ fontSize: 12 }}
                  />
                  <Tooltip />
                  <Bar dataKey="count" radius={[0, 4, 4, 0]}>
                    {funnelQ.data.stages.map((_, i) => (
                      <Cell key={i} fill={`hsl(222 47% ${15 + i * 8}%)`} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Reply rate by comment type</CardTitle>
            <CardDescription>
              How often each comment type lands a reply, last {range}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64 w-full">
              {!byTypeQ.data ? (
                <Loader />
              ) : (
                <ResponsiveContainer>
                  <BarChart
                    data={byTypeRows}
                    margin={{ left: 0, right: 10, top: 10, bottom: 5 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.05)" />
                    <XAxis
                      dataKey="label"
                      tick={{ fontSize: 11 }}
                      angle={-15}
                      textAnchor="end"
                      height={60}
                    />
                    <YAxis
                      tickFormatter={(v) => `${v}%`}
                      tick={{ fontSize: 11 }}
                      domain={[0, "auto"]}
                    />
                    <Tooltip
                      formatter={(value: number, key: string) =>
                        key === "rate" ? `${value}%` : value
                      }
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Bar dataKey="rate" name="reply rate (%)" fill="hsl(222 47% 18%)" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="shipped" name="shipped" fill="hsl(222 47% 60%)" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Reply rate by ICP score band</CardTitle>
            <CardDescription>
              Higher-scored authors should reply more. Last {range}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64 w-full">
              {!byScoreQ.data ? (
                <Loader />
              ) : (
                <ResponsiveContainer>
                  <BarChart
                    data={byScoreRows.map((r) => ({
                      band: `ICP ${r.band}`,
                      shipped: r.shipped,
                      replies: r.replies,
                      rate: Math.round(r.reply_rate * 100),
                    }))}
                    margin={{ left: 0, right: 10, top: 10, bottom: 5 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.05)" />
                    <XAxis dataKey="band" tick={{ fontSize: 11 }} />
                    <YAxis
                      tickFormatter={(v) => `${v}%`}
                      tick={{ fontSize: 11 }}
                      domain={[0, "auto"]}
                    />
                    <Tooltip
                      formatter={(value: number, key: string) =>
                        key === "rate" ? `${value}%` : value
                      }
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Bar dataKey="rate" name="reply rate (%)" fill="hsl(222 47% 18%)" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="shipped" name="shipped" fill="hsl(222 47% 60%)" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Loader() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      Loading…
    </div>
  );
}
