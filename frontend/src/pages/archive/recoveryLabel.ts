import { formatClock } from "./format";

type T = (key: string, params?: Record<string, string | number>) => string;

/** «requested at HH:MM · tier · ready by ~HH:MM» from what is known of a Recovery (#646). */
export function recoveryTimeline(recovery: { requested_at?: string | null; tier?: string | null; ready_by?: string | null }, t: T): string[] {
  return [
    recovery.requested_at ? t("recovery.requested_at", { time: formatClock(recovery.requested_at) }) : "",
    recovery.tier ? t(`recovery.tier.${recovery.tier}`) : "",
    recovery.ready_by ? t("recovery.ready_by", { time: formatClock(recovery.ready_by) }) : "",
  ].filter(Boolean);
}
