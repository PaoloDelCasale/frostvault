import type { ArchiveVersionItem, RecoverEstimateResponse } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type RecoverConfirmDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  version: ArchiveVersionItem | null;
  estimate: RecoverEstimateResponse | null;
  estimateError: string | null;
  t: Translate;
  onConfirm: () => void;
};

export function RecoverConfirmDialog({
  open,
  onOpenChange,
  path,
  version,
  estimate,
  estimateError,
  t,
  onConfirm,
}: RecoverConfirmDialogProps) {
  if (!version) return null;

  const lines: string[] = [
    t("ui.recover_version_summary", {
      number: version.version_number,
      storage: version.storage_class || "STANDARD",
    }),
  ];

  if (estimateError) {
    lines.push(estimateError);
  } else if (estimate?.requires_restore && estimate.estimate) {
    lines.push(
      t("ui.recover_estimate_line", {
        tier: estimate.estimate.tier,
        days: estimate.estimate.days,
        cost: Number(estimate.estimate.estimated_cost_eur).toFixed(2),
        hours: estimate.estimate.estimated_hours,
      }),
    );
    lines.push(t("ui.recover_irreversible_note"));
    if (estimate.high_impact) {
      lines.push(t("ui.recover_high_impact_note"));
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("ui.recover_confirm_title", { path })}
      description={lines.join("\n")}
      confirmLabel={t("ui.recover_continue")}
      cancelLabel={t("ui.cancel")}
      tone="default"
      onConfirm={onConfirm}
    />
  );
}
