import type { StatsResponse } from "@/api/types";
type Params = Record<string, string | number>;
import { formatBytes, formatDateTime, formatNumber } from "@/pages/archive/format";

type Runtime = StatsResponse["runtime"];

/**
 * The running scan and the outcome of the last one (#691). A `source` error
 * recorded before the running scan stays open until a scan succeeds, but it
 * reads as the last scan's outcome, with its time, not as the current state.
 */
export function ScanStatus({ runtime, t }: {
  runtime: Runtime | undefined; t: (key: string, params?: Params) => string;
}) {
  if (!runtime) return null;
  const scanning = runtime.scanning === true;
  const error = runtime.last_error_source;
  const failedAt = runtime.last_error_source_at;
  const total = runtime.verify_files_total;
  const verifying = scanning && typeof total === "number" && total > 0;
  if (!scanning && !(error && failedAt)) return null;
  return <div className="grid gap-0.5 pb-2 text-sm text-muted" data-testid="scan-status" role="status">
    {error && failedAt && <p className="text-ink" data-testid="scan-last-failed">
      {t("vault.scan.last_failed", { at: formatDateTime(failedAt), error })}</p>}
    {scanning && <p data-testid="scan-in-progress">
      {t(error && failedAt ? "vault.scan.new_in_progress" : "vault.scan.in_progress")}
      {verifying && <>{" · "}{t("vault.scan.verifying", {
        files: formatNumber(Number(runtime.verify_files_done ?? 0)), total: formatNumber(total),
        bytes: formatBytes(Number(runtime.verify_bytes_done ?? 0)), total_bytes: formatBytes(Number(runtime.verify_bytes_total ?? 0)),
      })}</>}
    </p>}
  </div>;
}
