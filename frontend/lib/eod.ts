import { api } from "./api";

export type EodCofounderPrefill = {
  cofounder_id: string;
  cofounder_name: string;
  shipped: number;
  dropped: number;
  edited: number;
  replies_count: number;
  bookings_count: number;
};

export type EodPrefill = {
  log_date: string;
  per_cofounder: EodCofounderPrefill[];
  last_submitted_at: string | null;
};

export type EodCofounderInput = {
  cofounder_id: string;
  crs_sent: string[];
  dms_sent: string[];
  connections_accepted: string[];
  replies_received_manual: { linkedin_url: string; text: string }[];
  bookings_manual: { linkedin_url: string; meeting_at: string }[];
  anomalies: string[];
  notes: string;
};

export const eodApi = {
  prefill: () => api.get<EodPrefill>("/api/eod/today"),
  submit: (per_cofounder: EodCofounderInput[]) =>
    api.post<{ log_id: string; nightly_task_id: string }>(
      "/api/eod/submit",
      { per_cofounder },
    ),
};
