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

/** Sole source of state-specific classes used by rendering and contrast checks. */
export const BADGE_STATE_VARIANT_CLASSES = {
  both: "bg-[var(--state-both-bg)] text-[var(--state-both-fg)]",
  local_only: "bg-[var(--state-local-bg)] text-[var(--state-local-fg)]",
  cloud_only: "bg-[var(--state-cloud-bg)] text-[var(--state-cloud-fg)]",
  restoring: "bg-[var(--state-restoring-bg)] text-[var(--state-restoring-fg)]",
  mixed: "bg-[var(--state-mixed-bg)] text-[var(--state-mixed-fg)]",
  missing: "bg-[var(--state-missing-bg)] text-[var(--state-missing-fg)]",
  unsupported: "bg-[var(--state-unsupported-bg)] text-[var(--state-unsupported-fg)]",
} as const satisfies Record<BadgeState, string>;

/** Independent labels — colour is never the only carrier of state. */
export const BADGE_STATE_LABELS: Record<BadgeState, string> = {
  both: "Local and cloud",
  local_only: "Local only",
  cloud_only: "Cloud only",
  restoring: "Recovery in progress",
  mixed: "Mixed state",
  missing: "Unavailable",
  unsupported: "Unsupported local entry",
};

const badgeVariants = cva(
  "inline-flex w-fit max-w-full items-center font-bold whitespace-nowrap",
  {
    variants: {
      state: BADGE_STATE_VARIANT_CLASSES,
      size: {
        default: "gap-[7px] rounded-badge px-2.5 py-1.5 text-[13px]",
        sm: "gap-1 rounded-md px-1.5 py-0.5 text-[11px]",
      },
    },
    defaultVariants: {
      state: "both",
      size: "default",
    },
  },
);

type BadgeProps = {
  state: BadgeState;
  label?: string;
  className?: string;
} & VariantProps<typeof badgeVariants>;

export function Badge({ state, label, size = "default", className }: BadgeProps) {
  return (
    <span
      className={cn(badgeVariants({ state, size }), className)}
      data-state={state}
    >
      {label ?? BADGE_STATE_LABELS[state]}
    </span>
  );
}
