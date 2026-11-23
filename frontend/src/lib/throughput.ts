import type { ExpectedFinish, UploadThroughput } from "@/api";
import { formatDayMonth, formatDecimal, formatTimeOfDay } from "@/pages/archive/format";

type Translate = (key: string, params?: Record<string, string | number>) => string;
export type Phase = "upload" | "verification";

/** An expected finish is an approximation: shown to the next five minutes. */
const FINISH_STEP_MS = 5 * 60_000;
const DAY_MS = 24 * 60 * 60_000;

/** Decimal megabytes per second, like a line's speed: «13,2 MB/s» (#755). */
export function formatMegabytesPerSecond(bytesPerSecond: number, t: Translate): string {
  return t("speed.megabytes_per_second", { value: formatDecimal(bytesPerSecond / 1e6) });
}

/** Megabits per second, to compare with the line: «105,6 Mbit/s». */
function formatMegabitsPerSecond(bytesPerSecond: number, t: Translate): string {
  return t("speed.megabits_per_second", { value: formatDecimal((bytesPerSecond * 8) / 1e6) });
}

function formatFilesPerSecond(filesPerSecond: number, t: Translate): string {
  return t("speed.files_per_second", { value: formatDecimal(filesPerSecond) });
}

function finishMoment(finish: ExpectedFinish, now: Date): { at: Date; day: "today" | "tomorrow" | "later" } {
  const at = new Date(Math.ceil(Date.parse(finish.at) / FINISH_STEP_MS) * FINISH_STEP_MS);
  const day = at.toDateString() === now.toDateString() ? "today"
    : at.toDateString() === new Date(now.getTime() + DAY_MS).toDateString() ? "tomorrow" : "later";
  return { at, day };
}

/** «Fine prevista verso le 06:30, limitata dal caricamento» (#755). */
export function expectedFinishText(finish: ExpectedFinish, t: Translate, now = new Date()): string {
  const { at, day } = finishMoment(finish, now);
  return t(`estimate.finish_${day}`, {
    time: formatTimeOfDay(at),
    date: formatDayMonth(at),
    limited: t(`estimate.limited_by_${finish.limited_by}`),
  });
}

/**
 * A Vault with work but no progress for five minutes has no expected finish:
 * «Fermo dalle 17:38», from its last progress (#759).
 */
export function stoppedText(since: string, t: Translate, now = new Date()): string {
  const { today, params } = sinceMoment(since, now);
  return t(today ? "estimate.stopped_today" : "estimate.stopped_earlier", params);
}

/**
 * A past moment for «dalle HH:MM»: today only the time, another day also the
 * date, for the `_earlier` form of a message (#759).
 */
export function sinceMoment(since: string, now = new Date()): { today: boolean; params: { time: string; date: string } } {
  const at = new Date(since);
  return { today: at.toDateString() === now.toDateString(), params: { time: formatTimeOfDay(at), date: formatDayMonth(at) } };
}

/** The last progress for a tight label: «17:38», or «mer 17:38» when not today. */
export function stoppedShort(since: string, now = new Date()): string {
  const at = new Date(since);
  return formatTimeOfDay(at, { weekday: at.toDateString() !== now.toDateString() });
}

/** The finish for a tight label: «06:30», or «mer 06:30» when not today. */
export function expectedFinishShort(finish: ExpectedFinish, now = new Date()): string {
  const { at, day } = finishMoment(finish, now);
  return formatTimeOfDay(at, { weekday: day !== "today" });
}

/**
 * One phase's speeds: now (the last seconds) and the average the estimate
 * uses; the upload also in Mbit/s and files/s. Null before any is measured.
 */
export function phaseSpeeds(phase: Phase, throughput: UploadThroughput | undefined, t: Translate): string | null {
  if (!throughput) return null;
  const measured = throughput[phase];
  const parts: string[] = [];
  const now = measured.bytes_per_second;
  if (now !== null && now !== undefined) {
    parts.push(t("estimate.now", { speed: formatMegabytesPerSecond(now, t) }));
    if (phase === "upload") {
      parts.push(formatMegabitsPerSecond(now, t));
      const files = throughput.upload.files_per_second;
      if (files !== null && files !== undefined) parts.push(formatFilesPerSecond(files, t));
    }
  }
  const average = measured.average_bytes_per_second;
  if (average !== null && average !== undefined) {
    parts.push(t("estimate.average", { minutes: Math.round(throughput.average_seconds / 60), speed: formatMegabytesPerSecond(average, t) }));
  }
  return parts.length ? parts.join(" · ") : null;
}

/** The instant speed alone, for the header indicator. */
export function instantUploadSpeed(throughput: UploadThroughput | undefined, t: Translate): string | null {
  const now = throughput?.upload.bytes_per_second;
  return now === null || now === undefined ? null : formatMegabytesPerSecond(now, t);
}
