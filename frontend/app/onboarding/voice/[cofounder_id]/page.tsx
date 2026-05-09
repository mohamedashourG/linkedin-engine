"use client";

import { useState, useEffect, use } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { WizardProgress } from "@/components/onboarding/progress";
import { ApiError } from "@/lib/api";
import { onboardingApi } from "@/lib/onboarding";

type Example = { post: string; comment: string };

export default function VoicePage({
  params,
}: {
  params: Promise<{ cofounder_id: string }>;
}) {
  const { cofounder_id } = use(params);
  const router = useRouter();
  const [tone, setTone] = useState("");
  const [examples, setExamples] = useState<Example[]>([
    { post: "", comment: "" },
    { post: "", comment: "" },
    { post: "", comment: "" },
  ]);
  const [error, setError] = useState<string | null>(null);

  const cofoundersQ = useQuery({
    queryKey: ["cofounders"],
    queryFn: onboardingApi.listCofounders,
  });
  const cofounders = cofoundersQ.data ?? [];
  const current = cofounders.find((c) => c._id === cofounder_id);
  const remaining = cofounders.filter((c) => !c.voice_profile && c._id !== cofounder_id);
  const nextCofounder = remaining[0];

  useEffect(() => {
    if (current?.voice_profile) {
      setTone(current.voice_profile.tone_description);
      setExamples(current.voice_profile.examples);
    }
  }, [current?._id, current?.voice_profile]);

  const save = useMutation({
    mutationFn: () =>
      onboardingApi.saveVoice(cofounder_id, {
        tone_description: tone,
        examples: examples.filter((ex) => ex.post && ex.comment),
      }),
    onSuccess: () => {
      if (nextCofounder) {
        router.push(`/onboarding/voice/${nextCofounder._id}`);
      } else {
        router.push("/onboarding/calendly");
      }
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  const validExamples = examples.filter((ex) => ex.post && ex.comment).length;
  const canSubmit =
    tone.trim().length >= 10 && validExamples >= 3 && !save.isPending;

  return (
    <div className="space-y-8">
      <WizardProgress current="voice" />

      <Card>
        <CardHeader>
          <CardTitle>
            Voice profile{current ? ` — ${current.display_name}` : ""}
          </CardTitle>
          <CardDescription>
            One-line tone description plus 3+ real post/comment pairs the engine
            will mimic. The engine builds two prompts (Source A / Source B) for
            different keyword tiers.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label>Tone description</Label>
            <Textarea
              rows={2}
              placeholder="Direct, technical, warm. Drops a specific data point. Never hedges."
              value={tone}
              onChange={(e) => setTone(e.target.value)}
            />
          </div>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label>Examples ({validExamples} / 3 minimum)</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setExamples([...examples, { post: "", comment: "" }])
                }
              >
                <Plus className="mr-1 h-4 w-4" /> Add
              </Button>
            </div>
            {examples.map((ex, i) => (
              <div key={i} className="rounded-md border bg-background p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-muted-foreground">
                    Example {i + 1}
                  </span>
                  {examples.length > 3 && (
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() =>
                        setExamples(examples.filter((_, j) => j !== i))
                      }
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  )}
                </div>
                <Textarea
                  placeholder="Original post text..."
                  rows={3}
                  value={ex.post}
                  onChange={(e) => {
                    const next = [...examples];
                    next[i] = { ...next[i], post: e.target.value };
                    setExamples(next);
                  }}
                />
                <Textarea
                  placeholder="Their comment..."
                  rows={2}
                  value={ex.comment}
                  onChange={(e) => {
                    const next = [...examples];
                    next[i] = { ...next[i], comment: e.target.value };
                    setExamples(next);
                  }}
                />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="flex justify-between">
        <Button variant="ghost" onClick={() => router.push("/onboarding/accounts")}>
          Back
        </Button>
        <Button
          disabled={!canSubmit}
          onClick={() => {
            setError(null);
            save.mutate();
          }}
        >
          {save.isPending
            ? "Building voice templates..."
            : nextCofounder
              ? "Save and next cofounder"
              : "Save and continue"}
        </Button>
      </div>
    </div>
  );
}
