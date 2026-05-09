import { redirect } from "next/navigation";
import { cookies } from "next/headers";

import { Sidebar } from "@/components/shared/sidebar";
import { Toaster } from "@/components/shared/toaster";

async function fetchMe(cookieHeader: string) {
  const apiUrl = process.env.API_URL || "http://localhost:8000";
  const res = await fetch(`${apiUrl}/api/auth/me`, {
    headers: { cookie: cookieHeader },
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json() as Promise<{
    name: string;
    onboarding_complete: boolean;
  }>;
}

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const cookieStore = await cookies();
  const token = cookieStore.get("access_token");
  if (!token) redirect("/login");

  const me = await fetchMe(cookieStore.toString());
  if (!me) redirect("/login");
  if (!me.onboarding_complete) redirect("/onboarding/product");

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      <Sidebar userName={me.name} />
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl px-8 py-8">{children}</div>
      </main>
      <Toaster />
    </div>
  );
}
