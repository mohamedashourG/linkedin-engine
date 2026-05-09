import { redirect } from "next/navigation";
import { cookies } from "next/headers";

import LogoutButton from "@/components/shared/logout-button";

export default async function OnboardingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const cookieStore = await cookies();
  const token = cookieStore.get("access_token");
  if (!token) redirect("/login");

  return (
    <div className="min-h-screen bg-muted/30">
      <header className="border-b bg-background">
        <div className="container flex h-14 items-center justify-between">
          <span className="font-semibold">LinkedIn Engagement Engine</span>
          <LogoutButton />
        </div>
      </header>
      <main className="container max-w-3xl py-10">{children}</main>
    </div>
  );
}
