/**
 * The one formatting module for amounts, sizes, counts and dates (#654).
 * Everything follows the active locale (`<html lang>`, set by the i18n
 * provider) unless a `locale` is passed explicitly.
 */

/** The page language set by the i18n provider (`<html lang>`). */
const pageLocale = () => (typeof document !== "undefined" && document.documentElement.lang) || undefined;

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];

/** A size in binary (IEC) units with the locale's decimal separator: `1,5 GiB`. */
export function formatBytes(value: number | null | undefined, locale?: string): string {
  if (value === null || value === undefined) return "—";
  let n = Number(value);
  if (!Number.isFinite(n)) return "—";
  let i = 0;
  while (Math.abs(n) >= 1024 && i < BYTE_UNITS.length - 1) {
    n /= 1024;
    i++;
  }
  const digits = i === 0 ? 0 : 1;
  const number = new Intl.NumberFormat(locale ?? pageLocale(), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
  return `${number} ${BYTE_UNITS[i]}`;
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

export function formatNumber(value: number): string {
  return new Intl.NumberFormat(pageLocale()).format(value);
}

/**
 * A count always grouped, four-digit ones too: «1.900» / «1,900» (#729, #731).
 * Italian leaves four digits ungrouped by default.
 */
export function formatGroupedCount(value: number, locale?: string): string {
  const options = { maximumFractionDigits: 0, useGrouping: true } as const;
  try { return new Intl.NumberFormat(locale ?? pageLocale(), options).format(value); } catch { return new Intl.NumberFormat("en", options).format(value); }
}

/** A calendar month (`2027-05`, UTC) in the page language: «maggio 2027». */
export function formatMonth(value: string, locale?: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(value);
  if (!match) return value;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1));
  return date.toLocaleDateString(locale ?? pageLocale(), { month: "long", year: "numeric", timeZone: "UTC" });
}

/** Human date and time in the page language; "—" when absent or invalid. */
export function formatDateTime(value: unknown, locale?: string): string {
  if (typeof value !== "string" || !value) return "—";
  // A date-only value has no time to show: keep it on its calendar day.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return formatDate(value, locale);
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale ?? pageLocale(), { dateStyle: "medium", timeStyle: "short" });
}

/** Human calendar date in `locale`; date-only ISO values (`2026-11-01`) stay on their calendar day. */
export function formatDate(value: unknown, locale?: string): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return date.toLocaleDateString(locale ?? pageLocale(), { dateStyle: "medium", ...(dateOnly ? { timeZone: "UTC" } : {}) });
}

/**
 * A calendar date read in UTC, like the month-by-month release (`2027-05`):
 * the end of a Minimum Storage Duration falls on the same day everywhere (#731).
 */
export function formatUtcDate(value: unknown, locale?: string): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(locale ?? pageLocale(), { dateStyle: "medium", timeZone: "UTC" });
}

export type MoneyOptions = { currency?: string; locale?: string; signed?: boolean };

/**
 * An amount with two decimals in the locale's currency format: `3,42 €` / `€3.42`.
 * Positive amounts under one cent read "< 0,01 €"; `signed` shows `+`/`-` for deltas.
 * Absent or invalid amounts read "—".
 */
export function formatMoney(value: number | string | null | undefined, { currency = "EUR", locale, signed = false }: MoneyOptions = {}): string {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  let money: Intl.NumberFormat;
  try {
    money = new Intl.NumberFormat(locale ?? pageLocale(), { style: "currency", currency: currency || "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: signed ? "exceptZero" : "auto" });
  } catch {
    money = new Intl.NumberFormat(locale ?? pageLocale(), { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: signed ? "exceptZero" : "auto" });
  }
  return !signed && amount > 0 && amount < 0.01 ? `< ${money.format(0.01)}` : money.format(amount);
}

/**
 * An estimated amount: «~3,42 €». An amount already shown as «< 0,01 €» gets
 * no «~», which would read «~< 0,01 €» (#731).
 */
export function formatApproxMoney(value: number | string | null | undefined, options: MoneyOptions = {}): string {
  const money = formatMoney(value, options);
  return money === "—" || money.startsWith("<") ? money : `~${money}`;
}

/** A euro amount (see {@link formatMoney}). */
export function formatEur(value: number | string | null | undefined, locale?: string): string {
  return formatMoney(value, { locale });
}

/** How long a cold recovery takes, by S3 storage class. */
export function recoveryTimeKey(storageClass: unknown): string {
  return storageClass === "DEEP_ARCHIVE" ? "essential.ready_deep" : storageClass === "GLACIER" ? "essential.ready_glacier" : "essential.ready_now";
}

/** An instant as a clock time in the page language; another day reads as {@link formatDateTime} (#677). */
export function formatClock(value: unknown, now: Date = new Date()): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toDateString() === now.toDateString()
    ? date.toLocaleTimeString(pageLocale(), { timeStyle: "short" })
    : formatDateTime(value);
}
