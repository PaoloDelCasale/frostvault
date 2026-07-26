export type StorageClassOption = {
  id: string;
  currency: string;
  storage_rate_eur_per_gib_month: number;
  retrieval: "instant" | "restore" | string;
  min_duration_days: number;
  requires_restore: boolean;
  availability_zones: "multi" | "single" | string;
  restore_hours_bulk?: number;
  restore_hours_standard?: number;
  restore_rate_eur_per_gib_bulk?: number;
  restore_rate_eur_per_gib_standard?: number;
  /** Per-GiB GET retrieval fee for Instant/IA classes (no RestoreObject job). */
  retrieval_rate_eur_per_gib?: number;
};

type Translate = (key: string, params?: Record<string, string | number>) => string;

const RESTORE_REQUIRED_CLASSES = new Set(["GLACIER", "DEEP_ARCHIVE"]);

export function formatRate(value: number): string {
  if (value >= 0.01) return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  return value.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
}

export function storageClassNeedsRestore(option: StorageClassOption): boolean {
  return option.retrieval === "restore" || option.requires_restore;
}

export function formatStorageClassRate(
  option: StorageClassOption,
  t: Translate,
): string {
  return t("ui.storage_class_rate", {
    rate: formatRate(option.storage_rate_eur_per_gib_month),
  });
}

export function formatStorageClassRetrieval(
  option: StorageClassOption,
  t: Translate,
): string {
  if (storageClassNeedsRestore(option)) {
    return t("ui.storage_class_retrieval_restore", {
      hours: option.restore_hours_bulk ?? 0,
    });
  }
  return t("ui.storage_class_retrieval_instant");
}

export function formatStorageClassRecovery(
  option: StorageClassOption,
  t: Translate,
): string {
  if (storageClassNeedsRestore(option)) {
    return t("ui.storage_class_recovery_price", {
      restore_rate: formatRate(option.restore_rate_eur_per_gib_bulk ?? 0),
    });
  }
  if (
    option.retrieval_rate_eur_per_gib != null &&
    option.retrieval_rate_eur_per_gib > 0
  ) {
    return t("ui.storage_class_recovery_price", {
      restore_rate: formatRate(option.retrieval_rate_eur_per_gib),
    });
  }
  return t("ui.storage_class_recovery_none");
}

/** Compact single-line label for native `<option>` fallbacks (e.g. vault panel). */
export function formatStorageClassOptionLabel(
  option: StorageClassOption,
  t: Translate,
): string {
  const rate = formatStorageClassRate(option, t);
  const retrieval = formatStorageClassRetrieval(option, t);
  const recovery = formatStorageClassRecovery(option, t);
  if (storageClassNeedsRestore(option)) {
    return t("ui.storage_class_option_restore", {
      id: option.id,
      rate: formatRate(option.storage_rate_eur_per_gib_month),
      hours: option.restore_hours_bulk ?? 0,
      restore_rate: formatRate(option.restore_rate_eur_per_gib_bulk ?? 0),
      retrieval,
      recovery,
      rate_label: rate,
    });
  }
  return t("ui.storage_class_option_instant", {
    id: option.id,
    rate: formatRate(option.storage_rate_eur_per_gib_month),
    retrieval,
    recovery,
    rate_label: rate,
  });
}

export function sourceNeedsRestoreForClassChange(input: {
  currentClass?: string | null;
  restoreState?: string | null;
}): boolean {
  const current = (input.currentClass || "STANDARD").toUpperCase();
  if (!RESTORE_REQUIRED_CLASSES.has(current)) return false;
  return input.restoreState !== "available";
}
