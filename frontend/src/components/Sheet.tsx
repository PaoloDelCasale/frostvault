import { Dialog as RadixDialog } from "radix-ui";
import { X } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Side panel for details: anchored right on wide screens, a bottom sheet on
 * phones. Radix dialog semantics (focus trap, Esc, focus returned).
 */
export function Sheet({ open, onOpenChange, title, description, closeLabel = "Close", children, className }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: string;
  closeLabel?: string; children: ReactNode; className?: string;
}) {
  return <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
    <RadixDialog.Portal>
      <RadixDialog.Overlay className="fixed inset-0 z-50 bg-[var(--overlay)]" />
      <RadixDialog.Content className={cn(
        "fixed inset-x-0 bottom-0 z-50 flex max-h-[88dvh] flex-col rounded-t-panel border border-line bg-surface text-ink shadow-lg outline-none",
        "md:inset-y-0 md:right-0 md:left-auto md:max-h-none md:w-[min(32rem,100vw)] md:rounded-none md:border-y-0 md:border-r-0",
        className,
      )}>
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <RadixDialog.Title className="break-words text-lg font-bold">{title}</RadixDialog.Title>
            <RadixDialog.Description className={description ? "mt-0.5 break-words text-sm text-muted" : "sr-only"}>{description ?? title}</RadixDialog.Description>
          </div>
          <RadixDialog.Close asChild>
            <Button type="button" variant="ghost" size="icon" aria-label={closeLabel}><X aria-hidden="true" /></Button>
          </RadixDialog.Close>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </RadixDialog.Content>
    </RadixDialog.Portal>
  </RadixDialog.Root>;
}
