import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

export type StorageClass = "standard" | "glacier" | "deep-archive";

export const STORAGE_BADGE_LABELS: Record<StorageClass, string> = {
  standard: "Standard",
  glacier: "Glacier",
  "deep-archive": "Deep Archive",
};

const storageBadgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-bold whitespace-nowrap",
  {
    variants: {
      storage: {
        standard: "border-line bg-[#f1f4f2] text-[#526159]",
        glacier: "border-[#cddff5] bg-blue-soft text-[#24568f]",
        "deep-archive": "border-[#d7c9f0] bg-violet-soft text-[#593a8a]",
      },
    },
    defaultVariants: {
      storage: "standard",
    },
  },
);

type StorageBadgeProps = {
  storage: StorageClass;
  label?: string;
  className?: string;
} & VariantProps<typeof storageBadgeVariants>;

export function StorageBadge({ storage, label, className }: StorageBadgeProps) {
  return (
    <span className={cn(storageBadgeVariants({ storage }), className)} data-storage={storage}>
      {label ?? STORAGE_BADGE_LABELS[storage]}
    </span>
  );
}
