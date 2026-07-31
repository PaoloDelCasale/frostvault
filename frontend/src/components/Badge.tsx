import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

export type BadgeState =
  | "both"
  | "local_only"
  | "cloud_only"
  | "restoring"
  | "mixed"
  | "missing"
  | "unsupported";

/** Independent labels — colour is never the only carrier of state. */
export const BADGE_STATE_LABELS: Record<BadgeState, string> = {
  both: "Server + cloud",
  local_only: "Server only",
  cloud_only: "Cloud only",
  restoring: "Recovery in progress",
  mixed: "Mixed state",
  missing: "Unavailable",
  unsupported: "Unsupported local entry",
};

const badgeVariants = cva(
  "inline-flex items-center gap-[7px] rounded-badge px-2.5 py-1.5 text-[13px] font-bold whitespace-nowrap",
  {
    variants: {
      state: {
        both: "bg-[var(--state-both-bg)] text-[var(--state-both-fg)]",
        local_only: "bg-[var(--state-local-bg)] text-[var(--state-local-fg)]",
        cloud_only: "bg-[var(--state-cloud-bg)] text-[var(--state-cloud-fg)]",
        restoring: "bg-[var(--state-restoring-bg)] text-[var(--state-restoring-fg)]",
        mixed: "bg-[var(--state-mixed-bg)] text-[var(--state-mixed-fg)]",
        missing: "bg-[var(--state-missing-bg)] text-[var(--state-missing-fg)]",
        unsupported: "bg-[var(--state-unsupported-bg)] text-[var(--state-unsupported-fg)]",
      },
    },
    defaultVariants: {
      state: "both",
    },
  },
);

type BadgeProps = {
  state: BadgeState;
  label?: string;
  className?: string;
} & VariantProps<typeof badgeVariants>;

export function Badge({ state, label, className }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ state }), className)} data-state={state}>
      {label ?? BADGE_STATE_LABELS[state]}
    </span>
  );
}
