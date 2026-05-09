"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { ApiError } from "@/lib/api";
import { contactsApi } from "@/lib/contacts";

export default function ContactsPage() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<{
    inserted: number;
    skipped_duplicate: number;
    parsed: number;
  } | null>(null);

  const { data: contacts = [] } = useQuery({
    queryKey: ["contacts"],
    queryFn: contactsApi.list,
  });

  const bulkMutation = useMutation({
    mutationFn: (t: string) => contactsApi.bulk(t),
    onSuccess: (result) => {
      setLastResult(result);
      setText("");
      qc.invalidateQueries({ queryKey: ["contacts"] });
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => contactsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["contacts"] }),
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Target contacts</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          People you want the engine to find posts from. Each daily run searches
          for each contact's recent activity in addition to your keyword pool.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Bulk import</CardTitle>
          <CardDescription>
            One contact per line. Each line accepts any of:
            <br />
            <code className="text-xs">Jane Rivera</code>
            <br />
            <code className="text-xs">
              Jane Rivera, VP of Platform Engineering, Series B SaaS
            </code>
            <br />
            <code className="text-xs">
              Jane Rivera, https://linkedin.com/in/jane-rivera-platform
            </code>
            <br />
            <code className="text-xs">
              https://linkedin.com/in/jane-rivera-platform
            </code>
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            rows={6}
            placeholder={
              "Jane Rivera, VP of Platform Engineering, Series B SaaS\nMarcus Chen, https://linkedin.com/in/marcus-chen-eng\nhttps://linkedin.com/in/priya-shah-vp"
            }
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          {lastResult && (
            <p className="text-sm text-muted-foreground">
              Imported {lastResult.inserted} of {lastResult.parsed} parsed
              {lastResult.skipped_duplicate > 0 &&
                ` · ${lastResult.skipped_duplicate} duplicates skipped`}
            </p>
          )}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          <div className="flex justify-end">
            <Button
              disabled={!text.trim() || bulkMutation.isPending}
              onClick={() => {
                setError(null);
                setLastResult(null);
                bulkMutation.mutate(text);
              }}
            >
              <Upload className="mr-2 h-4 w-4" />
              {bulkMutation.isPending ? "Importing…" : "Import"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">
            Your list ({contacts.length})
          </h2>
        </div>

        {contacts.length === 0 ? (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
            No target contacts yet. Paste a list above to start.
          </div>
        ) : (
          <div className="grid gap-2">
            {contacts.map((c) => (
              <div
                key={c._id}
                className="flex items-center justify-between rounded-md border bg-background p-3"
              >
                <div className="space-y-0.5">
                  <div className="font-medium">{c.name}</div>
                  <div className="text-xs text-muted-foreground">
                    {[c.title, c.company].filter(Boolean).join(" · ") || "—"}
                    {c.linkedin_url && (
                      <>
                        {" · "}
                        <a
                          href={c.linkedin_url}
                          target="_blank"
                          rel="noreferrer"
                          className="underline"
                        >
                          {c.linkedin_url}
                        </a>
                      </>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant="muted">{c.status}</Badge>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => removeMutation.mutate(c._id)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
