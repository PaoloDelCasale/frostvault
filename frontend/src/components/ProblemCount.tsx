import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The one style for an open Problems count (#658): problem colours when nonzero, neutral at zero. */
export function ProblemCount({ count, children, className, testId }: { count: number; children?: ReactNode; className?: string; testId?: string }) {
  return <span data-testid={testId} data-tone={count > 0 ? "problem" : "none"}
    className={cn("inline-flex w-fit shrink-0 items-center rounded-badge px-2 text-xs font-bold tabular-nums",
      count > 0 ? "bg-[var(--state-problem-bg)] text-[var(--state-problem-fg)]" : "bg-[var(--state-unsupported-bg)] text-muted", className)}>
    {children ?? count}
  </span>;
}
