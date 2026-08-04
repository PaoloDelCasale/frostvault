import { AlertDialog } from "radix-ui";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type ConfirmDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  tone?: "danger" | "default";
  confirmDisabled?: boolean;
  /** Keep the alert open while the caller settles an asynchronous action. */
  keepOpenOnConfirm?: boolean;
  children?: ReactNode;
};

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancel",
  onConfirm,
  tone = "danger",
  confirmDisabled = false,
  keepOpenOnConfirm = false,
  children,
}: ConfirmDialogProps) {
  return (
    <AlertDialog.Root open={open} onOpenChange={onOpenChange}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-50 bg-[var(--overlay)]" />
        <AlertDialog.Content
          className={cn(
            "fixed top-1/2 left-1/2 z-50 w-[min(28rem,calc(100vw-1.75rem))] -translate-x-1/2 -translate-y-1/2",
            "rounded-[18px] border border-line bg-surface p-5 text-ink shadow-lg outline-none",
            "pb-[max(1.25rem,env(safe-area-inset-bottom))]",
          )}
        >
          <AlertDialog.Title className="text-lg font-bold">{title}</AlertDialog.Title>
          <AlertDialog.Description className="mt-2 whitespace-pre-line text-sm text-muted">
            {description}
          </AlertDialog.Description>
          {children ? <div className="mt-4 grid gap-3">{children}</div> : null}
          <div className="mt-5 flex flex-wrap justify-end gap-2">
            <AlertDialog.Cancel asChild>
              <Button type="button" variant="secondary" className="min-h-11 min-w-11 px-4">
                {cancelLabel}
              </Button>
            </AlertDialog.Cancel>
            <AlertDialog.Action asChild>
              <Button
                type="button"
                variant={tone === "danger" ? "danger" : "primary"}
                className="min-h-11 min-w-11 px-4"
                disabled={confirmDisabled}
                onClick={(event) => {
                  if (keepOpenOnConfirm) event.preventDefault();
                  onConfirm();
                }}
              >
                {confirmLabel}
              </Button>
            </AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
