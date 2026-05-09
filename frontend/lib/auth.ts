import { api } from "./api";

export type User = {
  _id: string;
  email: string;
  name: string;
  timezone: string;
  onboarding_complete: boolean;
  paused: boolean;
  created_at: string;
};

export const authApi = {
  signup: (data: {
    email: string;
    password: string;
    name: string;
    timezone: string;
  }) => api.post<User>("/api/auth/signup", data),

  login: (data: { email: string; password: string }) =>
    api.post<User>("/api/auth/login", data),

  logout: () => api.post<void>("/api/auth/logout"),

  me: () => api.get<User>("/api/auth/me"),
};
