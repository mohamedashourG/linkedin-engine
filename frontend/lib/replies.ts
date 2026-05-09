import { api } from "./api";

export type Reply = {
  id: string;
  candidate_id: string;
  cofounder_id: string;
  lead_id: string | null;
  reply_text: string;
  reply_author_name: string | null;
  reply_author_linkedin_url: string | null;
  reply_author_is_post_owner: boolean;
  reply_published_at: string | null;
  detected_at: string;
  suggested_reply: string;
  suggested_reply_type: string;
  user_action: "pending" | "sent" | "dismissed" | "edited";
  candidate_post_text: string | null;
  candidate_post_url: string | null;
  cofounder_name: string | null;
  our_comment: string | null;
};

export type UnipileAccount = {
  id: string;
  name: string;
  profile_url: string | null;
  avatar_url: string | null;
  account_type: string | null;
};

export const repliesApi = {
  list: (days = 14) => api.get<Reply[]>(`/api/replies/?days=${days}`),
  unread: () => api.get<{ unread: number }>("/api/replies/unread-count"),
  action: (
    id: string,
    action: "sent" | "dismissed" | "edited",
    edited_text?: string,
  ) =>
    api.put<{ ok: boolean }>(`/api/replies/${id}/action`, {
      action,
      edited_text,
    }),
  pollNow: () =>
    api.post<{ task_id: string; status: string }>("/api/replies/poll-now"),
  unipileAccounts: () =>
    api.get<UnipileAccount[]>("/api/replies/unipile/accounts"),
  unipileConnect: (cofounderId: string) =>
    api.post<{ url: string; name: string }>(
      `/api/replies/unipile/connect/${cofounderId}`,
    ),
  unipileSync: (cofounderId: string) =>
    api.post<{ attached: boolean; account_id: string | null }>(
      `/api/replies/unipile/sync/${cofounderId}`,
    ),
};
