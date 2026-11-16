import type { StatsResponse } from "@/api/types";
import { ProgressBar } from "@/components/ProgressBar";
import type { Adoption } from "@/lib/adoption";
import { formatRemaining, minutesLeft } from "@/lib/remaining";
import { cn } from "@/lib/utils";
import { formatBytes, formatDateTime, formatNumber } from "@/pages/archive/format";

type Params = Record<string, string | number>;
type Runtime = StatsResponse["runtime"];
type Translate = (key: string, params?: Params) => string;
type PhaseState = "done" | "running" | "waiting";

/** Phases of a scan, in the order they run (#711). */
const PHASES = ["traversal", "verification", "finalization"] as const;
type Phase = (typeof PHASES)[number];
/** Source error causes the Owner reads translated; any other keeps its text. */
const CAUSES = new Set(["tree_changed", "root_changed", "root_unavailable", "superseded", "incomplete", "watcher_denied"]);

function percent(done: number, total: number): number {
  return total > 0 ? (done / total) * 100 : 0;
}

/**
 * The traversal: files seen and their rate, plus a time estimate and a bar
 * only when a previous scan left a count of Local Copies (#696).
 */
function traversal(runtime: Runtime, t: Translate): { detail: string; value: number | null } | null {
  const seen = runtime.traversal_files_seen;
  if (typeof seen !== "number") return null;
  const rate = Number(runtime.traversal_files_per_minute ?? 0);
  const files = formatNumber(seen);
  const perMinute = formatNumber(Math.round(rate));
  const expected = runtime.traversal_files_expected;
  if (typeof expected === "number" && expected > seen && rate > 0) {
    return {
      detail: t("vault.scan.traversal_detail_estimate", {
        files, expected: formatNumber(expected), rate: perMinute,
        remaining: formatRemaining((expected - seen) / rate, t),
      }),
      value: percent(seen, expected),
    };
  }
  return { detail: t("vault.scan.traversal_detail", { files, rate: perMinute }), value: null };
}

/** Fingerprint verification, by bytes: its pace is known once it ran a minute (#711). */
function verification(runtime: Runtime, t: Translate): { detail: string; value: number } | null {
  const total = Number(runtime.verify_files_total ?? 0);
  if (total <= 0) return null;
  const bytesDone = Number(runtime.verify_bytes_done ?? 0);
  const bytesTotal = Number(runtime.verify_bytes_total ?? 0);
  const params = {
    files: formatNumber(Number(runtime.verify_files_done ?? 0)), total: formatNumber(total),
    bytes: formatBytes(bytesDone), total_bytes: formatBytes(bytesTotal),
  };
  const minutes = minutesLeft(bytesTotal - bytesDone, runtime.verify_bytes_per_minute);
  const detail = minutes === null
    ? t("vault.scan.verification_detail", params)
    : t("vault.scan.verification_detail_rate", {
      ...params, rate: formatBytes(Number(runtime.verify_bytes_per_minute)), remaining: formatRemaining(minutes, t),
    });
  return { detail, value: bytesTotal > 0 ? percent(bytesDone, bytesTotal) : percent(Number(runtime.verify_files_done ?? 0), total) };
}

function phaseState(phase: Phase, current: Phase): PhaseState {
  const [index, running] = [PHASES.indexOf(phase), PHASES.indexOf(current)];
  return index < running ? "done" : index === running ? "running" : "waiting";
}

/**
 * The running scan as its phases, in order, each with its state, progress
 * and time left (#711); then what its success changes. The outcome of an
 * earlier failed scan stays below it, muted and with its cause translated:
 * a `source` error stays open until a scan succeeds, but it is the last
 * scan's outcome, not the current state (#691).
 */
export function ScanStatus({ runtime, t, adoption, unconfirmed = false }: {
  runtime: Runtime | undefined; t: Translate; adoption?: Adoption | null; unconfirmed?: boolean;
}) {
  if (!runtime) return null;
  const scanning = runtime.scanning === true;
  const stored = runtime.last_error_source;
  const failedAt = runtime.last_error_source_at;
  const failed = Boolean(stored && failedAt);
  if (!scanning && !failed) return null;
  const cause = runtime.last_error_source_cause;
  const current: Phase = PHASES.includes(runtime.scan_phase as Phase) ? runtime.scan_phase as Phase : "traversal";
  const progress = { traversal: traversal(runtime, t), verification: verification(runtime, t), finalization: null };
  return <div className="grid gap-2 pb-2 text-sm" data-testid="scan-status" role="status">
    {scanning && <section className="grid gap-2 rounded-card border border-line bg-surface p-3" data-testid="scan-in-progress">
      <h2 className="font-bold">{t("vault.scan.panel_title")}</h2>
      <ol className="grid gap-2">
        {PHASES.map((phase, index) => {
          const state = phaseState(phase, current);
          const shown = state === "running" ? progress[phase] : null;
          return <li key={phase} className="grid gap-1" data-testid={`scan-phase-${phase}`} data-state={state}>
            <p className="flex flex-wrap items-baseline gap-x-2">
              <span className={cn("font-semibold", state === "waiting" && "text-muted")}>{index + 1}. {t(`vault.scan.phase_${phase}`)}</span>
              <span className={cn("text-xs", state === "running" ? "text-green" : "text-muted")}>{t(`vault.scan.phase_${state}`)}</span>
            </p>
            {shown && <p className="text-muted" data-testid={`scan-${phase}`}>{shown.detail}</p>}
            {shown && shown.value !== null && <ProgressBar value={shown.value} className="max-w-md" />}
          </li>;
        })}
      </ol>
      {adoption && adoption.total > 0 && <div className="grid gap-1 border-t border-line pt-2" data-testid="adoption-progress">
        <p className="text-muted">{t("vault.adoption.in_cloud", { count: formatNumber(adoption.inCloud), total: formatNumber(adoption.total) })}</p>
        <ProgressBar value={percent(adoption.inCloud, adoption.total)} className="max-w-md" />
      </div>}
      {(failed || unconfirmed) && <p className="text-muted" data-testid="scan-after">
        {t(failed ? "vault.scan.after_resolves" : "vault.scan.after_confirms")}</p>}
    </section>}
    {failed && <p className={cn(scanning ? "text-muted" : "text-ink")} data-testid="scan-last-failed">
      {t("vault.scan.previous_failed", {
        at: formatDateTime(failedAt as string),
        cause: cause && CAUSES.has(cause) ? t("vault.scan.cause." + cause) : String(stored),
      })}
      {scanning && <> · {t("vault.scan.previous_failed_resolving")}</>}
    </p>}
  </div>;
}
