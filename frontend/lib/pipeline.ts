import { api } from "./api";

export type LeadCard = {
  id: string;
  linkedin_url: string;
  name: string | null;
  title: string | null;
  company: string | null;
  current_stage: string;
  status: string;
  reply_count: number;
  last_touched_at: string | null;
  cr_sent_at: string | null;
  cr_accepted_at: string | null;
  dm_sent_at: string | null;
  booking_count: number;
};

export type TimelineEntry = {
  kind: "candidate" | "reply" | "booking" | "stage_change";
  at: string;
  title: string;
  body: string | null;
};

export type LeadDetail = {
  lead: LeadCard;
  timeline: TimelineEntry[];
};

export type PipelineResponse = {
  by_stage: Record<string, LeadCard[]>;
};

export const pipelineApi = {
  get: () => api.get<PipelineResponse>("/api/pipeline/"),
  lead: (id: string) => api.get<LeadDetail>(`/api/pipeline/leads/${id}`),
  setStage: (id: string, stage: string) =>
    api.put<{ ok: boolean }>(`/api/pipeline/leads/${id}/stage`, { stage }),
};
