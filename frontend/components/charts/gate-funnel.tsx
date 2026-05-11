"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  analyticsApi,
  type GateDropGroup,
  type GateDropPost,
  type GateFunnelScope,
  type GateStage,
  type Range,
} from "@/lib/analytics";
import { cn } from "@/lib/utils";

// ────────────────────────────────────────────────────────────────────────────
// Visual constants
// ────────────────────────────────────────────────────────────────────────────

type StageKey =
  | "discovery"
  | "verification"
  | "cheap_gates"
  | "expensive_gates"
  | "drafter"
  | "other";

const STAGE_META: Record<
  StageKey,
  { label: string; helper: string; color: string; bg: string; ring: string }
> = {
  discovery: {
    label: "Discovery",
    helper:
      "Posts pulled from Unipile, APIDirect, Crustdata or Exa, then scored inline against the operator's ICP.",
    color: "#06b6d4",
    bg: "bg-cyan-500/10",
    ring: "ring-cyan-500/40",
  },
  verification: {
    label: "Verification",
    helper:
      "Recency + snippet sanity check. No LLM cost. Drops too-old, too-thin, or unresolvable posts.",
    color: "#94a3b8",
    bg: "bg-slate-500/10",
    ring: "ring-slate-500/40",
  },
  cheap_gates: {
    label: "Cheap gates",
    helper:
      "LLM-judged but author-context-free. Drops recruiter/vendor/news posts and low-signal content.",
    color: "#f59e0b",
    bg: "bg-amber-500/10",
    ring: "ring-amber-500/40",
  },
  expensive_gates: {
    label: "Expensive gates",
    helper:
      "Full LLM ICP scoring with author title + industry + geo + stage. The most expensive gate.",
    color: "#ef4444",
    bg: "bg-red-500/10",
    ring: "ring-red-500/40",
  },
  drafter: {
    label: "Drafter / Validator",
    helper:
      "Voice-matched LLM comment generation + token/buzzword validation.",
    color: "#8b5cf6",
    bg: "bg-violet-500/10",
    ring: "ring-violet-500/40",
  },
  other: {
    label: "Other",
    helper: "Unclassified drops.",
    color: "#64748b",
    bg: "bg-slate-400/10",
    ring: "ring-slate-400/40",
  },
};

const STAGE_HELPERS: Record<string, string> = {
  discovered:
    "All posts pulled across every enabled discovery source (Unipile keyword + RULE 24 people search, APIDirect, Crustdata, Exa).",
  verified:
    "Passed recency + snippet checks. Drops here are post-too-old or empty/thin text — no LLM was called.",
  cheap_passed:
    "Passed the cheap LLM gates (non-buyer + post-quality). These filters knock out roughly two-thirds of remaining posts before the expensive gates.",
  gate_passed:
    "Passed the analyst-reportage gate AND scored ≥ threshold on the ICP rubric. These are candidates eligible for drafting.",
  drafted:
    "Voice-matched comment drafted and validated (no banned tokens, no buzzwords).",
  slated:
    "Picked by the allocator under the comment-type quotas and sealed into today's slate.",
};

type ScopeKey = "run-latest" | "agg-7d" | "agg-30d" | "agg-90d";
const SCOPE_OPTIONS: { key: ScopeKey; label: string; scope: GateFunnelScope }[] = [
  { key: "run-latest", label: "Latest run", scope: { kind: "run" } },
  { key: "agg-7d", label: "Aggregate · 7d", scope: { kind: "aggregate", range: "7d" } },
  { key: "agg-30d", label: "Aggregate · 30d", scope: { kind: "aggregate", range: "30d" } },
  { key: "agg-90d", label: "Aggregate · 90d", scope: { kind: "aggregate", range: "90d" } },
];

const SOURCE_LABELS: Record<string, string> = {
  unipile_kw: "Unipile · keyword",
  unipile_inline_drop: "Unipile · inline",
  unipile_people: "Unipile · people (RULE 24)",
  apidirect_kw: "APIDirect",
  crustdata: "Crustdata · inbox",
  crustdata_screener: "Crustdata · screener",
  exa_kw: "Exa",
  contact_seed: "Contact seed",
};

const FORMAT_PCT = (n: number) =>
  Number.isFinite(n) ? `${n > 0 ? "+" : ""}${n.toFixed(0)}%` : "—";

// ────────────────────────────────────────────────────────────────────────────
// Main component
// ────────────────────────────────────────────────────────────────────────────

