export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

/** FastAPI/Pydantic 422 uses `detail` as an array of { loc, msg, ... }; other errors use a string. */
function formatErrorBody(data: unknown, status: number): string {
  if (data == null || typeof data !== "object") {
    return `Request failed (${status})`;
  }
  const d = data as Record<string, unknown>;
  if (typeof d.detail === "string") {
    return d.detail;
  }
  if (Array.isArray(d.detail)) {
    return d.detail
      .map((item: unknown) => {
        if (item && typeof item === "object" && "msg" in item) {
          const loc = (item as { loc?: unknown }).loc;
          const locStr = Array.isArray(loc) ? loc.join(".") : String(loc ?? "");
          const msg = String((item as { msg: unknown }).msg);
          return locStr ? `${locStr}: ${msg}` : msg;
        }
        try {
          return JSON.stringify(item);
        } catch {
          return String(item);
        }
      })
      .join("; ");
  }
  if (d.message != null) {
    return String(d.message);
  }
  return `Request failed (${status})`;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
    ...init,
  });

  if (res.status === 204) {
    return undefined as T;
  }

  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(res.status, text || `Request failed (${res.status})`);
  }

  if (!res.ok) {
    throw new ApiError(res.status, formatErrorBody(data, res.status));
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path, { method: "GET" }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PUT",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
