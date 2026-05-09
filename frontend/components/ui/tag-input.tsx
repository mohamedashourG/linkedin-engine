"use client";

import * as React from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export interface TagInputProps {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  maxTags?: number;
  className?: string;
  disabled?: boolean;
  /** dedup ignoring case + leading/trailing whitespace */
  dedupe?: boolean;
}

/**
 * Pill-style multi-tag input. Add a tag with Enter, Tab, or comma. Remove with
 * the X button on each pill, or Backspace when the input is empty. Pasting
 * comma- or newline-separated text adds multiple tags at once.
 */
export const TagInput = React.forwardRef<HTMLInputElement, TagInputProps>(
  function TagInput(
    {
      value,
      onChange,
      placeholder,
      maxTags,
      className,
      disabled,
      dedupe = true,
    },
    ref,
  ) {
    const [draft, setDraft] = React.useState("");
    const [focused, setFocused] = React.useState(false);

    const commit = (raw: string) => {
      const parts = raw
        .split(/[,\n]/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (parts.length === 0) return false;
      let next = [...value];
      const seen = new Set(
        dedupe ? next.map((t) => t.toLowerCase()) : next,
      );
      for (const p of parts) {
        const key = dedupe ? p.toLowerCase() : p;
        if (seen.has(key)) continue;
        if (maxTags && next.length >= maxTags) break;
        next.push(p);
        seen.add(key);
      }
      if (next.length !== value.length) {
        onChange(next);
        return true;
      }
      return false;
    };

    const removeAt = (idx: number) => {
      const next = value.filter((_, i) => i !== idx);
      onChange(next);
    };

    const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (disabled) return;
      if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
        if (draft.trim()) {
          e.preventDefault();
          if (commit(draft)) setDraft("");
        }
        return;
      }
      if (e.key === "Backspace" && !draft && value.length > 0) {
        e.preventDefault();
        removeAt(value.length - 1);
      }
    };

    const onPaste = (e: React.ClipboardEvent<HTMLInputElement>) => {
      const text = e.clipboardData.getData("text");
      if (text.includes(",") || text.includes("\n")) {
        e.preventDefault();
        commit(text);
        setDraft("");
      }
    };

    const onBlur = () => {
      setFocused(false);
      if (draft.trim()) {
        commit(draft);
        setDraft("");
      }
    };

    return (
      <div
        className={cn(
          "flex min-h-10 w-full flex-wrap items-center gap-1.5 rounded-md border border-input bg-background px-2 py-1.5 text-sm transition-colors",
          focused && "ring-2 ring-ring ring-offset-2 ring-offset-background",
          disabled && "cursor-not-allowed opacity-50",
          className,
        )}
        onClick={(e) => {
          // Click anywhere in the wrapper focuses the input.
          const target = e.target as HTMLElement;
          if (target.tagName !== "INPUT") {
            (e.currentTarget.querySelector("input") as HTMLInputElement | null)?.focus();
          }
        }}
      >
        {value.map((tag, i) => (
          <span
            key={`${tag}-${i}`}
            className="inline-flex items-center gap-1 rounded-md border bg-muted/50 pl-2 pr-1 py-0.5 text-xs"
          >
            <span className="leading-none">{tag}</span>
            <button
              type="button"
              tabIndex={-1}
              onClick={(e) => {
                e.stopPropagation();
                removeAt(i);
              }}
              className="ml-0.5 inline-flex h-4 w-4 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
              aria-label={`Remove ${tag}`}
              disabled={disabled}
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <input
          ref={ref}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onFocus={() => setFocused(true)}
          onBlur={onBlur}
          placeholder={value.length === 0 ? placeholder : ""}
          disabled={disabled || (maxTags ? value.length >= maxTags : false)}
          className="flex-1 min-w-[120px] bg-transparent px-1 py-0.5 text-sm outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
        />
      </div>
    );
  },
);
