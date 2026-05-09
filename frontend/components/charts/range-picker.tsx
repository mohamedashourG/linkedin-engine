"use client";

import { Button } from "@/components/ui/button";
import type { Range } from "@/lib/analytics";

const OPTIONS: Range[] = ["7d", "30d", "90d"];

export function RangePicker({
  value,
  onChange,
}: {
  value: Range;
  onChange: (r: Range) => void;
}) {
  return (
    <div className="inline-flex rounded-md border bg-background p-0.5">
      {OPTIONS.map((opt) => (
        <Button
          key={opt}
          size="sm"
          variant={value === opt ? "default" : "ghost"}
          className="h-7 px-3 text-xs"
          onClick={() => onChange(opt)}
        >
          {opt}
        </Button>
      ))}
    </div>
  );
}
