import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type BottomSheetAction = {
  id: string;
  label: string;
  /** Optional scope hint under the label (e.g. cloud vs local). */
  description?: string;
  tone?: "danger" | "default";
};

type BottomSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  actions: BottomSheetAction[];
  onAction: (actionId: string) => void;
};

export function BottomSheet({
  open,
  onOpenChange,
  title,
  actions,
  onAction,
}: BottomSheetProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[var(--overlay)]" />
        <Dialog.Content
          className={cn(
            "fixed inset-x-0 bottom-0 z-50 outline-none",
            "rounded-t-[18px] border border-b-0 border-line bg-surface shadow-lg",
            "px-4 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))]",
          )}
          aria-describedby={undefined}
        >
          <Dialog.Title className="mb-3 break-words text-base font-bold leading-snug text-ink">
            {title}
          </Dialog.Title>
          <div className="flex flex-col gap-2">
            {actions.map((action) => (
              <Button
                key={action.id}
                type="button"
                variant={action.tone === "danger" ? "danger" : "secondary"}
                className={cn(
                  "min-h-11 w-full justify-start px-4",
                  action.description &&
                    "h-auto min-h-11 flex-col items-start gap-1 whitespace-normal py-3",
                )}
                onClick={() => {
                  onAction(action.id);
                  onOpenChange(false);
                }}
              >
                <span className="w-full text-left font-semibold text-wrap">
                  {action.label}
                </span>
                {action.description ? (
                  <span
                    className={cn(
                      "w-full text-left text-sm font-normal leading-snug text-pretty",
                      action.tone === "danger" ? "text-white" : "text-ink",
                    )}
                  >
                    {action.description}
                  </span>
                ) : null}
              </Button>
            ))}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
