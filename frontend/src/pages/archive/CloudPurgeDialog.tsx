import { useEffect, useId, useState, type FormEvent } from "react";

import type { CloudDeletionPreview, CloudDeletionSettings } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type CloudPurgeDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  vaultName: string;
  settings: CloudDeletionSettings | null;
  preview: CloudDeletionPreview | null;
  t: Translate;
  onConfirm: (payload: {
    reason: string;
    confirmation: string;
    generated_phrase: string;
  }) => void;
};

export function CloudPurgeDialog({
  open,
  onOpenChange,
  path,
  vaultName,
  settings,
  preview,
  t,
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

  const phrase = settings?.generated_phrase ?? "";

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!reason.trim() || !confirmation.trim() || !phrase) return;
    onConfirm({
      reason: reason.trim(),
      confirmation: confirmation.trim(),
      generated_phrase: phrase,
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("ui.cloud_purge_title")}
      description={`${t("ui.cloud_purge_intro")} (${path})`}
      className="w-[min(28rem,calc(100%-1.75rem))]"
    >
      <form className="grid gap-3" onSubmit={handleSubmit} data-testid="cloud-purge-form">
        {settings?.delete_marker_explanation ? (
          <p className="text-sm text-muted">{settings.delete_marker_explanation}</p>
        ) : null}
        {preview ? (
          <p className="text-sm font-bold text-ink" data-testid="cloud-purge-preview">
            {t("ui.cloud_purge_preview", {
              objects: preview.object_count,
              versions: preview.version_count,
              markers: preview.delete_marker_count,
              bytes: preview.byte_count,
            })}
          </p>
        ) : null}
        {settings?.purge_delay_seconds != null ? (
          <p className="text-sm text-muted">
            {t("ui.cloud_purge_delay", {
              seconds: settings.purge_delay_seconds,
            })}
          </p>
        ) : null}
        <FormField label={t("ui.cloud_purge_reason")} htmlFor={`${id}-reason`}>
          <FormInput
            id={`${id}-reason`}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
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
            required
            minLength={1}
            data-testid="cloud-purge-confirmation"
            placeholder={phrase || vaultName}
          />
        </FormField>
        {phrase ? (
          <p className="text-xs text-muted" data-testid="cloud-purge-phrase">
            {t("ui.cloud_purge_phrase")}: {phrase}
          </p>
        ) : null}
        <div className="mt-2 flex flex-wrap justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            className="min-h-11 min-w-11"
            onClick={() => onOpenChange(false)}
          >
            {t("ui.cancel")}
          </Button>
          <Button
            type="submit"
            variant="danger"
            className="min-h-11 min-w-11"
            disabled={!reason.trim() || !confirmation.trim()}
            data-testid="cloud-purge-submit"
          >
            {t("ui.cloud_purge_submit")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
