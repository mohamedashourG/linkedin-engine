"use client";

import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileSpreadsheet, Trash2, Upload, X } from "lucide-react";

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
import { contactsApi, UploadResult } from "@/lib/contacts";

const ACCEPTED_TYPES = ".csv,.xlsx,.xls";

export default function ContactsPage() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<{
    inserted: number;
    skipped_duplicate: number;
    parsed: number;
  } | null>(null);

  // File upload state
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

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

  const uploadMutation = useMutation({
    mutationFn: (file: File) => contactsApi.upload(file),
    onSuccess: (result) => {
      setUploadResult(result);
      setSelectedFile(null);
      if (fileRef.current) fileRef.current.value = "";
      qc.invalidateQueries({ queryKey: ["contacts"] });
    },
    onError: (err: Error) => setUploadError(err.message),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => contactsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["contacts"] }),
  });

  const handleFile = useCallback((file: File | null) => {
    setUploadError(null);
    setUploadResult(null);
    if (!file) return;
    const ext = file.name.split(".").pop()?.toLowerCase();
    if (!ext || !["csv", "xlsx", "xls"].includes(ext)) {
      setUploadError("Unsupported file type. Use .csv or .xlsx");
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      setUploadError("File too large (max 5 MB)");
      return;
    }
    setSelectedFile(file);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragActive(false);
      const file = e.dataTransfer.files?.[0];
      if (file) handleFile(file);
    },
    [handleFile],
  );

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Target contacts</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          People you want the engine to find posts from. Each daily run searches
          for each contact&apos;s recent LinkedIn activity alongside your keyword
          pool.
        </p>
      </div>

      {/* ── File upload card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <FileSpreadsheet className="h-5 w-5" />
            Import from file
          </CardTitle>
          <CardDescription>
            Upload a CSV or Excel file with columns for{" "}
            <strong>Name</strong>, <strong>Title</strong>,{" "}
            <strong>Company</strong>, and <strong>LinkedIn URL</strong>.
            Headers are auto-detected.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div
            className={`relative flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 transition-colors ${
              dragActive
                ? "border-primary bg-primary/5"
                : "border-muted-foreground/25 hover:border-muted-foreground/50"
            }`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={handleDrop}
          >
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPTED_TYPES}
              className="absolute inset-0 cursor-pointer opacity-0"
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
            />
            {selectedFile ? (
              <div className="flex items-center gap-2">
                <FileSpreadsheet className="h-5 w-5 text-primary" />
                <span className="font-medium">{selectedFile.name}</span>
                <span className="text-xs text-muted-foreground">
                  ({(selectedFile.size / 1024).toFixed(0)} KB)
                </span>
                <button
                  type="button"
                  className="ml-2 rounded p-1 hover:bg-muted"
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelectedFile(null);
                    if (fileRef.current) fileRef.current.value = "";
                  }}
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ) : (
              <>
                <Upload className="mb-2 h-8 w-8 text-muted-foreground" />
                <p className="text-sm text-muted-foreground">
                  Drag & drop a .csv or .xlsx file, or click to browse
                </p>
              </>
            )}
          </div>

          {uploadResult && (
            <p className="text-sm text-muted-foreground">
              Imported {uploadResult.inserted} of {uploadResult.parsed} contacts
              from <strong>{uploadResult.filename}</strong>
              {uploadResult.skipped_duplicate > 0 &&
                ` · ${uploadResult.skipped_duplicate} duplicates skipped`}
            </p>
          )}
          {uploadError && (
            <p className="text-sm text-destructive" role="alert">
              {uploadError}
            </p>
          )}

          {selectedFile && (
            <div className="flex justify-end">
              <Button
                disabled={uploadMutation.isPending}
                onClick={() => {
                  setUploadError(null);
                  setUploadResult(null);
                  uploadMutation.mutate(selectedFile);
                }}
              >
                <Upload className="mr-2 h-4 w-4" />
                {uploadMutation.isPending ? "Uploading…" : "Upload & import"}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Bulk text import card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Paste contacts</CardTitle>
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

      {/* ── Contact list ── */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">
            Your list ({contacts.length})
          </h2>
        </div>

        {contacts.length === 0 ? (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
            No target contacts yet. Upload a file or paste a list above to
            start.
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
                    {[c.title, c.company].filter(Boolean).join(" · ") ||
                      "—"}
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
