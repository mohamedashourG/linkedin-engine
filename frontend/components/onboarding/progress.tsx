import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

export type WizardStep = "product" | "accounts" | "voice" | "calendly" | "schedule";

const STEPS: { key: WizardStep; label: string; optional?: boolean }[] = [
  { key: "product", label: "Product" },
  { key: "accounts", label: "Accounts" },
  { key: "voice", label: "Voice", optional: true },
  { key: "calendly", label: "Calendly", optional: true },
  { key: "schedule", label: "Schedule", optional: true },
];

export function WizardProgress({ current }: { current: WizardStep }) {
  const currentIdx = STEPS.findIndex((s) => s.key === current);

  return (
    <ol className="flex items-center gap-2">
      {STEPS.map((step, idx) => {
        const isDone = idx < currentIdx;
        const isCurrent = idx === currentIdx;
        return (
          <li key={step.key} className="flex flex-1 items-center gap-2">
            <div
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-sm font-medium",
                isCurrent && "border-primary bg-primary text-primary-foreground",
                isDone && "border-primary bg-primary text-primary-foreground",
                !isCurrent &&
                  !isDone &&
                  "border-border bg-background text-muted-foreground",
              )}
            >
              {isDone ? <Check className="h-4 w-4" /> : idx + 1}
            </div>
            <span
              className={cn(
                "text-sm",
                isCurrent && "font-medium text-foreground",
                !isCurrent && "text-muted-foreground",
              )}
            >
              {step.label}
              {step.optional && (
                <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                  (optional)
                </span>
              )}
            </span>
            {idx < STEPS.length - 1 && (
              <div
                className={cn(
                  "h-px flex-1 bg-border",
                  isDone && "bg-primary",
                )}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}
