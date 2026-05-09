import { cn } from "@/lib/utils";

export function KpiTile({
  label,
  value,
  hint,
  accent,
  className,
}: {
  label: string;
  value: string | number;
  hint?: string;
  accent?: "default" | "success" | "warning";
  className?: string;
}) {
  const accentClasses =
    accent === "success"
      ? "text-emerald-600"
      : accent === "warning"
        ? "text-amber-600"
        : "text-foreground";
  return (
    <div className={cn("rounded-lg border bg-background p-4", className)}>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-1 text-2xl font-semibold", accentClasses)}>
        {value}
      </div>
      {hint && (
        <div className="mt-1 text-xs text-muted-foreground">{hint}</div>
      )}
    </div>
  );
}
