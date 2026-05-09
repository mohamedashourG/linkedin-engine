"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { WizardProgress } from "@/components/onboarding/progress";
import { ApiError } from "@/lib/api";
import { onboardingApi } from "@/lib/onboarding";

export default function CalendlyPage() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => onboardingApi.connectCalendly(url),
    onSuccess: () => router.push("/onboarding/schedule"),
    onError: (err: ApiError) => setError(err.detail),
  });

  return (
    <div className="space-y-8">
      <WizardProgress current="calendly" />

      <Card>
        <CardHeader>
          <CardTitle>Connect Calendly</CardTitle>
          <CardDescription>
            We attribute bookings back to the comment that earned them. Paste
            your Calendly link below.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Label>Calendly URL</Label>
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://calendly.com/your-handle/intro"
          />
          <p className="text-xs text-muted-foreground">
            Webhook signing key is generated on save. The webhook handler is
            wired in Phase 5.
          </p>
        </CardContent>
      </Card>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="flex justify-between">
        <Button variant="ghost" onClick={() => router.back()}>
          Back
        </Button>
        <Button
          disabled={!url || save.isPending}
          onClick={() => {
            setError(null);
            save.mutate();
          }}
        >
          {save.isPending ? "Saving..." : "Save and continue"}
        </Button>
      </div>
    </div>
  );
}
