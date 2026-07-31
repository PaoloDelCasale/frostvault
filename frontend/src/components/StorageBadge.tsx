import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

export type StorageClass = "standard" | "glacier" | "deep-archive";

/** Sole source of storage-specific classes used by rendering and contrast checks. */
export const STORAGE_BADGE_VARIANT_CLASSES = {
  standard: "border-line bg-[var(--storage-standard-bg)] text-[var(--storage-standard-fg)]",
  glacier: "border-[var(--storage-glacier-border)] bg-blue-soft text-[var(--storage-glacier-fg)]",
  "deep-archive": "border-[var(--storage-archive-border)] bg-violet-soft text-[var(--storage-archive-fg)]",
} as const satisfies Record<StorageClass, string>;

export const STORAGE_BADGE_LABELS: Record<StorageClass, string> = {
  standard: "Standard",
  glacier: "Glacier",
  "deep-archive": "Deep Archive",
};

const storageBadgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-bold whitespace-nowrap",
  {
    variants: {
      storage: STORAGE_BADGE_VARIANT_CLASSES,
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
