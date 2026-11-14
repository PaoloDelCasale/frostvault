import { useEffect, useRef, useState, type FormEvent } from "react";

import { cancelStoppedFolderDeletion, fetchStopProtection, stopProtection } from "@/api";
import type { StopProtectionStatus } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { formatDateTime } from "@/pages/archive/format";

export function StopProtectionDialog({ open, onOpenChange, vaultId, vaultName, onCompleted }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vaultId: number;
  vaultName: string;
  /** Runs when the dialog closes after a change, so the outcome stays readable until then (#652). */
  onCompleted?: () => void | Promise<void>;
}) {
  const { t } = useI18n();
  const [operation, setOperation] = useState<StopProtectionStatus | null>(null);
  const [choice, setChoice] = useState<"keep" | "delete">("keep");
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const changed = useRef(false);

  function handleOpenChange(next: boolean) {
    onOpenChange(next);
    if (!next && changed.current) {
      changed.current = false;
      void onCompleted?.();
    }
  }

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setOperation(null);
    setChoice("keep");
    setConfirmation("");
    setReason("");
    setError("");
    void fetchStopProtection(vaultId).then((value) => {
      if (!cancelled) setOperation(value);
    }).catch((failure: unknown) => {
      if (!cancelled) setError(failure instanceof Error ? failure.message : String(failure));
    });
    return () => { cancelled = true; };
  }, [open, vaultId]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setOperation(await stopProtection(vaultId, { cloud_disposition: choice, confirmation, reason }));
      changed.current = true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally { setBusy(false); }
  }

  async function cancel() {
    setBusy(true);
    setError("");
    try {
      setOperation(await cancelStoppedFolderDeletion(vaultId));
      changed.current = true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally { setBusy(false); }
  }

  async function keepRemaining() {
    setBusy(true);
    setError("");
    try {
      setOperation(await stopProtection(vaultId, { cloud_disposition: "keep", confirmation: vaultName,
        reason: t("stop_protection.keep_remaining") }));
      changed.current = true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally { setBusy(false); }
  }

  const failed = operation?.jobs.some((job) => job.status === "failed")
    && operation.jobs.every((job) => ["completed", "cancelled", "failed"].includes(job.status));

  return <Dialog open={open} onOpenChange={handleOpenChange} title={t("stop_protection.title")}
    closeLabel={t("ui.close")}
    description={t("stop_protection.description", { vault: vaultName })}>
    <div className="grid gap-4">
      <p className="text-sm text-muted">{t("stop_protection.local_retained")}</p>
      {error ? <p role="alert" className="text-destructive">{error}</p> : null}
      {operation?.stopped ? <div className="grid gap-3" data-testid="stop-protection-status">
        <p>{t(operation.root_released ? "stop_protection.stopped" : "stop_protection.pending")}</p>
        {operation.pending_until ? <p>{t("stop_protection.deadline", { date: formatDateTime(operation.pending_until) })}</p> : null}
        <p className="text-sm text-muted">{t("stop_protection.cloud_retained_help")}</p>
        {failed && !operation.root_released ? <>
          <p role="alert">{t("stop_protection.failed_help")}</p>
          <Button variant="secondary" disabled={busy} onClick={() => void keepRemaining()}>
            {t("stop_protection.keep_remaining")}
          </Button>
        </> : null}
        {operation.cancellable ? <Button variant="secondary" disabled={busy} onClick={() => void cancel()}>
          {t("stop_protection.cancel_deletion")}
        </Button> : null}
        <Button variant="secondary" onClick={() => {
          void fetchStopProtection(vaultId).then(setOperation).catch((failure: unknown) => {
            setError(failure instanceof Error ? failure.message : String(failure));
          });
        }}>{t("stop_protection.refresh")}</Button>
        <Button onClick={() => handleOpenChange(false)}>{t("ui.close")}</Button>
      </div> : <form onSubmit={(event) => void submit(event)} className="grid gap-4">
        <FormField label={t("stop_protection.cloud_choice")} htmlFor="stop-cloud-choice">
          <select id="stop-cloud-choice" value={choice} onChange={(event) => setChoice(event.target.value === "delete" ? "delete" : "keep")}
            className="min-h-11 rounded border border-input bg-surface px-3">
            <option value="keep">{t("stop_protection.keep")}</option>
            <option value="delete">{t("stop_protection.delete")}</option>
          </select>
        </FormField>
        {choice === "delete" ? <p className="text-sm text-destructive">{t("stop_protection.delete_help")}</p> : null}
        <FormField label={t("stop_protection.confirmation", { vault: vaultName })} htmlFor="stop-confirmation">
          <FormInput id="stop-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required />
        </FormField>
        <FormField label={t("stop_protection.reason")} htmlFor="stop-reason">
          <FormInput id="stop-reason" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required />
        </FormField>
        <Button variant="danger" type="submit" disabled={busy || !operation || confirmation !== vaultName || !reason.trim()}>
          {t("stop_protection.title")}
        </Button>
      </form>}
    </div>
  </Dialog>;
}
