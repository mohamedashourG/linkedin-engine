import { api } from "./api";
import type { IcpRubric } from "./onboarding";

export type KeywordTiers = {
  tier_1: string[];
  tier_2: string[];
  tier_3: string[];
};

export type CommentQuotas = Record<
  "A" | "B" | "C" | "D" | "E" | "F",
  number[]
>;

export type SettingsResponse = {
  keywords: KeywordTiers;
  icp_rubric: IcpRubric | null;
  comment_quotas: Record<string, number[]>;
  daily_target: number;
  hard_floor: number;
  run_time_local: string;
  paused: boolean;
};

export type SettingsPatch = Partial<{
  keywords: KeywordTiers;
  icp_rubric: IcpRubric;
  comment_quotas: CommentQuotas;
  daily_target: number;
  hard_floor: number;
  run_time_local: string;
  paused: boolean;
}>;

export const settingsApi = {
  get: () => api.get<SettingsResponse>("/api/settings/"),
  patch: (body: SettingsPatch) =>
    api.put<SettingsResponse>("/api/settings/", body),
};
