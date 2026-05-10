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

export type UploadResult = {
  inserted: number;
  skipped_duplicate: number;
  parsed: number;
  filename: string;
};

export const contactsApi = {
  list: () => api.get<Contact[]>("/api/contacts/"),
  bulk: (text: string) =>
    api.post<BulkResult>("/api/contacts/bulk", { text }),
  remove: (id: string) =>
    api.delete<{ ok: boolean }>(`/api/contacts/${id}`),
  removeMany: (ids: string[]) =>
    api.post<{ deleted: number }>("/api/contacts/bulk-delete", { ids }),
  removeAll: () => api.delete<{ deleted: number }>("/api/contacts/"),
  upload: async (file: File): Promise<UploadResult> => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/contacts/upload", {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      const detail =
        data?.detail || `Upload failed (${res.status})`;
      throw new Error(detail);
    }
    return res.json();
  },
};
