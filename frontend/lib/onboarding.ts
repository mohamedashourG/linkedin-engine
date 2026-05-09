import { api } from "./api";

export type ProductExtracted = {
  target_industries: string[];
  target_titles: string[];
  target_geographies: string[];
  target_pain_points: string[];
  suggested_keywords: {
    tier_1: string[];
    tier_2: string[];
    tier_3: string[];
  };
};

export type IcpRubricTier = { matches: string[]; score: number };
export type IcpRubric = {
  title: { tiers: IcpRubricTier[] };
  industry: { tiers: IcpRubricTier[] };
  geography: { tiers: IcpRubricTier[] };
  stage: { tiers: IcpRubricTier[] };
  threshold: number;
};

export type ExtractResponse = {
  product_extracted: ProductExtracted;
  icp_rubric: IcpRubric;
};

export type Cofounder = {
  _id: string;
  display_name: string;
  linkedin_url: string;
  calendly_url: string | null;
  email: string;
  daily_volume_target: number;
  voice_profile: {
    tone_description: string;
    examples: { post: string; comment: string }[];
    source_a_template: string;
    source_b_template: string;
  } | null;
  unipile_account_id: string | null;
  connect_message_template: string | null;
  active: boolean;
  created_at: string;
};

export type OnboardingStatus = {
  has_product: boolean;
  cofounder_count: number;
  cofounders_with_voice: number;
  has_calendly: boolean;
  has_schedule: boolean;
  onboarding_complete: boolean;
};

export const onboardingApi = {
  status: () => api.get<OnboardingStatus>("/api/onboarding/status"),

  extractProduct: (free_text: string) =>
    api.post<ExtractResponse>("/api/onboarding/product/extract", { free_text }),

  saveProduct: (data: {
    product_description: string;
    product_extracted: ProductExtracted;
    icp_rubric: IcpRubric;
  }) => api.put<{ ok: boolean }>("/api/onboarding/product", data),

  listCofounders: () =>
    api.get<Cofounder[]>("/api/onboarding/cofounders"),

  createCofounder: (data: {
    display_name: string;
    linkedin_url: string;
    calendly_url?: string;
    email: string;
    daily_volume_target: number;
  }) => api.post<Cofounder>("/api/onboarding/cofounders", data),

  deleteCofounder: (id: string) =>
    api.delete<{ ok: boolean }>(`/api/onboarding/cofounders/${id}`),

  saveVoice: (
    cofounderId: string,
    data: {
      tone_description: string;
      examples: { post: string; comment: string }[];
    },
  ) =>
    api.put<Cofounder>(
      `/api/onboarding/cofounders/${cofounderId}/voice`,
      data,
    ),

  connectCalendly: (calendly_url: string) =>
    api.post<{ ok: boolean }>("/api/onboarding/calendly", { calendly_url }),

  setSchedule: (data: { run_time_local: string; daily_target: number }) =>
    api.put<{ ok: boolean }>("/api/onboarding/schedule", data),
};
