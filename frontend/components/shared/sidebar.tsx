"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  Calendar,
  CheckSquare,
  Inbox,
  KanbanSquare,
  LogOut,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { authApi } from "@/lib/auth";
import { repliesApi } from "@/lib/replies";
import { cn } from "@/lib/utils";
import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

type NavItem = {
  href: string;
  label: string;
  icon: typeof Sparkles;
  match?: (pathname: string) => boolean;
};

const NAV: NavItem[] = [
  { href: "/", label: "Overview", icon: BarChart3, match: (p) => p === "/" },
  {
    href: "/today",
    label: "Today's slate",
    icon: Sparkles,
  },
  { href: "/replies", label: "Replies", icon: Inbox },
  { href: "/pipeline", label: "Pipeline", icon: KanbanSquare },
  { href: "/eod", label: "End of day", icon: CheckSquare },
  { href: "/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/contacts", label: "Contacts", icon: Users },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar({ userName }: { userName: string }) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const unreadQ = useQuery({
    queryKey: ["replies-unread"],
    queryFn: repliesApi.unread,
    refetchInterval: 60_000,
  });
  const unread = unreadQ.data?.unread ?? 0;

  const logout = useMutation({
    mutationFn: authApi.logout,
    onSuccess: () => {
      router.replace("/login");
      router.refresh();
    },
  });

  return (
    <aside className="flex h-full w-60 flex-col border-r bg-muted/30">
      <div className="flex items-center gap-2 px-5 pt-5 pb-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-foreground text-background">
          <Sparkles className="h-4 w-4" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold">Engagement Engine</span>
          <span className="text-[11px] text-muted-foreground">{userName}</span>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 px-2">
        {NAV.map((item) => {
          const active = item.match
            ? item.match(pathname)
            : pathname.startsWith(item.href);
          const Icon = item.icon;
          const isReplies = item.href === "/replies";
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "group relative flex items-center gap-2.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                active
                  ? "bg-background text-foreground shadow-sm border"
                  : "text-muted-foreground hover:bg-background/50 hover:text-foreground",
              )}
            >
              <Icon
                className={cn(
                  "h-4 w-4 shrink-0",
                  active ? "text-foreground" : "text-muted-foreground",
                )}
              />
              <span className="flex-1 truncate">{item.label}</span>
              {isReplies && unread > 0 && (
                <Badge variant="destructive" className="h-5 px-1.5 text-[10px]">
                  {unread}
                </Badge>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="border-t p-2">
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start"
          onClick={() => logout.mutate()}
          disabled={logout.isPending}
        >
          <LogOut className="mr-2 h-4 w-4" />
          {logout.isPending ? "Signing out…" : "Sign out"}
        </Button>
      </div>
    </aside>
  );
}
