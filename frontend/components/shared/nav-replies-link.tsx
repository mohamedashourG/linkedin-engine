"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { repliesApi } from "@/lib/replies";

export default function NavRepliesLink() {
  const { data } = useQuery({
    queryKey: ["replies-unread"],
    queryFn: repliesApi.unread,
    refetchInterval: 60_000,
  });
  const unread = data?.unread ?? 0;
  return (
    <Link
      href="/replies"
      className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
    >
      Replies
      {unread > 0 && (
        <Badge variant="destructive" className="px-1.5 py-0 text-[10px]">
          {unread}
        </Badge>
      )}
    </Link>
  );
}
