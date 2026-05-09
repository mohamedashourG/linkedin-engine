import { api } from "./api";

export type Contact = {
  _id: string;
  name: string;
  title: string | null;
  company: string | null;
  linkedin_url: string | null;
  status: string;
  created_at: string;
};

export type BulkResult = {
  inserted: number;
  skipped_duplicate: number;
  parsed: number;
};

export const contactsApi = {
  list: () => api.get<Contact[]>("/api/contacts/"),
  bulk: (text: string) =>
    api.post<BulkResult>("/api/contacts/bulk", { text }),
  remove: (id: string) =>
    api.delete<{ ok: boolean }>(`/api/contacts/${id}`),
};
