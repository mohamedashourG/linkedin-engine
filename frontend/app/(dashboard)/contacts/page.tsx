"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronRight, FileSpreadsheet, FolderInput, Star, Trash2, Upload, Users, X } from "lucide-react";

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
import { contactsApi, UNGROUPED, UploadResult } from "@/lib/contacts";

const ALL_GROUPS = "__all__";
// Empty-string sentinel = no drill-in. Page lands here on first visit and
// shows ONLY the groups table; contact rows stay hidden until the operator
// picks a group (or "All contacts").
const NO_DRILL_IN = "";

const ACCEPTED_TYPES = ".csv,.xlsx,.xls";

export default function ContactsPage() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<{
    inserted: number;
    skipped_duplicate: number;
    parsed: number;
    group: string | null;
  } | null>(null);

  // File upload state
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Multi-select state for bulk delete
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Groups state. `groupFilter`:
  //   "" (NO_DRILL_IN) → groups table only, contact rows hidden
  //   ALL_GROUPS       → drill-in showing every contact
  //   "<name>"         → drill-in showing only that group's contacts
  //   UNGROUPED        → drill-in showing the no-group bucket
  // `importGroup` is the dropdown/free-text value for the next import.
  const [groupFilter, setGroupFilter] = useState<string>(NO_DRILL_IN);
  const [importGroup, setImportGroup] = useState<string>("");

  // Pull list of groups + active pointer
  const groupsQ = useQuery({
    queryKey: ["contact-groups"],
    queryFn: contactsApi.groups,
  });

  // Fetch contacts only when the operator has drilled into a group. Saves a
  // network roundtrip on the initial groups-only view.
  const { data: contacts = [] } = useQuery({
    queryKey: ["contacts", groupFilter],
    queryFn: () =>
      contactsApi.list(groupFilter === ALL_GROUPS ? undefined : groupFilter),
    enabled: groupFilter !== NO_DRILL_IN,
  });

  const allIds = useMemo(() => contacts.map((c) => c._id), [contacts]);
  const allSelected =
    allIds.length > 0 && allIds.every((id) => selectedIds.has(id));

  const toggleOne = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelectedIds((prev) =>
      prev.size === allIds.length ? new Set() : new Set(allIds),
    );
  };

  const invalidateContactsAndGroups = () => {
    qc.invalidateQueries({ queryKey: ["contacts"] });
    qc.invalidateQueries({ queryKey: ["contact-groups"] });
  };

  const bulkMutation = useMutation({
    mutationFn: (t: string) =>
      contactsApi.bulk(t, importGroup.trim() || undefined),
    onSuccess: (result) => {
      setLastResult(result);
      setText("");
      setImportGroup("");
      invalidateContactsAndGroups();
      // Drill straight into the group the server chose (caller's value if
      // provided, else an auto-generated unique name). Fall back to ALL on
      // the rare zero-insert case.
      setGroupFilter(result.group ?? ALL_GROUPS);
    },
    onError: (err: ApiError) => setError(err.detail),
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) =>
      contactsApi.upload(file, importGroup.trim() || undefined),
    onSuccess: (result) => {
      setUploadResult(result);
      setSelectedFile(null);
      setImportGroup("");
      if (fileRef.current) fileRef.current.value = "";
      invalidateContactsAndGroups();
      setGroupFilter(result.group ?? ALL_GROUPS);
    },
    onError: (err: Error) => setUploadError(err.message),
  });

  const setGroupMutation = useMutation({
    mutationFn: ({ ids, group }: { ids: string[]; group: string | null }) =>
      contactsApi.setGroup(ids, group),
    onSuccess: () => {
      setSelectedIds(new Set());
      invalidateContactsAndGroups();
    },
  });

  const setActiveGroupMutation = useMutation({
    mutationFn: (group: string | null) => contactsApi.setActiveGroup(group),
    onSuccess: () => invalidateContactsAndGroups(),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => contactsApi.remove(id),
    onSuccess: (_data, id) => {
      setSelectedIds((prev) => {
        if (!prev.has(id)) return prev;
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      invalidateContactsAndGroups();
    },
  });

  const removeManyMutation = useMutation({
    mutationFn: (ids: string[]) => contactsApi.removeMany(ids),
    onSuccess: () => {
      setSelectedIds(new Set());
      invalidateContactsAndGroups();
    },
  });

  const removeAllMutation = useMutation({
    mutationFn: () => contactsApi.removeAll(),
    onSuccess: () => {
      setSelectedIds(new Set());
      invalidateContactsAndGroups();
    },
  });

  const handleAssignGroup = () => {
    if (selectedIds.size === 0) return;
    const input = prompt(
      `Assign ${selectedIds.size} contact(s) to which group? (leave blank to clear)`,
      "",
    );
    if (input === null) return; // cancelled
    const trimmed = input.trim();
    setGroupMutation.mutate({
      ids: Array.from(selectedIds),
      group: trimmed === "" ? null : trimmed,
    });
  };

  const handleSetActiveGroup = (group: string | null) => {
    setActiveGroupMutation.mutate(group);
  };

  const handleDeleteSelected = () => {
    if (selectedIds.size === 0) return;
    const n = selectedIds.size;
    if (!confirm(`Delete ${n} selected contact${n === 1 ? "" : "s"}?`)) return;
    removeManyMutation.mutate(Array.from(selectedIds));
  };

  const handleDeleteAll = () => {
    if (contacts.length === 0) return;
    if (
      !confirm(
        `Delete all ${contacts.length} contacts? This cannot be undone.`,
      )
    )
      return;
    removeAllMutation.mutate();
  };

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

  const groupsList = groupsQ.data?.groups ?? [];
  const activeGroup = groupsQ.data?.active_group ?? null;
  const totalContacts = groupsQ.data?.total_contacts ?? 0;

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

      {/* ── Groups card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <Users className="h-5 w-5" />
            Groups
          </CardTitle>
          <CardDescription>
            Bucket contacts (e.g., &quot;Company X List&quot;). Click a row to
            view that group&apos;s contacts. Toggle &quot;Active for runs&quot;
            to scope daily runs to one group only.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Group</th>
                  <th className="px-3 py-2 text-right font-medium">Contacts</th>
                  <th className="px-3 py-2 text-right font-medium">Use for runs</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {/* "All contacts" virtual row — selecting it clears the
                    list filter; cannot be set active (that's what null
                    active_group already means). */}
                <GroupRow
                  label="All contacts"
                  count={totalContacts}
                  selected={groupFilter === ALL_GROUPS}
                  onSelect={() => setGroupFilter(ALL_GROUPS)}
                  activeToggle={null}
                  hint={!activeGroup ? "currently used for runs" : undefined}
                />
                {groupsList.map((g) => {
                  const key = g.name ?? UNGROUPED;
                  const label = g.name ?? "Ungrouped";
                  const selected = groupFilter === key;
                  // For the ungrouped bucket, the active value is the
                  // backend's __ungrouped__ sentinel (so discovery filters
                  // to seeds with no group). For named groups it's the name.
                  const activeValue = g.name ?? UNGROUPED;
                  const isActive = activeGroup === activeValue;
                  return (
                    <GroupRow
                      key={key}
                      label={label}
                      count={g.count}
                      selected={selected}
                      onSelect={() => setGroupFilter(key)}
                      activeToggle={{
                        isActive,
                        onClick: () =>
                          handleSetActiveGroup(isActive ? null : activeValue),
                        pending: setActiveGroupMutation.isPending,
                      }}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="text-xs text-muted-foreground">
            {activeGroup ? (
              <>
                Daily runs will use only{" "}
                <strong className="text-foreground">
                  {activeGroup === UNGROUPED ? "Ungrouped" : activeGroup}
                </strong>
                . Toggle the row off to clear and walk every contact.
              </>
            ) : (
              <>No active group — daily runs walk every contact.</>
            )}
          </div>
        </CardContent>
      </Card>

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
              {uploadResult.inserted === 0 && uploadResult.skipped_duplicate > 0 ? (
                <>
                  All {uploadResult.skipped_duplicate} contacts already exist
                  {uploadResult.group ? (
                    <> in <strong className="text-foreground">{uploadResult.group}</strong></>
                  ) : (
                    " across multiple groups"
                  )}.
                </>
              ) : (
                <>
                  Imported {uploadResult.inserted} of {uploadResult.parsed} contacts
                  from <strong>{uploadResult.filename}</strong>
                  {uploadResult.skipped_duplicate > 0 &&
                    ` · ${uploadResult.skipped_duplicate} duplicates skipped`}
                </>
              )}
            </p>
          )}
          {uploadError && (
            <p className="text-sm text-destructive" role="alert">
              {uploadError}
            </p>
          )}

          {selectedFile && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
              <GroupPicker
                value={importGroup}
                onChange={setImportGroup}
                existingGroups={groupsList
                  .map((g) => g.name)
                  .filter((n): n is string => !!n)}
              />
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
              {lastResult.inserted === 0 && lastResult.skipped_duplicate > 0 ? (
                <>
                  All {lastResult.skipped_duplicate} contacts already exist
                  {lastResult.group ? (
                    <> in <strong className="text-foreground">{lastResult.group}</strong></>
                  ) : (
                    " across multiple groups"
                  )}.
                </>
              ) : (
                <>
                  Imported {lastResult.inserted} of {lastResult.parsed} parsed
                  {lastResult.skipped_duplicate > 0 &&
                    ` · ${lastResult.skipped_duplicate} duplicates skipped`}
                </>
              )}
            </p>
          )}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <GroupPicker
              value={importGroup}
              onChange={setImportGroup}
              existingGroups={groupsList
                .map((g) => g.name)
                .filter((n): n is string => !!n)}
            />
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

      {/* ── Contact list (only when a group is drilled into) ── */}
      {groupFilter !== NO_DRILL_IN && (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1.5 px-2 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => {
                setGroupFilter(NO_DRILL_IN);
                setSelectedIds(new Set());
              }}
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Back to groups
            </Button>
            <span className="text-muted-foreground">/</span>
            <h2 className="text-lg font-semibold">
              {groupFilter === ALL_GROUPS
                ? `All contacts (${contacts.length})`
                : groupFilter === UNGROUPED
                  ? `Ungrouped (${contacts.length})`
                  : `${groupFilter} (${contacts.length})`}
            </h2>
          </div>
          {contacts.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2 text-sm text-muted-foreground">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  className="h-4 w-4 cursor-pointer"
                  aria-label="Select all contacts"
                />
                Select all
              </label>
              <Button
                variant="outline"
                size="sm"
                disabled={selectedIds.size === 0 || setGroupMutation.isPending}
                onClick={handleAssignGroup}
              >
                <FolderInput className="mr-2 h-4 w-4" />
                {setGroupMutation.isPending
                  ? "Assigning…"
                  : `Assign to group${
                      selectedIds.size > 0 ? ` (${selectedIds.size})` : ""
                    }`}
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={
                  selectedIds.size === 0 || removeManyMutation.isPending
                }
                onClick={handleDeleteSelected}
              >
                <Trash2 className="mr-2 h-4 w-4" />
                {removeManyMutation.isPending
                  ? "Deleting…"
                  : `Delete selected${
                      selectedIds.size > 0 ? ` (${selectedIds.size})` : ""
                    }`}
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={removeAllMutation.isPending}
                onClick={handleDeleteAll}
              >
                <Trash2 className="mr-2 h-4 w-4" />
                {removeAllMutation.isPending ? "Deleting…" : "Delete all"}
              </Button>
            </div>
          )}
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
                <div className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={selectedIds.has(c._id)}
                    onChange={() => toggleOne(c._id)}
                    className="h-4 w-4 cursor-pointer"
                    aria-label={`Select ${c.name}`}
                  />
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
                </div>
                <div className="flex items-center gap-2">
                  {c.group && (
                    <Badge variant="outline" className="rounded-full">
                      {c.group}
                    </Badge>
                  )}
                  <Badge variant="muted">{c.status}</Badge>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => {
                      if (!confirm(`Delete ${c.name}?`)) return;
                      removeMutation.mutate(c._id);
                    }}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      )}
    </div>
  );
}

