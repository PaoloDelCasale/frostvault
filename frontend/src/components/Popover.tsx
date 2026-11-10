import { Popover as RadixPopover } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Anchored panel for header menus (Vault switcher, upload activity):
 * Esc and outside click close it, focus returns to the trigger.
 */
export function Popover({ open, onOpenChange, trigger, children, label, className, align = "start" }: {
  open: boolean; onOpenChange: (open: boolean) => void; trigger: ReactNode; children: ReactNode;
  label: string; className?: string; align?: "start" | "end";
}) {
  return <RadixPopover.Root open={open} onOpenChange={onOpenChange}>
    <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
    <RadixPopover.Portal>
      <RadixPopover.Content aria-label={label} align={align} sideOffset={6} collisionPadding={8}
        className={cn("z-50 w-[min(22rem,calc(100vw-1rem))] rounded-card border border-line bg-surface p-2 text-ink shadow-lg outline-none", className)}>
        {children}
      </RadixPopover.Content>
    </RadixPopover.Portal>
  </RadixPopover.Root>;
}
