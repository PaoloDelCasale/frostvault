export const LIFECYCLE_CLASSES = [
  "STANDARD_IA",
  "ONEZONE_IA",
  "GLACIER_IR",
  "GLACIER",
  "DEEP_ARCHIVE",
];

// AWS waterfall, including Standard-IA → One Zone-IA but not the reverse.
const TARGETS: Record<string, string[]> = {
  STANDARD: LIFECYCLE_CLASSES,
  STANDARD_IA: ["ONEZONE_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"],
  ONEZONE_IA: ["GLACIER", "DEEP_ARCHIVE"],
  GLACIER_IR: ["GLACIER", "DEEP_ARCHIVE"],
  GLACIER: ["DEEP_ARCHIVE"],
  DEEP_ARCHIVE: [],
};
const RESIDENCE: Record<string, number> = {
  STANDARD_IA: 30,
  ONEZONE_IA: 30,
  GLACIER_IR: 90,
  GLACIER: 90,
};

export type DraftTransition = { days: string; storage_class: string };

/** Days are object ages. The gap constrains residence in the source class. */
export function validStoragePlan(rules: DraftTransition[]): boolean {
  let previousDays = -1;
  let previousClass = "STANDARD";
  for (const rule of rules) {
    const days = Number(rule.days);
    if (
      !/^\d+$/.test(rule.days) ||
      !Number.isSafeInteger(days) ||
      days < 0 ||
      days <= previousDays
    ) return false;
    if (!TARGETS[previousClass].includes(rule.storage_class)) return false;
    if (["STANDARD_IA", "ONEZONE_IA"].includes(rule.storage_class) && days < 30) return false;
    if (days - previousDays < (RESIDENCE[previousClass] ?? 0)) return false;
    previousDays = days;
    previousClass = rule.storage_class;
  }
  return true;
}
