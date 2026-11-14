/** Byte, duration, and count formatting for archive statistics. */

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(value);
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export type DurationUnit = "seconds" | "minutes" | "hours";

/** Pick the largest whole-ish unit for a delay shown to humans. */
export function pickDurationUnit(seconds: number): { value: number; unit: DurationUnit } {
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s >= 3600) {
    const hours = s / 3600;
    const value = Number.isInteger(hours) ? hours : Math.round(hours * 10) / 10;
    return { value, unit: "hours" };
  }
  if (s >= 60) {
    const minutes = s / 60;
    const value = Number.isInteger(minutes) ? minutes : Math.round(minutes * 10) / 10;
    return { value, unit: "minutes" };
  }
  return { value: s, unit: "seconds" };
}

export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/** Short counts for tight UI (state breakdown chips). 999 stays 999; 1200 → 1.2K. */
export function formatCompactCount(value: number): string {
  const n = Math.max(0, Math.round(Number(value)));
  if (!Number.isFinite(n)) return "0";
  if (n < 1000) return String(n);
  return new Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

/** The page language set by the i18n provider (`<html lang>`). */
const pageLocale = () => (typeof document !== "undefined" && document.documentElement.lang) || undefined;

export function formatNumber(value: number): string {
  return new Intl.NumberFormat(pageLocale()).format(value);
}

/** Human date and time in the page language; "—" when absent or invalid. */
export function formatDateTime(value: unknown): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(pageLocale(), { dateStyle: "medium", timeStyle: "short" });
}

/** Human calendar date in `locale`; date-only ISO values (`2026-11-01`) stay on their calendar day. */
export function formatDate(value: unknown, locale?: string): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return date.toLocaleDateString(locale ?? pageLocale(), { dateStyle: "medium", ...(dateOnly ? { timeZone: "UTC" } : {}) });
}

/** A euro amount with two decimals in `locale`; positive amounts under one cent read "< 0.01 €". */
export function formatEur(value: number, locale?: string): string {
  const money = new Intl.NumberFormat(locale ?? pageLocale(), { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  return amount > 0 && amount < 0.01 ? `< ${money.format(0.01)}` : money.format(amount);
}

/** How long a cold recovery takes, by S3 storage class. */
export function recoveryTimeKey(storageClass: unknown): string {
  return storageClass === "DEEP_ARCHIVE" ? "essential.ready_deep" : storageClass === "GLACIER" ? "essential.ready_glacier" : "essential.ready_now";
}

/** An instant as a clock time in the page language; the date is added when it is not today. */
export function formatClock(value: unknown, now: Date = new Date()): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toDateString() === now.toDateString()
    ? date.toLocaleTimeString(pageLocale(), { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString(pageLocale(), { dateStyle: "short", timeStyle: "short" });
}
