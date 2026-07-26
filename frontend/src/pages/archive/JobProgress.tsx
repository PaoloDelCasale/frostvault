import type { JobGroup } from "@/api/types";
import { ProgressBar } from "@/components/ProgressBar";
import { Button } from "@/components/ui/button";

import { operationStatusLabel } from "./actions";
import { formatBytes } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type JobProgressProps = {
  job: JobGroup;
  t: Translate;
  canCancel: boolean;
  canApprove: boolean;
  onCancel: (job: JobGroup) => void;
  onApprove: (job: JobGroup) => void;
  cancelBusy?: boolean;
  approveBusy?: boolean;
};

export function JobProgress({
  job,
  t,
  canCancel,
  canApprove,
  onCancel,
  onApprove,
  cancelBusy,
  approveBusy,
}: JobProgressProps) {
  const status = operationStatusLabel(job, t);
  const detail =
    job.total_bytes > 0
      ? t("ui.job_bytes_progress", {
          transferred: formatBytes(job.transferred_bytes),
          total: formatBytes(job.total_bytes),
        })
      : t("ui.job_files_progress", {
          completed: job.completed_count,
          total: job.item_count,
        });

  const estimateBits: string[] = [];
  if (job.estimated_cost_eur != null) {
    estimateBits.push(`~€${Number(job.estimated_cost_eur).toFixed(2)}`);
  }
  if (job.estimated_hours != null) {
    estimateBits.push(`~${job.estimated_hours}h`);
  }
  if (job.restore_tier) {
    estimateBits.push(job.restore_tier);
  }

  return (
    <div
      className="progress-stack min-w-[12rem] max-w-full"
      data-testid="job-progress"
      data-group-id={job.id}
      data-action={job.action}
      data-status={job.status}
    >
      <ProgressBar
        value={job.percent}
        label={status}
        detail={detail}
        className="min-w-[190px]"
      />
      {estimateBits.length ? (
        <p className="mt-1 text-[10px] text-muted">{estimateBits.join(" · ")}</p>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2">
        {canApprove && job.status === "pending_approval" ? (
          <Button
            type="button"
            variant="primary"
            className="min-h-11 min-w-11"
            disabled={approveBusy}
            data-testid="approve-recover"
            onClick={() => onApprove(job)}
          >
            {approveBusy ? t("ui.approving") : t("ui.approve_restore")}
          </Button>
        ) : null}
        {canCancel ? (
          <Button
            type="button"
            variant="secondary"
            className="min-h-11 min-w-11"
            disabled={cancelBusy}
            data-testid="cancel-job"
            onClick={() => onCancel(job)}
          >
            {cancelBusy ? t("ui.stopping") : t("ui.stop")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
