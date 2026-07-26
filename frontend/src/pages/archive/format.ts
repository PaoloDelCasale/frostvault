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
