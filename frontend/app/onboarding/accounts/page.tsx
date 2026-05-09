"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, Plus } from "lucide-react";

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
import { onboardingApi, type Cofounder } from "@/lib/onboarding";

export default function AccountsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const cofoundersQ = useQuery({
    queryKey: ["cofounders"],
    queryFn: onboardingApi.listCofounders,
  });

  const cofounders = cofoundersQ.data ?? [];

  return (
    <div className="space-y-8">
      <WizardProgress current="accounts" />

      <Card>
        <CardHeader>
          <CardTitle>Add the LinkedIn accounts you'll manage</CardTitle>
          <CardDescription>
            Each "cofounder" is a real person whose LinkedIn account will post
            comments. You'll set their voice in the next step.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {cofounders.map((cf) => (
            <CofounderRow
              key={cf._id}
              cofounder={cf}
              onDelete={async () => {
                await onboardingApi.deleteCofounder(cf._id);
                qc.invalidateQueries({ queryKey: ["cofounders"] });
              }}
            />
          ))}
          <NewCofounderForm
            onCreated={() => qc.invalidateQueries({ queryKey: ["cofounders"] })}
          />
        </CardContent>
      </Card>

      <div className="flex justify-between">
        <Button variant="ghost" onClick={() => router.push("/onboarding/product")}>
          Back
        </Button>
        <Button
          disabled={cofounders.length === 0}
          onClick={() => router.push(`/onboarding/voice/${cofounders[0]._id}`)}
        >
          Continue to voice profiles
        </Button>
      </div>
    </div>
  );
}

function CofounderRow({
  cofounder,
  onDelete,
}: {
  cofounder: Cofounder;
  onDelete: () => void;
}) {
  return (
    <div className="flex items-center justify-between rounded-md border bg-background p-3">
      <div className="space-y-0.5">
        <div className="font-medium">{cofounder.display_name}</div>
        <div className="text-xs text-muted-foreground">
          {cofounder.email} · target {cofounder.daily_volume_target}/day
          {cofounder.voice_profile && " · voice ✓"}
        </div>
      </div>
      <Button variant="ghost" size="icon" onClick={onDelete} aria-label="Delete">
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  );
}

function NewCofounderForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    display_name: "",
    linkedin_url: "",
    calendly_url: "",
    email: "",
    daily_volume_target: "20",
  });
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      onboardingApi.createCofounder({
        display_name: form.display_name,
        linkedin_url: form.linkedin_url,
        calendly_url: form.calendly_url || undefined,
        email: form.email,
        daily_volume_target: Number(form.daily_volume_target) || 20,
      }),
    onSuccess: () => {
      setForm({
        display_name: "",
        linkedin_url: "",
        calendly_url: "",
        email: "",
        daily_volume_target: "20",
      });
      setOpen(false);
      onCreated();
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  if (!open) {
    return (
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Plus className="mr-2 h-4 w-4" />
        Add cofounder
      </Button>
    );
  }

  return (
    <div className="space-y-3 rounded-md border bg-background p-4">
      <div className="grid grid-cols-2 gap-3">
        <FormField
          label="Display name"
          value={form.display_name}
          onChange={(v) => setForm({ ...form, display_name: v })}
        />
        <FormField
          label="Email"
          type="email"
          value={form.email}
          onChange={(v) => setForm({ ...form, email: v })}
        />
        <FormField
          label="LinkedIn URL"
          value={form.linkedin_url}
          onChange={(v) => setForm({ ...form, linkedin_url: v })}
          placeholder="https://www.linkedin.com/in/..."
        />
        <FormField
          label="Calendly URL (optional)"
          value={form.calendly_url}
          onChange={(v) => setForm({ ...form, calendly_url: v })}
          placeholder="https://calendly.com/..."
        />
        <FormField
          label="Daily volume target"
          type="number"
          value={form.daily_volume_target}
          onChange={(v) => setForm({ ...form, daily_volume_target: v })}
        />
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        <Button
          disabled={
            !form.display_name ||
            !form.linkedin_url ||
            !form.email ||
            create.isPending
          }
          onClick={() => {
            setError(null);
            create.mutate();
          }}
        >
          {create.isPending ? "Adding..." : "Add"}
        </Button>
      </div>
    </div>
  );
}

function FormField({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