export function GateFunnel({ slateRunId }: { slateRunId?: string }) {
  const [scopeKey, setScopeKey] = useState<ScopeKey>("run-latest");
  const [selected, setSelected] = useState<string | null>(null);

  const activeScope: GateFunnelScope = slateRunId
    ? { kind: "run", slateRunId }
    : SCOPE_OPTIONS.find((s) => s.key === scopeKey)!.scope;

  const q = useQuery({
    queryKey: ["gate-funnel", scopeKey, slateRunId ?? "latest"],
    queryFn: () => analyticsApi.gateFunnel(activeScope),
    refetchInterval: 8000,
  });

  const data = q.data;
  const hasData =
    !!data &&
    (data.scope === "aggregate" ? data.runs_included > 0 : !!data.slate_run_id);

  // Group drops by stage for the "drops by stage" section.
  const dropsByStage = useMemo(() => {
    if (!data) return [] as { stage: StageKey; total: number; groups: GateDropGroup[] }[];
    const m = new Map<StageKey, GateDropGroup[]>();
    for (const d of data.drops) {
      const k = (d.stage as StageKey) in STAGE_META ? (d.stage as StageKey) : "other";
      const arr = m.get(k) ?? [];
      arr.push(d);
      m.set(k, arr);
    }
    // stage order matches funnel flow
    const order: StageKey[] = [
      "discovery",
      "verification",
      "cheap_gates",
      "expensive_gates",
      "drafter",
      "other",
    ];
    return order
      .filter((k) => m.has(k))
      .map((k) => ({
        stage: k,
        total: (m.get(k) ?? []).reduce((acc, g) => acc + g.count, 0),
        groups: (m.get(k) ?? []).sort((a, b) => b.count - a.count),
      }));
  }, [data]);

  const selectedGroup: GateDropGroup | null =
    data?.drops.find((d) => d.reason === selected) ?? null;

  // Header
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <CardTitle>Discovery → Slate funnel</CardTitle>
            <CardDescription>
              Per-stage retention with full drop diagnostics. Each drop carries
              the post URL + LLM rationale so you can audit any decision in one click.
            </CardDescription>
          </div>
          <ScopeMeta data={data} />
        </div>
        {!slateRunId && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {SCOPE_OPTIONS.map((opt) => (
              <Button
                key={opt.key}
                size="sm"
                variant={scopeKey === opt.key ? "default" : "outline"}
                onClick={() => {
                  setScopeKey(opt.key);
                  setSelected(null);
                }}
                className="h-7 text-xs"
              >
                {opt.label}
              </Button>
            ))}
          </div>
        )}
      </CardHeader>

      <CardContent className="space-y-6">
        {q.isLoading && !data && (
          <div className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
            Loading funnel…
          </div>
        )}
        {!q.isLoading && !hasData && (
          <div className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
            {data?.scope === "aggregate"
              ? `No slate runs found in the past ${data?.range}.`
              : "No slate runs yet. Trigger the first run from Settings or the worker CLI."}
          </div>
        )}

        {hasData && data && (
          <>
            <StageFunnel stages={data.stages} />
            <DropsByStage
              groups={dropsByStage}
              selected={selected}
              onSelect={setSelected}
            />
            {selectedGroup && (
              <DropDrilldown
                group={selectedGroup}
                onClose={() => setSelected(null)}
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Header meta (right-aligned scope info)
// ────────────────────────────────────────────────────────────────────────────

function ScopeMeta({ data }: { data: ReturnType<typeof useQuery>["data"] }) {
  if (!data) return null;
  const d = data as {
    scope: string;
    range: string | null;
    runs_included: number;
    slate_run_id: string | null;
    run_date: string | null;
    status: string | null;
  };
  const isAgg = d.scope === "aggregate";
  if (isAgg) {
    return (
      <div className="text-right text-xs text-muted-foreground space-y-1">
        <div>
          scope{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-medium">
            aggregate · {d.range}
          </code>
        </div>
        <div>{d.runs_included} run(s) included</div>
      </div>
    );
  }
  return (
    <div className="text-right text-xs text-muted-foreground space-y-1">
      <div>
        run{" "}
        <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-medium">
          {d.slate_run_id?.slice(-6) ?? "—"}
        </code>
      </div>
      <div>{d.run_date ? new Date(d.run_date).toLocaleString() : "—"}</div>
      <div>
        status:{" "}
        <Badge variant="outline" className="ml-0.5">
          {d.status ?? "?"}
        </Badge>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Stage funnel — proportional bars with conversion deltas
// ────────────────────────────────────────────────────────────────────────────

function StageFunnel({ stages }: { stages: GateStage[] }) {
  const max = Math.max(1, ...stages.map((s) => s.count));
  return (
    <section className="space-y-1">
      <SectionLabel
        title="Stage retention"
        hint="Width is proportional to the count at each stage. Red deltas mark drops, green marks gains."
      />
      <div className="space-y-1.5">
        {stages.map((s, i) => {
          const prev = i > 0 ? stages[i - 1].count : null;
          const delta = prev != null ? s.count - prev : null;
          const pct = prev != null && prev > 0 ? (delta! / prev) * 100 : null;
          const width = Math.max(4, (s.count / max) * 100); // min 4% so the row is always visible
          return (
            <div key={s.key} className="grid grid-cols-[140px_1fr_120px] items-center gap-3">
              <div className="text-xs">
                <div className="font-medium" title={STAGE_HELPERS[s.key] ?? ""}>
                  {s.label}
                </div>
                {STAGE_HELPERS[s.key] && (
                  <div className="text-[10px] text-muted-foreground/80 line-clamp-1">
                    {STAGE_HELPERS[s.key]}
                  </div>
                )}
              </div>
              <div className="relative h-7 rounded bg-muted/40">
                <div
                  className="h-full rounded bg-gradient-to-r from-foreground/85 to-foreground/70 transition-[width] duration-300"
                  style={{ width: `${width}%` }}
                />
                <div className="absolute inset-y-0 left-2 flex items-center text-[11px] font-medium text-background mix-blend-difference">
                  {s.count}
                </div>
              </div>
              <div className="text-right text-xs tabular-nums">
                {pct == null ? (
                  <span className="text-muted-foreground">—</span>
                ) : (
                  <span
                    className={cn(
                      delta! < 0 ? "text-red-600" : "text-emerald-600",
                    )}
                  >
                    {FORMAT_PCT(pct)}{" "}
                    <span className="text-muted-foreground">
                      ({delta! > 0 ? "+" : ""}
                      {delta})
                    </span>
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Drops by stage — grouped cards with mini-bars per reason
// ────────────────────────────────────────────────────────────────────────────

function DropsByStage({
  groups,
  selected,
  onSelect,
}: {
  groups: { stage: StageKey; total: number; groups: GateDropGroup[] }[];
  selected: string | null;
  onSelect: (reason: string | null) => void;
}) {
  if (groups.length === 0) {
    return (
      <section>
        <SectionLabel title="Drops by stage" hint="Nothing dropped — every post made it through." />
        <div className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-sm text-muted-foreground">
          No drops recorded.
        </div>
      </section>
    );
  }

  const totalAll = groups.reduce((acc, g) => acc + g.total, 0);

  return (
    <section className="space-y-3">
      <SectionLabel
        title="Drops by stage"
        hint={`${totalAll} total drops across ${groups.length} stage(s). Click any reason to inspect the posts.`}
      />
      <div className="grid gap-3 lg:grid-cols-2">
        {groups.map((g) => (
          <StageGroupCard
            key={g.stage}
            stage={g.stage}
            total={g.total}
            groups={g.groups}
            selected={selected}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  );
}

function StageGroupCard({
  stage,
  total,
  groups,
  selected,
  onSelect,
}: {
  stage: StageKey;
  total: number;
  groups: GateDropGroup[];
  selected: string | null;
  onSelect: (reason: string | null) => void;
}) {
  const meta = STAGE_META[stage];
  const maxInGroup = Math.max(1, ...groups.map((g) => g.count));
  return (
    <div
      className={cn(
        "rounded-md border p-3 transition-shadow",
        "ring-1 ring-inset",
        meta.bg,
        meta.ring,
      )}
    >
      <div className="flex items-baseline justify-between gap-3 border-b border-foreground/5 pb-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className="inline-block size-2.5 rounded-full"
              style={{ background: meta.color }}
            />
            <span className="font-semibold text-sm">{meta.label}</span>
          </div>
          <p className="mt-0.5 text-[11px] text-muted-foreground line-clamp-2">
            {meta.helper}
          </p>
        </div>
        <div className="text-right">
          <div className="text-lg font-semibold tabular-nums leading-none">{total}</div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            dropped
          </div>
        </div>
      </div>
      <ul className="mt-2 space-y-1.5">
        {groups.map((g) => {
          const w = Math.max(6, (g.count / maxInGroup) * 100);
          const isSel = selected === g.reason;
          return (
            <li key={g.reason}>
              <button
                onClick={() => onSelect(isSel ? null : g.reason)}
                className={cn(
                  "group block w-full rounded-md px-2 py-1.5 text-left transition-colors",
                  "hover:bg-foreground/5",
                  isSel ? "bg-foreground/10" : "",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs font-medium truncate">{g.label}</div>
                  <div className="text-xs tabular-nums shrink-0">
                    {g.count}
                  </div>
                </div>
                <div className="mt-1 h-1.5 overflow-hidden rounded bg-foreground/5">
                  <div
                    className="h-full rounded transition-[width] duration-300"
                    style={{
                      width: `${w}%`,
                      background: meta.color,
                      opacity: isSel ? 1 : 0.7,
                    }}
                  />
                </div>
                {g.description && (
                  <p className="mt-1 text-[10px] text-muted-foreground line-clamp-2 group-hover:line-clamp-none">
                    {g.description}
                  </p>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Drilldown — list of dropped posts for the selected reason
// ────────────────────────────────────────────────────────────────────────────

function DropDrilldown({
  group,
  onClose,
}: {
  group: GateDropGroup;
  onClose: () => void;
}) {
  const stage = (group.stage as StageKey) in STAGE_META ? (group.stage as StageKey) : "other";
  const meta = STAGE_META[stage];
  return (
    <section
      className={cn(
        "rounded-md border p-3",
        "ring-1 ring-inset",
        meta.bg,
        meta.ring,
      )}
    >
      <div className="flex items-start justify-between gap-3 border-b border-foreground/5 pb-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className="inline-block size-2.5 rounded-full"
              style={{ background: meta.color }}
            />
            <span className="text-sm font-semibold">{group.label}</span>
            <Badge variant="outline" className="text-[10px]">{meta.label}</Badge>
          </div>
          {group.description && (
            <p className="mt-1 text-xs text-muted-foreground">{group.description}</p>
          )}
          <p className="mt-1 text-[11px] text-muted-foreground">
            {group.count} total · showing {group.posts.length}
          </p>
        </div>
        <button
          onClick={onClose}
          className="text-xs text-muted-foreground underline shrink-0"
        >
          close
        </button>
      </div>
      <ul className="mt-3 grid gap-2">
        {group.posts.map((p) => (
          <DroppedPost key={p.candidate_id} p={p} accentColor={meta.color} />
        ))}
        {group.posts.length === 0 && (
          <li className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
            No sample posts available for this reason in this scope.
          </li>
        )}
      </ul>
    </section>
  );
}

function DroppedPost({ p, accentColor }: { p: GateDropPost; accentColor: string }) {
  const text =
    p.post_text.length > 280 ? p.post_text.slice(0, 280) + "…" : p.post_text;
  const sourceLabel = p.source ? SOURCE_LABELS[p.source] ?? p.source : null;
  return (
    <li
      className="rounded-md border bg-background p-3 text-sm shadow-sm"
      style={{ borderLeftWidth: 3, borderLeftColor: accentColor }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-semibold truncate">
              {p.author_name ?? "(unknown author)"}
            </span>
            {sourceLabel && (
              <Badge variant="outline" className="text-[10px]">
                {sourceLabel}
              </Badge>
            )}
            {p.matched_keyword && (
              <Badge variant="secondary" className="text-[10px]">
                kw: {p.matched_keyword}
              </Badge>
            )}
          </div>
          {p.author_title && (
            <div className="mt-0.5 text-[11px] text-muted-foreground line-clamp-1">
              {p.author_title}
            </div>
          )}
          <p className="mt-2 text-xs text-foreground/80 line-clamp-3">
            {text}
          </p>
        </div>
        {p.post_url && (
          <a
            href={p.post_url}
            target="_blank"
            rel="noreferrer noopener"
            className="shrink-0 rounded-md border bg-background px-2.5 py-1 text-xs font-medium hover:bg-muted"
          >
            Open ↗
          </a>
        )}
      </div>
      <div className="mt-2 rounded-md bg-muted/60 px-2.5 py-1.5 text-xs">
        <div>
          <span className="font-medium">Why dropped:</span>{" "}
          <span className="text-muted-foreground">{p.drop_reason}</span>
        </div>
        {p.gate_rationale && (
          <div className="mt-1 italic text-foreground/70">
            <span className="not-italic font-medium">Rationale:</span>{" "}
            {p.gate_rationale}
          </div>
        )}
      </div>
    </li>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Small reusables
// ────────────────────────────────────────────────────────────────────────────

function SectionLabel({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <h3 className="text-sm font-semibold">{title}</h3>
      {hint && (
        <p className="text-[11px] text-muted-foreground line-clamp-1">{hint}</p>
      )}
    </div>
  );
}
