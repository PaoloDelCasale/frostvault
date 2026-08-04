import { Dialog as RadixDialog } from "radix-ui";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type DialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  /** Accessible label for the dismiss control. */
  closeLabel?: string;
  children: ReactNode;
  className?: string;
};

/**
 * Accessible dialog (Radix): focus trap, Esc to close, focus returned to trigger.
 */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  closeLabel = "Close",
  children,
  className,
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-[var(--overlay)]" />
        <RadixDialog.Content
          className={cn(
            "fixed top-1/2 left-1/2 z-50 w-[min(760px,calc(100%-30px))] -translate-x-1/2 -translate-y-1/2",
            "rounded-panel border border-line bg-surface p-[22px] text-ink shadow-lg outline-none",
            className,
          )}
        >
          <div className="mb-4 flex items-start justify-between gap-5">
            <div>
              <RadixDialog.Title className="text-lg font-bold">{title}</RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="mt-1 text-sm text-muted">
                  {description}
                </RadixDialog.Description>
              ) : (
                <RadixDialog.Description className="sr-only">{title}</RadixDialog.Description>
              )}
            </div>
            <RadixDialog.Close asChild>
              <Button
                type="button"
                variant="secondary"
                className="min-h-11 min-w-11"
                aria-label={closeLabel}
              >
                ×
              </Button>
            </RadixDialog.Close>
          </div>
          {children}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
