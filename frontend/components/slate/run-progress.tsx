"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  Brain,
  CheckCircle2,
  ListFilter,
  Mail,
  PenLine,
  Search,
  ShieldCheck,
  Sparkles,
  UserCheck,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { SlateRun } from "@/lib/slate";

const STAGE_LABEL: Record<string, { label: string; icon: typeof Search }> = {
  discovery: { label: "Discovery", icon: Search },
  verification: { label: "Verification", icon: CheckCircle2 },
  profile_resolve: { label: "Profile resolve", icon: UserCheck },
  gates: { label: "4-gate filter", icon: ListFilter },
  allocator: { label: "Allocation", icon: Activity },
  drafter: { label: "Drafting", icon: PenLine },
  rule_23: { label: "RULE 23", icon: ShieldCheck },
  email_delivery: { label: "Sending email", icon: Mail },
  complete: { label: "Sealed", icon: Sparkles },
  aborted: { label: "Force aborted", icon: ShieldCheck },
};

const STAGE_ORDER = [
  "discovery",
  "verification",
  "profile_resolve",
  "gates",
  "allocator",
  "drafter",
  "rule_23",
  "email_delivery",
];

function formatEta(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "";
  if (seconds < 60) return `~${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s > 0 ? `~${m}m ${s}s` : `~${m}m`;
  const h = Math.floor(m / 60);
  return `~${h}h ${m % 60}m`;
}

function useElapsed(startedAt: string | null | undefined) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!startedAt) return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [startedAt]);
  if (!startedAt) return 0;
  return Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
}

export function RunProgress({ slate }: { slate: SlateRun }) {
  const stage = slate.current_stage ?? "";
  const cfg = STAGE_LABEL[stage] ?? { label: stage || "Idle", icon: Brain };
  const Icon = cfg.icon;
  const elapsedSinceStage = useElapsed(slate.stage_started_at);
  const eta = slate.stage_eta_seconds ?? 0;
  const adjustedEta = Math.max(0, eta - elapsedSinceStage);

  const processed = slate.stage_progress?.processed ?? 0;
  const total = slate.stage_progress?.total ?? 0;
  const stagePct =
    total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : null;

  // Overall pipeline percent: weight stages roughly by observed time spent.
  const stageIdx = STAGE_ORDER.indexOf(stage);
  const STAGE_WEIGHTS = [3, 1, 5, 75, 1, 8, 1, 6]; // matches observed run mix
  const totalWeight = STAGE_WEIGHTS.reduce((a, b) => a + b, 0);
  let overallPct = 0;
  if (stage === "complete") {
    overallPct = 100;
  } else if (stageIdx >= 0) {
    const before = STAGE_WEIGHTS.slice(0, stageIdx).reduce((a, b) => a + b, 0);
    const within =
      stagePct !== null ? (STAGE_WEIGHTS[stageIdx] * stagePct) / 100 : 0;
    overallPct = Math.min(99, Math.round(((before + within) / totalWeight) * 100));
  }

  return (
    <div className="rounded-xl border bg-background p-4 space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          <div
            className={cn(
              "flex h-8 w-8 items-center justify-center rounded-lg",
              stage === "complete"
                ? "bg-emerald-50 text-emerald-700"
                : stage === "aborted"
                  ? "bg-destructive/10 text-destructive"
                  : "bg-foreground/5 text-foreground",
            )}
          >
            <Icon className="h-4 w-4" />
          </div>
          <div>
            <div className="text-sm font-semibold">{cfg.label}</div>
            <div className="text-xs text-muted-foreground">
              {slate.stage_note ?? "Building today's slate"}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {stagePct !== null && (
            <Badge variant="muted" className="rounded-full font-mono text-[11px]">
              {processed}/{total}
            </Badge>
          )}
          {adjustedEta > 0 && (
            <Badge variant="info" className="rounded-full">
              {formatEta(adjustedEta)} remaining
            </Badge>
          )}
          {elapsedSinceStage > 0 && stage !== "complete" && (
            <Badge variant="muted" className="rounded-full font-mono text-[11px]">
              elapsed {formatEta(elapsedSinceStage)}
            </Badge>
          )}
        </div>
      </div>

      {/* Overall pipeline progress bar */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[11px] text-muted-foreground">
          <span>Overall pipeline</span>
          <span className="font-mono">{overallPct}%</span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              "h-full rounded-full transition-all duration-500",
              stage === "aborted" ? "bg-destructive" : "bg-foreground",
            )}
            style={{ width: `${overallPct}%` }}
          />
        </div>
      </div>

      {/* Per-stage stepper */}
      <div className="grid grid-cols-8 gap-1">
        {STAGE_ORDER.map((s, i) => {
          const done = stage === "complete" || (stageIdx >= 0 && i < stageIdx);
          const active = i === stageIdx && stage !== "complete";
          const cfg2 = STAGE_LABEL[s];
          return (
            <div
              key={s}
              className="flex flex-col items-center gap-1"
              title={cfg2?.label}
            >
              <div
                className={cn(
                  "h-1 w-full rounded",
                  done && "bg-foreground",
                  active && "bg-foreground/60 animate-pulse",
                  !done && !active && "bg-muted",
                )}
              />
              <span className="hidden sm:block truncate text-[10px] text-muted-foreground">
                {cfg2?.label.split(" ")[0] ?? s}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
