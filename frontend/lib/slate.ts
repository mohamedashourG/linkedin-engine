import { api } from "./api";

export type Candidate = {
  id: string;
  cofounder_id: string;
  post_url: string;
  author_name: string | null;
  post_text: string;
  post_published_at: string | null;
  source: string;
  source_classification: string;
  status: string;
  comment_text: string | null;
  comment_type: string | null;
  icp_score: number | null;
  user_action: string;
  drop_reason: string | null;
};

export type SlateRun = {
  id: string;
  run_date: string;
  status:
    | "building"
    | "sealed"
    | "force_aborted";
  total_slated: number;
  per_cofounder_counts: Record<string, Record<string, number>>;
  sealed_at: string | null;
  email_sent: boolean;
  force_abort_reason: string | null;
};

export type SlateCofounder = {
  id: string;
  display_name: string;
  linkedin_url: string;
  daily_volume_target: number;
};

export type SlateTodayResponse = {
  slate_run: SlateRun | null;
  candidates: Candidate[];
  cofounders: SlateCofounder[];
};

export const slateApi = {
  today: () => api.get<SlateTodayResponse>("/api/slate/today"),
  action: (
    candidateId: string,
    action: "copied" | "shipped" | "dropped" | "edited",
    edited_text?: string,
  ) =>
    api.put<{ ok: string }>(`/api/slate/candidates/${candidateId}/action`, {
      action,
      edited_text,
    }),
  runNow: () =>
    api.post<{ task_id: string; status: string }>("/api/slate/run-now"),
};
