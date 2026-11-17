type Translate = (key: string, params?: Record<string, string | number>) => string;

/** Minutes left as "N minutes" or, from an hour on, "N.N hours". */
export function formatRemaining(minutes: number, t: Translate): string {
  const whole = Math.max(1, Math.ceil(minutes));
  return whole >= 60
    ? t("vault.scan.remaining_hours", { count: Math.round(whole / 6) / 10 })
    : t("vault.scan.remaining_minutes", { count: whole });
}
