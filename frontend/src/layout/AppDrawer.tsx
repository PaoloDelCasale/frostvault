import { Dialog } from "radix-ui";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { ShellCapabilities, ShellNavHandlers } from "./types";
import { ShellNavItems } from "./ShellNavItems";

type AppDrawerProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
  trigger: ReactNode;
};

export function AppDrawer({
  open,
  onOpenChange,
  capabilities,
  handlers,
  trigger,
}: AppDrawerProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-[var(--overlay)]" />
        <Dialog.Content
          className={cn(
            "fixed inset-y-0 left-0 z-50 flex w-[min(20rem,100vw)] flex-col",
            "border-r border-line bg-surface shadow-lg",
            "pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))]",
            "pl-[max(0.75rem,env(safe-area-inset-left))] pr-3",
            "outline-none",
          )}
          aria-describedby={undefined}
        >
          <div className="mb-3 flex items-center justify-between gap-2">
            <Dialog.Title className="text-base font-bold text-ink">
              Navigation
            </Dialog.Title>
            <Dialog.Close asChild>
              <Button
                type="button"
                variant="secondary"
                className="min-h-11 min-w-11"
                aria-label="Close navigation"
              >
                ×
              </Button>
            </Dialog.Close>
          </div>
          <nav aria-label="Vault navigation" className="flex flex-1 flex-col gap-2 overflow-y-auto">
            <ShellNavItems
              capabilities={capabilities}
              handlers={handlers}
              onNavigate={() => onOpenChange(false)}
            />
          </nav>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
