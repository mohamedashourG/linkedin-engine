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

export type ProductExtracted = {
  target_industries: string[];
  target_titles: string[];
  target_geographies: string[];
  target_pain_points: string[];
};

export type SettingsResponse = {
  keywords: KeywordTiers;
  icp_rubric: IcpRubric | null;
  product_extracted: ProductExtracted;
  company_name: string;
  product_description: string;
  comment_quotas: Record<string, number[]>;
  daily_target: number;
  hard_floor: number;
  run_time_local: string;
  paused: boolean;
  operator_email: string;
  slate_recipients: string[];
};

export type SettingsPatch = Partial<{
  keywords: KeywordTiers;
  icp_rubric: IcpRubric;
  product_extracted: ProductExtracted;
  company_name: string;
  product_description: string;
  comment_quotas: CommentQuotas;
  daily_target: number;
  hard_floor: number;
  run_time_local: string;
  paused: boolean;
  slate_recipients: string[];
}>;

export const settingsApi = {
  get: () => api.get<SettingsResponse>("/api/settings/"),
  patch: (body: SettingsPatch) =>
    api.put<SettingsResponse>("/api/settings/", body),
  regenerateIcp: (free_text?: string) =>
    api.post<SettingsResponse>("/api/settings/icp/regenerate", {
      free_text: free_text ?? null,
    }),
};
