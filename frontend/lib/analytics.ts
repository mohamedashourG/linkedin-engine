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

export const analyticsApi = {
  overview: (range: Range = "30d") =>
    api.get<Overview>(`/api/analytics/overview?range=${range}`),
  funnel: (range: Range = "30d") =>
    api.get<FunnelResponse>(`/api/analytics/funnel?range=${range}`),
  byType: (range: Range = "30d") =>
    api.get<ByTypeResponse>(`/api/analytics/by_type?range=${range}`),
  byScore: (range: Range = "30d") =>
    api.get<ByScoreResponse>(`/api/analytics/by_score?range=${range}`),
};
