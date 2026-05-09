"use client";

import { useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  Linkedin,
  PauseCircle,
  PlayCircle,
  RefreshCw,
  Save,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { TagInput } from "@/components/ui/tag-input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Plug, Settings2 } from "lucide-react";
import { ApiError, api } from "@/lib/api";
import { onboardingApi, type Cofounder } from "@/lib/onboarding";
import { repliesApi } from "@/lib/replies";
import { settingsApi } from "@/lib/settings";

export default function SettingsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const cofoundersQ = useQuery({
    queryKey: ["cofounders"],
    queryFn: onboardingApi.listCofounders,
  });

  // Default to integrations; if Unipile is bouncing back (?connected=…) stay on integrations.
  const initialTab =
    params.get("tab") === "engine" ? "engine" : "integrations";
  const [tab, setTab] = useState<string>(initialTab);

  const connectedCount = (cofoundersQ.data ?? []).filter(
    (c) => !!c.unipile_account_id,
  ).length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Vendor connections and engine tuning.
        </p>
      </div>

      <Tabs
        value={tab}
        onValueChange={(v) => {
          setTab(v);
          router.replace(`/settings?tab=${v}`);
        }}
      >
        <TabsList>
          <TabsTrigger value="integrations">
            <Plug className="h-3.5 w-3.5" />
            Integrations
            {cofoundersQ.data && (
              <Badge
                variant={connectedCount > 0 ? "success" : "muted"}
                className="ml-1 rounded-full px-1.5 py-0 text-[10px]"
              >
                {connectedCount}/{cofoundersQ.data.length}
              </Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="engine">
            <Settings2 className="h-3.5 w-3.5" />
            Engine
          </TabsTrigger>
        </TabsList>

        <TabsContent value="integrations">
          <Section
            title="LinkedIn connections"
            description="Each cofounder needs an active Unipile connection so the engine can search posts, poll replies, and send connection requests on their behalf."
          >
            {cofoundersQ.isLoading ? (
              <Skeleton className="h-44 w-full" />
            ) : cofoundersQ.data?.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No cofounders yet. Finish onboarding first.
              </p>
            ) : (
              (cofoundersQ.data ?? []).map((cf) => (
                <CofounderUnipileCard key={cf._id} cofounder={cf} />
              ))
            )}
          </Section>
        </TabsContent>

        <TabsContent value="engine">
          <Section
            title="Discovery & ICP"
            description="Keyword pools drive every daily run's post search. Tier-1 is high-precision; tier-2/3 are broader fallbacks. The ICP threshold sets how strict the gate is — anything below this score is dropped."
          >
            <EngineConfigCard />
          </Section>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-4">
      <div className="border-b pb-3">
        <h2 className="text-base font-semibold">{title}</h2>
        {description && (
          <p className="mt-1 text-sm text-muted-foreground">{description}</p>
        )}
      </div>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function CofounderUnipileCard({ cofounder }: { cofounder: Cofounder }) {
  const params = useSearchParams();
  const router = useRouter();
  const qc = useQueryClient();

  const [message, setMessage] = useState(
    cofounder.connect_message_template ??
      "Enjoyed your perspective on the thread. Would love to stay connected if you're open to it.",
  );

  const connect = useMutation({
    mutationFn: () => repliesApi.unipileConnect(cofounder._id),
    onSuccess: ({ url }) => {
      window.location.href = url;
    },
    onError: (err: ApiError) => toast.error(err.detail),
  });

  const sync = useMutation({
    mutationFn: () => repliesApi.unipileSync(cofounder._id),
    onSuccess: (res) => {
      if (res.attached) {
        toast.success(`Connected as ${res.account_id}`);
        qc.invalidateQueries({ queryKey: ["cofounders"] });
      } else {
        toast.info(
          "No account found yet. Finish the LinkedIn login on the Unipile page, then click Sync.",
        );
      }
    },
    onError: (err: ApiError) => toast.error(err.detail),
  });

  const saveMessage = useMutation({
    mutationFn: () =>
      api.put(`/api/onboarding/cofounders/${cofounder._id}`, {
        connect_message_template: message || null,
      }),
    onSuccess: () => {
      toast.success("Auto-CR template saved");
      qc.invalidateQueries({ queryKey: ["cofounders"] });
    },
    onError: (err: ApiError) => toast.error(err.detail),
  });

  const disconnect = useMutation({
    mutationFn: () =>
      api.put(`/api/onboarding/cofounders/${cofounder._id}`, {
        unipile_account_id: null,
      }),
    onSuccess: () => {
      toast.message("Disconnected from Unipile");
      qc.invalidateQueries({ queryKey: ["cofounders"] });
    },
    onError: (err: ApiError) => toast.error(err.detail),
  });

  // If we just got bounced back from Unipile (?connected=<id>), run sync.
  useEffect(() => {
    const connected = params.get("connected");
    const failed = params.get("failed");
    if (connected === cofounder._id) {
      sync.mutate(undefined, {
        onSettled: () => router.replace("/settings"),
      });
    } else if (failed === cofounder._id) {
      toast.error("LinkedIn connection failed or was cancelled.");
      router.replace("/settings");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, cofounder._id]);

  const isConnected = !!cofounder.unipile_account_id;

  return (
    <div className="rounded-xl border bg-background p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold">{cofounder.display_name}</h3>
            {isConnected ? (
              <Badge variant="success" className="rounded-full">
                <Check className="h-3 w-3" />
                connected
              </Badge>
            ) : (
              <Badge variant="muted" className="rounded-full">
                not connected
              </Badge>
            )}
          </div>
          <p className="text-xs text-muted-foreground">{cofounder.email}</p>
        </div>
        {isConnected ? (
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => sync.mutate()}
              disabled={sync.isPending}
            >
              <RefreshCw
                className={`mr-1 h-3.5 w-3.5 ${
                  sync.isPending ? "animate-spin" : ""
                }`}
              />
              Re-sync
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-muted-foreground hover:text-destructive"
              onClick={() => disconnect.mutate()}
              disabled={disconnect.isPending}
            >
              Disconnect
            </Button>
          </div>
        ) : (
          <Button
            size="sm"
            onClick={() => connect.mutate()}
            disabled={connect.isPending}
          >
            <Linkedin className="mr-2 h-3.5 w-3.5" />
            {connect.isPending ? "Opening Unipile…" : "Connect LinkedIn"}
          </Button>
        )}
      </div>

      {isConnected && (
        <div className="mt-3 rounded-md border bg-muted/30 px-3 py-2 font-mono text-[11px] text-muted-foreground">
          {cofounder.unipile_account_id}
        </div>
      )}

      <div className="mt-5 space-y-2">
        <Label className="text-xs uppercase tracking-wide text-muted-foreground">
          Auto-CR message template
        </Label>
        <Textarea
          rows={2}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          maxLength={300}
          className="text-sm"
        />
        <div className="flex items-center justify-between">
          <p className="text-xs text-muted-foreground">
            Sent when a lead replies twice. Max 300 chars.
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={() => saveMessage.mutate()}
            disabled={saveMessage.isPending}
          >
            {saveMessage.isPending ? "Saving…" : "Save template"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function EngineConfigCard() {
  const qc = useQueryClient();
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
  });

  const [tier1, setTier1] = useState<string[]>([]);
  const [tier2, setTier2] = useState<string[]>([]);
  const [tier3, setTier3] = useState<string[]>([]);
  const [threshold, setThreshold] = useState("6");
  const [dailyTarget, setDailyTarget] = useState("30");
  const [hardFloor, setHardFloor] = useState("20");
  const [runTime, setRunTime] = useState("09:00");
  const [paused, setPaused] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Hydrate form from server.
  useEffect(() => {
    const data = settingsQ.data;
    if (!data) return;
    setTier1(data.keywords.tier_1 ?? []);
    setTier2(data.keywords.tier_2 ?? []);
    setTier3(data.keywords.tier_3 ?? []);
    if (data.icp_rubric?.threshold !== undefined) {
      setThreshold(String(data.icp_rubric.threshold));
    }
    setDailyTarget(String(data.daily_target));
    setHardFloor(String(data.hard_floor));
    setRunTime(data.run_time_local);
    setPaused(data.paused);
    setDirty(false);
  }, [settingsQ.data]);

  const markDirty = () => setDirty(true);

  const save = useMutation({
    mutationFn: () => {
      const body: Parameters<typeof settingsApi.patch>[0] = {
        keywords: { tier_1: tier1, tier_2: tier2, tier_3: tier3 },
        daily_target: Number(dailyTarget) || 30,
        hard_floor: Number(hardFloor) || 20,
        run_time_local: runTime,
        paused,
      };
      const t = Number(threshold);
      if (!isNaN(t) && settingsQ.data?.icp_rubric) {
        body.icp_rubric = { ...settingsQ.data.icp_rubric, threshold: t };
      }
      return settingsApi.patch(body);
    },
    onSuccess: (data) => {
      qc.setQueryData(["settings"], data);
      setDirty(false);
      toast.success("Settings saved");
    },
    onError: (err: ApiError) => toast.error(err.detail),
  });

  const togglePaused = useMutation({
    mutationFn: (next: boolean) => settingsApi.patch({ paused: next }),
    onSuccess: (data) => {
      qc.setQueryData(["settings"], data);
      setPaused(data.paused);
      toast.success(data.paused ? "Engine paused" : "Engine resumed");
    },
    onError: (err: ApiError) => toast.error(err.detail),
  });

  if (settingsQ.isLoading || !settingsQ.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Keywords */}
      <div className="rounded-xl border bg-background p-5 space-y-5">
        <div>
          <h3 className="text-sm font-semibold">Keyword pools</h3>
          <p className="text-xs text-muted-foreground">
            Type a keyword and press <kbd className="rounded border bg-muted px-1 text-[10px]">Enter</kbd>,{" "}
            <kbd className="rounded border bg-muted px-1 text-[10px]">,</kbd>, or{" "}
            <kbd className="rounded border bg-muted px-1 text-[10px]">Tab</kbd> to add. Pasting a comma- or newline-separated list adds multiple at once.
          </p>
        </div>

        <KeywordTier
          label="Tier-1"
          tone="High-precision phrases ICP buyers use unprompted."
          values={tier1}
          onChange={(v) => {
            setTier1(v);
            markDirty();
          }}
          accent="default"
        />
        <KeywordTier
          label="Tier-2"
          tone="Adjacent phrases used when tier-1 pool exhausts."
          values={tier2}
          onChange={(v) => {
            setTier2(v);
            markDirty();
          }}
          accent="secondary"
        />
        <KeywordTier
          label="Tier-3"
          tone="Broad fallback. Off by default."
          values={tier3}
          onChange={(v) => {
            setTier3(v);
            markDirty();
          }}
          accent="muted"
        />
      </div>

      {/* Numerics */}
      <div className="rounded-xl border bg-background p-5">
        <div className="mb-4">
          <h3 className="text-sm font-semibold">Run cadence</h3>
          <p className="text-xs text-muted-foreground">
            How aggressive the daily slate is.
          </p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field
            label="ICP threshold"
            hint="drop posts below this score"
          >
            <Input
              type="number"
              min={1}
              max={50}
              value={threshold}
              onChange={(e) => {
                setThreshold(e.target.value);
                markDirty();
              }}
            />
          </Field>
          <Field
            label="Daily target"
            hint="comments per cofounder"
          >
            <Input
              type="number"
              min={1}
              max={200}
              value={dailyTarget}
              onChange={(e) => {
                setDailyTarget(e.target.value);
                markDirty();
              }}
            />
          </Field>
          <Field label="Hard floor" hint="RULE 23 minimum">
            <Input
              type="number"
              min={1}
              max={200}
              value={hardFloor}
              onChange={(e) => {
                setHardFloor(e.target.value);
                markDirty();
              }}
            />
          </Field>
          <Field label="Run time (local)" hint="weekday daily run">
            <Input
              type="time"
              value={runTime}
              onChange={(e) => {
                setRunTime(e.target.value);
                markDirty();
              }}
            />
          </Field>
        </div>
      </div>

      {/* Save bar */}
      <div className="sticky bottom-4 z-10 flex items-center justify-between rounded-xl border bg-background/95 p-3 shadow-sm backdrop-blur">
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant={paused ? "default" : "outline"}
            onClick={() => togglePaused.mutate(!paused)}
            disabled={togglePaused.isPending}
          >
            {paused ? (
              <PlayCircle className="mr-1.5 h-3.5 w-3.5" />
            ) : (
              <PauseCircle className="mr-1.5 h-3.5 w-3.5" />
            )}
            {paused ? "Resume engine" : "Pause engine"}
          </Button>
          {paused && (
            <Badge variant="warning" className="rounded-full">
              <AlertTriangle className="h-3 w-3" />
              daily run skipped
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {dirty && (
            <span className="text-xs text-muted-foreground">unsaved changes</span>
          )}
          <Button
            size="sm"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}
          >
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {save.isPending ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function KeywordTier({
  label,
  tone,
  values,
  onChange,
  accent,
}: {
  label: string;
  tone: string;
  values: string[];
  onChange: (v: string[]) => void;
  accent: "default" | "secondary" | "muted";
}) {
  const accentBg =
    accent === "default"
      ? "bg-foreground/5"
      : accent === "secondary"
        ? "bg-foreground/[0.025]"
        : "bg-transparent";
  return (
    <div className={`rounded-lg p-3 ${accentBg}`}>
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <Label className="text-xs">
          <span className="font-semibold">{label}</span>
          <span className="ml-2 font-normal text-muted-foreground">{tone}</span>
        </Label>
        <span className="text-[11px] tabular-nums text-muted-foreground">
          {values.length} {values.length === 1 ? "keyword" : "keywords"}
        </span>
      </div>
      <TagInput
        value={values}
        onChange={onChange}
        placeholder="add a keyword and press Enter"
      />
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs">{label}</Label>
      {children}
      {hint && <p className="text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  );
}
