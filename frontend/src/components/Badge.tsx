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
        both: "bg-green-soft text-[#185a37]",
        local_only: "bg-red-soft text-[#92372f]",
        cloud_only: "bg-blue-soft text-[#24568f]",
        restoring: "bg-amber-soft text-[#775400]",
        mixed: "bg-amber-soft text-[#775400]",
        missing: "bg-[#eee] text-[#555]",
        unsupported: "bg-amber-soft text-[#775400]",
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
