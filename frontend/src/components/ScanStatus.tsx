import type { StatsResponse } from "@/api/types";
type Params = Record<string, string | number>;
import { formatBytes, formatDateTime, formatNumber } from "@/pages/archive/format";

type Runtime = StatsResponse["runtime"];
type Translate = (key: string, params?: Params) => string;

/**
 * The traversal of the running scan (#696): files seen and their rate, plus a
 * time estimate only when a previous scan left a count of Local Copies.
 */
function traversalProgress(runtime: Runtime, t: Translate): string | null {
  const seen = runtime.traversal_files_seen;
  if (typeof seen !== "number") return null;
  const rate = Number(runtime.traversal_files_per_minute ?? 0);
  const files = formatNumber(seen);
  const perMinute = formatNumber(Math.round(rate));
  const expected = runtime.traversal_files_expected;
  if (typeof expected === "number" && expected > seen && rate > 0) {
    const minutes = Math.max(1, Math.ceil((expected - seen) / rate));
    const remaining = minutes >= 60
      ? t("vault.scan.remaining_hours", { count: Math.round(minutes / 6) / 10 })
      : t("vault.scan.remaining_minutes", { count: minutes });
    return t("vault.scan.traversal_estimate", {
      files, expected: formatNumber(expected), rate: perMinute, remaining,
    });
  }
  return t("vault.scan.traversal", { files, rate: perMinute });
}

/**
 * The running scan and the outcome of the last one (#691). A `source` error
 * recorded before the running scan stays open until a scan succeeds, but it
 * reads as the last scan's outcome, with its time, not as the current state.
 */
export function ScanStatus({ runtime, t }: {
  runtime: Runtime | undefined; t: Translate;
}) {
  if (!runtime) return null;
  const scanning = runtime.scanning === true;
  const error = runtime.last_error_source;
  const failedAt = runtime.last_error_source_at;
  const total = runtime.verify_files_total;
  const verifying = scanning && typeof total === "number" && total > 0;
  const traversal = scanning && runtime.scan_phase === "traversal" ? traversalProgress(runtime, t) : null;
  if (!scanning && !(error && failedAt)) return null;
  return <div className="grid gap-0.5 pb-2 text-sm text-muted" data-testid="scan-status" role="status">
    {error && failedAt && <p className="text-ink" data-testid="scan-last-failed">
      {t("vault.scan.last_failed", { at: formatDateTime(failedAt), error })}</p>}
    {scanning && <p data-testid="scan-in-progress">
      {t(error && failedAt ? "vault.scan.new_in_progress" : "vault.scan.in_progress")}
      {traversal && <>{" · "}<span data-testid="scan-traversal">{traversal}</span></>}
      {verifying && <>{" · "}{t("vault.scan.verifying", {
        files: formatNumber(Number(runtime.verify_files_done ?? 0)), total: formatNumber(total),
        bytes: formatBytes(Number(runtime.verify_bytes_done ?? 0)), total_bytes: formatBytes(Number(runtime.verify_bytes_total ?? 0)),
      })}</>}
    </p>}
  </div>;
}
