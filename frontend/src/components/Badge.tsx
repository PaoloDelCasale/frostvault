import { cva, type VariantProps } from "class-variance-authority";
import { AlertTriangle, Ban, CirclePause, Clock, Cloud, CloudOff, HardDrive, HelpCircle, MinusCircle, RotateCw, ShieldCheck, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export type BadgeState =
  | "pending"
  | "protected"
  | "problem"
  | "unconfirmed"
  | "excluded"
  | "stopped"
  | "both"
  | "local_only"
  | "cloud_only"
  | "restoring"
  | "missing"
  | "unsupported";

/** Sole source of state-specific classes used by rendering and contrast checks. */
export const BADGE_STATE_VARIANT_CLASSES = {
  pending: "bg-[var(--state-local-bg)] text-[var(--state-local-fg)]",
  protected: "bg-[var(--state-both-bg)] text-[var(--state-both-fg)]",
  problem: "bg-[var(--state-problem-bg)] text-[var(--state-problem-fg)]",
  unconfirmed: "bg-[var(--state-unsupported-bg)] text-[var(--state-unsupported-fg)]",
  excluded: "bg-[var(--state-unsupported-bg)] text-[var(--state-unsupported-fg)]",
  stopped: "bg-[var(--state-missing-bg)] text-[var(--state-missing-fg)]",
  both: "bg-[var(--state-both-bg)] text-[var(--state-both-fg)]",
  local_only: "bg-[var(--state-local-bg)] text-[var(--state-local-fg)]",
  cloud_only: "bg-[var(--state-cloud-bg)] text-[var(--state-cloud-fg)]",
  restoring: "bg-[var(--state-restoring-bg)] text-[var(--state-restoring-fg)]",
  missing: "bg-[var(--state-missing-bg)] text-[var(--state-missing-fg)]",
  unsupported: "bg-[var(--state-unsupported-bg)] text-[var(--state-unsupported-fg)]",
} as const satisfies Record<BadgeState, string>;

/** Independent labels — colour is never the only carrier of state. */
export const BADGE_STATE_LABELS: Record<BadgeState, string> = {
  pending: "Pending",
  protected: "Protected",
  problem: "Problem",
  unconfirmed: "Unconfirmed",
  excluded: "Excluded",
  stopped: "Stopped by you",
  both: "Available",
  local_only: "Local backup",
  cloud_only: "Cloud only",
  restoring: "Recovery in progress",
  missing: "Unavailable",
  unsupported: "Unsupported local entry",
};

/** Shape carries state alongside colour and text. */
const BADGE_STATE_ICONS: Record<BadgeState, LucideIcon> = {
  pending: Clock,
  protected: ShieldCheck,
  problem: AlertTriangle,
  unconfirmed: HelpCircle,
  excluded: MinusCircle,
  stopped: CirclePause,
  both: ShieldCheck,
  local_only: HardDrive,
  cloud_only: Cloud,
  restoring: RotateCw,
  missing: CloudOff,
  unsupported: Ban,
};

const badgeVariants = cva(
  "inline-flex w-fit max-w-full items-center font-bold whitespace-normal wrap-anywhere",
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
  const Icon = BADGE_STATE_ICONS[state];
  return (
    <span
      className={cn(badgeVariants({ state, size }), className)}
      data-state={state}
    >
      <Icon className={size === "sm" ? "size-3 shrink-0" : "size-3.5 shrink-0"} aria-hidden="true" />
      {label ?? BADGE_STATE_LABELS[state]}
    </span>
  );
}
