import { api } from "./api";

export type Range = "7d" | "30d" | "90d";

export type Overview = {
  range: Range;
  shipped: number;
  replies: number;
  reply_rate: number;
  crs_sent: number;
  crs_accepted: number;
  bookings: number;
  converted_leads: number;
  active_leads: number;
  stalled_leads: number;
  sealed_runs: number;
  aborted_runs: number;
};

export type FunnelStage = { name: string; count: number };
export type FunnelResponse = { range: Range; stages: FunnelStage[] };

export type TypeBreakdown = {
  type: string;
  shipped: number;
  replies: number;
  reply_rate: number;
};
export type ByTypeResponse = { range: Range; rows: TypeBreakdown[] };

export type ScoreBand = {
  band: string;
  shipped: number;
  replies: number;
  reply_rate: number;
};
export type ByScoreResponse = { range: Range; rows: ScoreBand[] };

export type GateDropPost = {
  candidate_id: string;
  post_url: string | null;
  author_name: string | null;
  author_title: string | null;
  post_text: string;
  drop_reason: string;
  source: string | null;
  matched_keyword: string | null;
  gate_rationale: string | null;
};

export type GateDropGroup = {
  stage: string;
  reason: string;
  label: string;
  description: string;
  count: number;
  posts: GateDropPost[];
};

export type GateStage = {
  key: string;
  label: string;
  count: number;
};

export type GateFunnelResponse = {
  slate_run_id: string | null;
  run_date: string | null;
  status: string | null;
  scope: "run" | "aggregate";
  range: Range | null;
  runs_included: number;
  stages: GateStage[];
  drops: GateDropGroup[];
};

export type GateFunnelScope =
  | { kind: "run"; slateRunId?: string } // omit slateRunId = latest run
  | { kind: "aggregate"; range: Range };

export const analyticsApi = {
  overview: (range: Range = "30d") =>
    api.get<Overview>(`/api/analytics/overview?range=${range}`),
  funnel: (range: Range = "30d") =>
    api.get<FunnelResponse>(`/api/analytics/funnel?range=${range}`),
  byType: (range: Range = "30d") =>
    api.get<ByTypeResponse>(`/api/analytics/by_type?range=${range}`),
  byScore: (range: Range = "30d") =>
    api.get<ByScoreResponse>(`/api/analytics/by_score?range=${range}`),
  gateFunnel: (scope: GateFunnelScope) => {
    const params = new URLSearchParams();
    if (scope.kind === "aggregate") {
      params.set("aggregate", "true");
      params.set("range", scope.range);
    } else if (scope.slateRunId) {
      params.set("slate_run_id", scope.slateRunId);
    }
    const qs = params.toString();
    return api.get<GateFunnelResponse>(
      qs ? `/api/analytics/gate-funnel?${qs}` : `/api/analytics/gate-funnel`,
    );
  },
};
