import { api } from "./api";

export type Contact = {
  _id: string;
  name: string;
  title: string | null;
  company: string | null;
  linkedin_url: string | null;
  group: string | null;
  status: string;
  created_at: string;
};

export type BulkResult = {
  inserted: number;
  skipped_duplicate: number;
  parsed: number;
  /** The group every parsed contact landed in. Auto-generated server-side
   * when the caller didn't pass one. Null only when nothing was inserted. */
  group: string | null;
};

export type UploadResult = {
  inserted: number;
  skipped_duplicate: number;
  parsed: number;
  filename: string;
  /** Group every parsed row landed in. Auto-derived from the filename
   * server-side when the caller didn't pass one. Null when nothing was
   * inserted (e.g., file parsed to zero rows). */
  group: string | null;
};

export type GroupSummary = {
  name: string | null; // null = ungrouped bucket
  count: number;
};

export type GroupsResponse = {
  groups: GroupSummary[];
  total_contacts: number;
  active_group: string | null;
};

/** Sentinel to fetch only ungrouped contacts via the list endpoint. */
export const UNGROUPED = "__ungrouped__";

export const contactsApi = {
  list: (group?: string | null) => {
    const qs = group != null ? `?group=${encodeURIComponent(group)}` : "";
    return api.get<Contact[]>(`/api/contacts/${qs}`);
  },
  bulk: (text: string, group?: string | null) =>
    api.post<BulkResult>("/api/contacts/bulk", {
      text,
      ...(group ? { group } : {}),
    }),
  remove: (id: string) =>
    api.delete<{ ok: boolean }>(`/api/contacts/${id}`),
  removeMany: (ids: string[]) =>
    api.post<{ deleted: number }>("/api/contacts/bulk-delete", { ids }),
  removeAll: () => api.delete<{ deleted: number }>("/api/contacts/"),
  upload: async (file: File, group?: string | null): Promise<UploadResult> => {
    const form = new FormData();
    form.append("file", file);
    const qs = group ? `?group=${encodeURIComponent(group)}` : "";
    const res = await fetch(`/api/contacts/upload${qs}`, {
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
  groups: () => api.get<GroupsResponse>("/api/contacts/groups"),
  setGroup: (ids: string[], group: string | null) =>
    api.post<{ updated: number }>("/api/contacts/bulk-group", { ids, group }),
  setActiveGroup: (group: string | null) =>
    api.put<{ active_group: string | null }>("/api/contacts/active-group", {
      group,
    }),
};
