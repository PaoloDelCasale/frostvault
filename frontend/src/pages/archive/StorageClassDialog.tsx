import { useState } from "react";
import { AlertDialog } from "radix-ui";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  COLD_STORAGE_CLASSES,
  STORAGE_CLASS_OPTIONS,
  type ManualStorageClass,
} from "./actions";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type StorageClassDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  count: number;
  totalBytes: number;
  currentClass?: string | null;
  t: Translate;
  onConfirm: (target: ManualStorageClass) => void;
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

export function StorageClassDialog({
  open,
  onOpenChange,
  path,
  count,
  totalBytes,
  currentClass,
  t,
  onConfirm,
}: StorageClassDialogProps) {
  const defaultTarget =
    STORAGE_CLASS_OPTIONS.find((item) => item !== (currentClass || "STANDARD")) ||
    "STANDARD_IA";
  const [target, setTarget] = useState<ManualStorageClass>(defaultTarget);
  const showColdWarning = COLD_STORAGE_CLASSES.has(target);

  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(next) => {
        if (next) setTarget(defaultTarget);
        onOpenChange(next);
      }}
    >
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-50 bg-[rgba(15,30,21,0.42)]" />
        <AlertDialog.Content
          className={cn(
            "fixed top-1/2 left-1/2 z-50 w-[min(28rem,calc(100vw-1.75rem))] -translate-x-1/2 -translate-y-1/2",
            "rounded-[18px] border border-line bg-surface p-5 text-ink shadow-lg outline-none",
            "pb-[max(1.25rem,env(safe-area-inset-bottom))]",
          )}
          data-testid="storage-class-dialog"
        >
          <AlertDialog.Title className="text-lg font-bold">
            {t("ui.storage_class_confirm_title")}
          </AlertDialog.Title>
          <AlertDialog.Description className="mt-2 whitespace-pre-line text-sm text-muted">
            {t("ui.storage_class_confirm_body", {
              count,
              bytes: formatBytes(totalBytes),
              target,
              path,
            })}
          </AlertDialog.Description>
          <label className="mt-4 block text-sm font-medium text-ink">
            {t("ui.storage_class_picker_label")}
            <select
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm"
              value={target}
              onChange={(event) =>
                setTarget(event.target.value as ManualStorageClass)
              }
              data-testid="storage-class-picker"
            >
              {STORAGE_CLASS_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
          {showColdWarning ? (
            <p
              className="mt-3 text-sm text-amber-800"
              data-testid="storage-class-cold-warning"
            >
              {t("ui.storage_class_confirm_warning")}
            </p>
          ) : null}
          <p className="mt-3 text-sm text-muted">
            {t("ui.storage_class_policy_note")}
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialog.Cancel asChild>
              <Button type="button" variant="outline">
                {t("ui.cancel")}
              </Button>
            </AlertDialog.Cancel>
            <AlertDialog.Action asChild>
              <Button
                type="button"
                onClick={() => onConfirm(target)}
                data-testid="storage-class-confirm"
              >
                {t("ui.row_action_storage_class")}
              </Button>
            </AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
