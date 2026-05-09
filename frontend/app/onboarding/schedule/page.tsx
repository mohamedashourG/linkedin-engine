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

export default function SchedulePage() {
  const router = useRouter();
  const [runTime, setRunTime] = useState("09:00");
  const [target, setTarget] = useState("30");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      onboardingApi.setSchedule({
        run_time_local: runTime,
        daily_target: Number(target) || 30,
      }),
    onSuccess: () => {
      router.push("/");
      router.refresh();
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  return (
    <div className="space-y-8">
      <WizardProgress current="schedule" />

      <Card>
        <CardHeader>
          <CardTitle>Pick your daily run time</CardTitle>
          <CardDescription>
            Each weekday at this local time, the engine builds your slate and
            emails it. Weekends are off.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Run time (your local timezone)</Label>
              <Input
                type="time"
                value={runTime}
                onChange={(e) => setRunTime(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Daily comment target</Label>
              <Input
                type="number"
                min={1}
                max={200}
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            The engine will warn you if it can't hit your hard floor (default:
            target × 0.66).
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
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            save.mutate();
          }}
        >
          {save.isPending ? "Saving..." : "Finish onboarding"}
        </Button>
      </div>
    </div>
  );
}
