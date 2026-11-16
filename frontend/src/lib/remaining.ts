type Translate = (key: string, params?: Record<string, string | number>) => string;

/** Minutes left as "N minutes" or, from an hour on, "N.N hours". */
export function formatRemaining(minutes: number, t: Translate): string {
  const whole = Math.max(1, Math.ceil(minutes));
  return whole >= 60
    ? t("vault.scan.remaining_hours", { count: Math.round(whole / 6) / 10 })
    : t("vault.scan.remaining_minutes", { count: whole });
}

/** Minutes to move ``bytes`` at ``bytesPerMinute``, or null without a measured pace. */
export function minutesLeft(bytes: number, bytesPerMinute: number | null | undefined): number | null {
  const rate = Number(bytesPerMinute ?? 0);
  return rate > 0 && bytes > 0 ? bytes / rate : null;
}