function GroupPicker({
  value,
  onChange,
  existingGroups,
}: {
  value: string;
  onChange: (v: string) => void;
  existingGroups: string[];
}) {
  // Stable id-per-render so multiple GroupPickers on the page don't share
  // the same datalist (which would scope the suggestions to one input only).
  const listId = useMemo(
    () => `existing-groups-${Math.random().toString(36).slice(2, 8)}`,
    [],
  );
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs font-medium text-muted-foreground">
        Add to group{" "}
        <span className="text-muted-foreground/70">
          (pick existing or type a new one)
        </span>
      </label>
      <input
        type="text"
        list={listId}
        placeholder="e.g., Company X List"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring sm:w-72"
      />
      <datalist id={listId}>
        {existingGroups.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
    </div>
  );
}


function GroupRow({
  label,
  count,
  selected,
  onSelect,
  activeToggle,
  hint,
}: {
  label: string;
  count: number;
  selected: boolean;
  onSelect: () => void;
  activeToggle: {
    isActive: boolean;
    onClick: () => void;
    pending: boolean;
  } | null;
  hint?: string;
}) {
  return (
    <tr
      className={`group cursor-pointer transition ${
        selected ? "bg-primary/5" : "hover:bg-muted/40"
      }`}
      onClick={onSelect}
    >
      <td className="px-3 py-2.5">
        <div className="flex items-center gap-2">
          <ChevronRight
            className={`h-3.5 w-3.5 text-muted-foreground transition group-hover:translate-x-0.5 group-hover:text-foreground ${
              selected ? "text-primary" : ""
            }`}
          />
          <span
            className={`font-medium ${
              selected ? "text-primary" : "text-foreground"
            }`}
          >
            {label}
          </span>
          {hint && (
            <span className="text-[11px] text-muted-foreground">{hint}</span>
          )}
        </div>
      </td>
      <td className="px-3 py-2.5 text-right tabular-nums text-muted-foreground">
        {count}
      </td>
      <td className="px-3 py-2.5 text-right">
        {activeToggle ? (
          <button
            type="button"
            disabled={activeToggle.pending}
            onClick={(e) => {
              e.stopPropagation();
              activeToggle.onClick();
            }}
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 ${
              activeToggle.isActive
                ? "border-amber-500 bg-amber-500 text-white hover:bg-amber-600"
                : "border-input bg-background hover:bg-accent"
            }`}
          >
            <Star
              className={`h-3 w-3 ${
                activeToggle.isActive ? "fill-current" : ""
              }`}
            />
            {activeToggle.isActive ? "Active" : "Set active"}
          </button>
        ) : (
          <span className="text-[11px] text-muted-foreground">—</span>
        )}
      </td>
    </tr>
  );
}
