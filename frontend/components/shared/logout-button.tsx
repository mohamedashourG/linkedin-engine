"use client";

import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import { authApi } from "@/lib/auth";

export default function LogoutButton() {
  const router = useRouter();
  const mutation = useMutation({
    mutationFn: authApi.logout,
    onSuccess: () => {
      router.replace("/login");
      router.refresh();
    },
  });
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => mutation.mutate()}
      disabled={mutation.isPending}
    >
      <LogOut className="mr-2 h-4 w-4" />
      {mutation.isPending ? "Signing out..." : "Sign out"}
    </Button>
  );
}
