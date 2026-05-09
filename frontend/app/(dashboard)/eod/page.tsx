"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Send } from "lucide-react";

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
import { Badge } from "@/components/ui/badge";
import { ApiError } from "@/lib/api";
import {
  eodApi,
  type EodCofounderInput,
  type EodCofounderPrefill,
} from "@/lib/eod";

type FormState = Record<string, EodCofounderInput>;

export default function EodPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["eod-prefill"],
    queryFn: eodApi.prefill,
  });
  const [form, setForm] = useState<FormState>({});
  const [error, setError] = useState<string | null>(null);
  const [sentMsg, setSentMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!data) return;
    const next: FormState = {};
    for (const cf of data.per_cofounder) {
      next[cf.cofounder_id] = {
        cofounder_id: cf.cofounder_id,
        crs_sent: [],
        dms_sent: [],
        connections_accepted: [],
        replies_received_manual: [],
        bookings_manual: [],
        anomalies: [],
        notes: "",
      };
    }
    setForm(next);
  }, [data]);

  const submit = useMutation({
    mutationFn: () => eodApi.submit(Object.values(form)),
    onSuccess: (resp) => {
      setSentMsg(
        `EOD saved. Nightly batch queued (${resp.nightly_task_id.slice(0, 8)}…).`,
      );
      qc.invalidateQueries({ queryKey: ["eod-prefill"] });
      qc.invalidateQueries({ queryKey: ["pipeline"] });
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  if (isLoading || !data) {
    return <p className="text-muted-foreground">Loading EOD form…</p>;
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">End of day</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per-cofounder log for {data.log_date}. Counts auto-fill from what we
          already track; paste LinkedIn URLs for the human-side actions.
          Submitting kicks off the nightly batch (stage advancement, exhaustion
          ledger, harvester, STALL detection).
        </p>
        {data.last_submitted_at && (
          <p className="mt-1 text-xs text-muted-foreground">
            Last submitted {new Date(data.last_submitted_at).toLocaleString()}
          </p>
        )}
      </div>

      {data.per_cofounder.length === 0 && (
        <p className="text-sm text-muted-foreground">No active cofounders.</p>
      )}

      <div className="space-y-4">
        {data.per_cofounder.map((row) => (
          <CofounderEodCard
            key={row.cofounder_id}
            row={row}
            value={form[row.cofounder_id]}
            onChange={(next) =>
              setForm((s) => ({ ...s, [row.cofounder_id]: next }))
            }
          />
        ))}
      </div>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      {sentMsg && (
        <p className="text-sm text-foreground" role="status">
          {sentMsg}
        </p>
      )}

      <div className="flex justify-end">
        <Button
          disabled={submit.isPending || data.per_cofounder.length === 0}
          onClick={() => {
            setError(null);
            setSentMsg(null);
            submit.mutate();
          }}
        >
          <Send className="mr-2 h-4 w-4" />
          {submit.isPending ? "Submitting…" : "Submit EOD"}
        </Button>
      </div>
    </div>
  );
}

function CofounderEodCard({
  row,
  value,
  onChange,
}: {
  row: EodCofounderPrefill;
  value: EodCofounderInput | undefined;
  onChange: (next: EodCofounderInput) => void;
}) {
  if (!value) return null;
  const set = (patch: Partial<EodCofounderInput>) =>
    onChange({ ...value, ...patch });
  const lines = (xs: string[]) => xs.join("\n");
  const parseLines = (s: string) =>
    s
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-lg">{row.cofounder_name}</CardTitle>
            <CardDescription>
              auto-counts: {row.shipped} shipped · {row.replies_count} replies ·{" "}
              {row.bookings_count} bookings · {row.dropped} dropped ·{" "}
              {row.edited} edited
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <Badge variant="secondary">shipped {row.shipped}</Badge>
            {row.replies_count > 0 && (
              <Badge variant="default">replies {row.replies_count}</Badge>
            )}
            {row.bookings_count > 0 && (
              <Badge variant="default">books {row.bookings_count}</Badge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-2">
        <UrlList
          label="CRs sent today"
          lines={lines(value.crs_sent)}
          onChange={(s) => set({ crs_sent: parseLines(s) })}
          help="One LinkedIn URL per line. Advances each lead to S4."
        />
        <UrlList
          label="Connections accepted"
          lines={lines(value.connections_accepted)}
          onChange={(s) => set({ connections_accepted: parseLines(s) })}
          help="One per line. Advances to S5."
        />
        <UrlList
          label="DMs sent"
          lines={lines(value.dms_sent)}
          onChange={(s) => set({ dms_sent: parseLines(s) })}
          help="One per line. Advances to S6."
        />
        <UrlList
          label="Anomalies / blockers"
          lines={value.anomalies.join("\n")}
          onChange={(s) => set({ anomalies: parseLines(s) })}
          help="Free text, one per line. Surfaces in audit."
        />
        <div className="md:col-span-2 space-y-2">
          <Label>Notes</Label>
          <Textarea
            rows={3}
            value={value.notes}
            onChange={(e) => set({ notes: e.target.value })}
            placeholder="What stood out today? Anything to remember tomorrow?"
          />
        </div>
      </CardContent>
    </Card>
  );
}

function UrlList({
  label,
  lines,
  onChange,
  help,
}: {
  label: string;
  lines: string;
  onChange: (s: string) => void;
  help?: string;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Textarea
        rows={4}
        value={lines}
        onChange={(e) => onChange(e.target.value)}
        placeholder="https://linkedin.com/in/..."
        className="font-mono text-xs"
      />
      {help && <p className="text-xs text-muted-foreground">{help}</p>}
    </div>
  );
}
