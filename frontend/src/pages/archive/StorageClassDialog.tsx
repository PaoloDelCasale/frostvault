import { useEffect, useState } from "react";
import { AlertDialog } from "radix-ui";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  COLD_STORAGE_CLASSES,
  STORAGE_CLASS_OPTIONS,
  type ManualStorageClass,
} from "./actions";
import {
  formatStorageClassOptionLabel,
  sourceNeedsRestoreForClassChange,
  type StorageClassOption,
} from "./storageClassOptions";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type StorageClassDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  count: number;
  totalBytes: number;
  currentClass?: string | null;
  restoreState?: string | null;
  classOptions?: StorageClassOption[];
  restoreEstimate?: { hours: number; costEur: number } | null;
  t: Translate;
  onConfirm: (
    target: ManualStorageClass,
    options: { pinAfter: boolean },
  ) => void;
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function fallbackOptions(): StorageClassOption[] {
  return STORAGE_CLASS_OPTIONS.map((id) => ({
    id,
    currency: "EUR",
    storage_rate_eur_per_gib_month: 0,
    retrieval: COLD_STORAGE_CLASSES.has(id) && id !== "GLACIER_IR" ? "restore" : "instant",
    min_duration_days: 0,
    requires_restore: id === "GLACIER" || id === "DEEP_ARCHIVE",
    availability_zones: id === "ONEZONE_IA" ? "single" : "multi",
  }));
}

export function StorageClassDialog({
  open,
  onOpenChange,
  path,
  count,
  totalBytes,
  currentClass,
  restoreState,
  classOptions,
  restoreEstimate,
  t,
  onConfirm,
}: StorageClassDialogProps) {
  const options = classOptions?.length ? classOptions : fallbackOptions();
  const defaultTarget =
    (STORAGE_CLASS_OPTIONS.find((item) => item !== (currentClass || "STANDARD")) as
      | ManualStorageClass
      | undefined) || "STANDARD_IA";
  const [target, setTarget] = useState<ManualStorageClass>(defaultTarget);
  const [pinAfter, setPinAfter] = useState(false);
  const showColdWarning = COLD_STORAGE_CLASSES.has(target);
  const needsRestore = sourceNeedsRestoreForClassChange({
    currentClass,
    restoreState,
  });

  useEffect(() => {
    if (open) {
      setTarget(defaultTarget);
      setPinAfter(false);
    }
  }, [open, defaultTarget]);

  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
      }}
    >
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-50 bg-[rgba(15,30,21,0.42)]" />
        <AlertDialog.Content
          className={cn(
            "fixed top-1/2 left-1/2 z-50 w-[min(32rem,calc(100vw-1.75rem))] -translate-x-1/2 -translate-y-1/2",
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
              {options.map((option) => (
                <option key={option.id} value={option.id}>
                  {formatStorageClassOptionLabel(option, t)}
                </option>
              ))}
            </select>
          </label>
          {needsRestore ? (
            <p
              className="mt-3 text-sm text-amber-800"
              data-testid="storage-class-restore-warning"
            >
              {t("ui.storage_class_restore_warning", {
                hours: restoreEstimate?.hours ?? "—",
                cost:
                  restoreEstimate?.costEur != null
                    ? restoreEstimate.costEur.toFixed(4)
                    : "—",
              })}
            </p>
          ) : null}
          {showColdWarning ? (
            <p
              className="mt-3 text-sm text-amber-800"
              data-testid="storage-class-cold-warning"
            >
              {t("ui.storage_class_confirm_warning")}
            </p>
          ) : null}
          <label className="mt-3 flex items-start gap-2 text-sm text-ink">
            <input
              type="checkbox"
              className="mt-1"
              checked={pinAfter}
              onChange={(event) => setPinAfter(event.target.checked)}
              data-testid="storage-class-pin-after"
            />
            <span>{t("ui.storage_class_pin_after")}</span>
          </label>
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
                onClick={() => onConfirm(target, { pinAfter })}
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
