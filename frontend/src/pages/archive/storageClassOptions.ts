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
};

type Translate = (key: string, params?: Record<string, string | number>) => string;

const RESTORE_REQUIRED_CLASSES = new Set(["GLACIER", "DEEP_ARCHIVE"]);

function formatRate(value: number): string {
  if (value >= 0.01) return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  return value.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
}

export function formatStorageClassOptionLabel(
  option: StorageClassOption,
  t: Translate,
): string {
  const rate = formatRate(option.storage_rate_eur_per_gib_month);
  if (option.retrieval === "restore" || option.requires_restore) {
    return t("ui.storage_class_option_restore", {
      id: option.id,
      rate,
      hours: option.restore_hours_bulk ?? 0,
      restore_rate: formatRate(option.restore_rate_eur_per_gib_bulk ?? 0),
      min_days: option.min_duration_days,
    });
  }
  return t("ui.storage_class_option_instant", {
    id: option.id,
    rate,
    min_days: option.min_duration_days,
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
