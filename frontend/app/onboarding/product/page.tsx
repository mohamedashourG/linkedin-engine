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
import { Textarea } from "@/components/ui/textarea";
import { WizardProgress } from "@/components/onboarding/progress";
import { ApiError } from "@/lib/api";
import { onboardingApi, type ProductExtracted, type IcpRubric } from "@/lib/onboarding";

const empty: ProductExtracted = {
  target_industries: [],
  target_titles: [],
  target_geographies: [],
  target_pain_points: [],
  suggested_keywords: { tier_1: [], tier_2: [], tier_3: [] },
};

const csv = (xs: string[]) => xs.join(", ");
const parseCsv = (s: string) =>
  s.split(",").map((x) => x.trim()).filter(Boolean);

export default function ProductPage() {
  const router = useRouter();
  const [freeText, setFreeText] = useState("");
  const [extracted, setExtracted] = useState<ProductExtracted>(empty);
  const [rubric, setRubric] = useState<IcpRubric | null>(null);
  const [error, setError] = useState<string | null>(null);

  const extract = useMutation({
    mutationFn: (text: string) => onboardingApi.extractProduct(text),
    onSuccess: (resp) => {
      setExtracted(resp.product_extracted);
      setRubric(resp.icp_rubric);
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  const save = useMutation({
    mutationFn: () =>
      onboardingApi.saveProduct({
        product_description: freeText,
        product_extracted: extracted,
        icp_rubric: rubric ?? {
          title: { tiers: [{ matches: extracted.target_titles, score: 5 }] },
          industry: {
            tiers: [{ matches: extracted.target_industries, score: 3 }],
          },
          geography: {
            tiers: [{ matches: extracted.target_geographies, score: 2 }],
          },
          stage: { tiers: [{ matches: ["seed", "series-a", "series-b"], score: 3 }] },
          threshold: 6,
        },
      }),
    onSuccess: () => router.push("/onboarding/accounts"),
    onError: (err: ApiError) => setError(err.detail),
  });

  const hasExtracted =
    extracted.target_industries.length +
      extracted.target_titles.length +
      extracted.target_geographies.length >
    0;

  return (
    <div className="space-y-8">
      <WizardProgress current="product" />

      <Card>
        <CardHeader>
          <CardTitle>Describe what you sell and who you target</CardTitle>
          <CardDescription>
            Free-form. The engine extracts ICP / keyword tiers; you'll review and
            edit before saving.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Textarea
            placeholder="We sell a developer-tooling SaaS to platform engineers at Series B SaaS. Buyers are typically..."
            rows={6}
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
          />
          <div className="flex justify-end">
            <Button
              type="button"
              variant="secondary"
              disabled={freeText.trim().length < 20 || extract.isPending}
              onClick={() => {
                setError(null);
                extract.mutate(freeText.trim());
              }}
            >
              {extract.isPending ? "Extracting..." : "Extract ICP"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {hasExtracted && (
        <Card>
          <CardHeader>
            <CardTitle>Review and edit</CardTitle>
            <CardDescription>
              Comma-separated. The engine uses these for discovery and scoring.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Field
              label="Target industries"
              value={csv(extracted.target_industries)}
              onChange={(v) =>
                setExtracted({ ...extracted, target_industries: parseCsv(v) })
              }
            />
            <Field
              label="Target titles"
              value={csv(extracted.target_titles)}
              onChange={(v) =>
                setExtracted({ ...extracted, target_titles: parseCsv(v) })
              }
            />
            <Field
              label="Target geographies"
              value={csv(extracted.target_geographies)}
              onChange={(v) =>
                setExtracted({ ...extracted, target_geographies: parseCsv(v) })
              }
            />
            <Field
              label="Target pain points"
              value={csv(extracted.target_pain_points)}
              onChange={(v) =>
                setExtracted({ ...extracted, target_pain_points: parseCsv(v) })
              }
            />
            <Field
              label="Tier-1 keywords (high-precision)"
              value={csv(extracted.suggested_keywords.tier_1)}
              onChange={(v) =>
                setExtracted({
                  ...extracted,
                  suggested_keywords: {
                    ...extracted.suggested_keywords,
                    tier_1: parseCsv(v),
                  },
                })
              }
            />
            <Field
              label="Tier-2 keywords (adjacent)"
              value={csv(extracted.suggested_keywords.tier_2)}
              onChange={(v) =>
                setExtracted({
                  ...extracted,
                  suggested_keywords: {
                    ...extracted.suggested_keywords,
                    tier_2: parseCsv(v),
                  },
                })
              }
            />
            <Field
              label="Tier-3 keywords (broad)"
              value={csv(extracted.suggested_keywords.tier_3)}
              onChange={(v) =>
                setExtracted({
                  ...extracted,
                  suggested_keywords: {
                    ...extracted.suggested_keywords,
                    tier_3: parseCsv(v),
                  },
                })
              }
            />
          </CardContent>
        </Card>
      )}

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="flex justify-end">
        <Button
          type="button"
          disabled={
            freeText.trim().length < 20 || !hasExtracted || save.isPending
          }
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

function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Input value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
