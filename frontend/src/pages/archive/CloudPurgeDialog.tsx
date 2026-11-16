import { useEffect, useId, useState, type FormEvent } from "react";

import type { CloudDeletionPreview, CloudDeletionSettings } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";

import { formatBytes, formatNumber, pickDurationUnit } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** Skip reasons of a folder selection (#723), in the server's order. */
const SKIP_REASONS = ["present_locally", "pending_work", "pending_cleanup", "conflict", "not_verified", "no_exact_version"] as const;

function SelectionSummary({ preview, t }: { preview: CloudDeletionPreview; t: Translate }) {
  const skipped = (preview.skipped ?? {}) as Record<string, number>;
  const reasons = SKIP_REASONS.filter((reason) => Number(skipped[reason] ?? 0) > 0)
    .map((reason) => t(`ui.cloud_purge_skip.${reason}`, { count: formatNumber(Number(skipped[reason])) }));
  return <div className="grid gap-1 text-sm" data-testid="cloud-purge-selection">
    <p data-testid="cloud-purge-eligible">
      {t("ui.cloud_purge_eligible", { files: t("ui.cloud_purge_object", { count: formatNumber(preview.eligible_count) }), bytes: formatBytes(preview.eligible_bytes) })}
      {" · "}
      {reasons.length ? t("ui.cloud_purge_skipped", { reasons: reasons.join(", ") }) : t("ui.cloud_purge_skipped_none")}
    </p>
    <p data-testid="cloud-purge-split">{t("ui.cloud_purge_split", {
      duplicates: t("ui.cloud_purge_duplicates", { count: formatNumber(preview.duplicate_count) }),
      unique: t("ui.cloud_purge_no_local_copy", { count: formatNumber(preview.no_local_copy_count) }),
    })}</p>
  </div>;
}

function formatDelay(seconds: number, t: Translate): string {
  const { value, unit } = seconds >= 86400
    ? { value: Math.round(seconds / 86400), unit: "days" }
    : pickDurationUnit(seconds);
  return t(`ui.duration_${unit}`, { count: value });
}

export type CloudPurgeDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  isDirectory?: boolean;
  vaultName: string;
  settings: CloudDeletionSettings | null;
  preview: CloudDeletionPreview | null;
  t: Translate;
  /** The deletion request is running: the dialog keeps itself and its fields locked. */
  busy?: boolean;
  /** Failure of the last request, shown inside the dialog (#651). */
  error?: string;
  /** «Only Cloud Duplicates» (#723): shown when the caller can re-preview on change. */
  duplicatesOnly?: boolean;
  onDuplicatesOnlyChange?: (value: boolean) => void;
  /** A new preview is loading: confirmation waits for it. */
  previewing?: boolean;
  onConfirm: (payload: {
    reason: string;
    confirmation: string;
  }) => void;
};

export function CloudPurgeDialog({
  open,
  onOpenChange,
  path,
  isDirectory = false,
  vaultName,
  settings,
  preview,
  t,
  busy = false,
  error = "",
  duplicatesOnly = false,
  onDuplicatesOnlyChange,
  previewing = false,
  onConfirm,
}: CloudPurgeDialogProps) {
  const id = useId();
  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");

  useEffect(() => {
    if (!open) {
      setReason("");
      setConfirmation("");
    }
  }, [open]);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || previewing || !reason.trim() || confirmation !== vaultName || !preview || !preview.object_count) return;
    onConfirm({
      reason: reason.trim(),
      confirmation,
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("ui.cloud_purge_title")}
      closeLabel={t("ui.close")}
      description={`${t("ui.cloud_purge_intro")} (${path}${isDirectory ? "/" : ""})`}
      className="w-[min(28rem,calc(100%-1.75rem))]"
    >
      <form className="grid gap-3" onSubmit={handleSubmit} data-testid="cloud-purge-form">
        <p className="text-sm text-muted" data-testid="cloud-purge-local-note">
          {t("ui.cloud_purge_local_note")}
        </p>
        {onDuplicatesOnlyChange ? (
          <label className="flex min-h-11 items-start gap-3 text-sm" htmlFor={`${id}-duplicates`}>
            <input
              id={`${id}-duplicates`}
              type="checkbox"
              role="switch"
              className="mt-0.5 size-5 shrink-0 accent-[var(--green)]"
              checked={duplicatesOnly}
              disabled={busy || previewing}
              onChange={(event) => onDuplicatesOnlyChange(event.target.checked)}
              data-testid="cloud-purge-duplicates-only"
            />
            <span className="grid gap-0.5">
              <span className="font-semibold">{t("ui.cloud_purge_duplicates_only")}</span>
              <span className="text-muted">{t("ui.cloud_purge_duplicates_only_help")}</span>
            </span>
          </label>
        ) : null}
        {previewing ? <p role="status" className="text-sm text-muted" data-testid="cloud-purge-previewing">{t("ui.cloud_purge_previewing")}</p> : null}
        {preview && !previewing && preview.mode === "selection" ? <SelectionSummary preview={preview} t={t} /> : null}
        {preview && !previewing && !preview.object_count ? (
          <p className="text-sm font-semibold" data-testid="cloud-purge-nothing">{t("ui.cloud_purge_nothing")}</p>
        ) : null}
        {preview && !previewing && preview.object_count > 0 ? (
          <p className="text-sm font-bold text-ink" data-testid="cloud-purge-preview">
            {t("ui.cloud_purge_preview", {
              objects: t("ui.cloud_purge_object", { count: preview.object_count }),
              versions: t("ui.cloud_purge_version", { count: preview.version_count }),
              markers: t("ui.cloud_purge_marker", { count: preview.delete_marker_count }),
              bytes: formatBytes(preview.byte_count),
            })}
          </p>
        ) : null}
        {settings?.purge_delay_seconds != null ? (
          <p className="text-sm text-muted" data-testid="cloud-purge-delay">
            {t("ui.cloud_purge_delay", {
              delay: formatDelay(settings.purge_delay_seconds, t),
            })}
          </p>
        ) : null}
        <FormField label={t("ui.cloud_purge_reason")} htmlFor={`${id}-reason`}>
          <FormInput
            id={`${id}-reason`}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={busy}
            required
            minLength={1}
            data-testid="cloud-purge-reason"
          />
        </FormField>
        <FormField
          label={t("ui.cloud_purge_confirm_label", { vault: vaultName })}
          htmlFor={`${id}-confirm`}
        >
          <FormInput
            id={`${id}-confirm`}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            disabled={busy}
            required
            minLength={1}
            data-testid="cloud-purge-confirmation"
            placeholder={vaultName}
          />
        </FormField>
        {error ? (
          <p role="alert" className="text-sm font-semibold text-destructive" data-testid="cloud-purge-error">
            {error}
          </p>
        ) : null}
        <div className="mt-2 flex flex-wrap justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            className="min-h-11 min-w-11"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            {t("ui.cancel")}
          </Button>
          <Button
            type="submit"
            variant="danger"
            className="min-h-11 min-w-11"
            disabled={busy || previewing || !preview || !preview.object_count || !reason.trim() || confirmation !== vaultName}
            aria-busy={busy || undefined}
            data-testid="cloud-purge-submit"
          >
            {busy ? t("ui.cloud_purge_scheduling") : t("ui.cloud_purge_submit")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
