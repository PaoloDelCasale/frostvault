import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiQueryKeys, readOnMount, vaultApi, type StopProtectionStatus } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { errorMessage } from "@/lib/errorMessage";

export function StopProtectionDialog({ open, onOpenChange, vaultId, vaultName, onCompleted }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vaultId: number;
  vaultName: string;
  /** Runs when the dialog closes after a change, so the outcome stays readable until then (#652). */
  onCompleted?: () => void | Promise<void>;
}) {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
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
    setOperation(null);
    setChoice("keep");
    setConfirmation("");
    setReason("");
    setError("");
  }, [open, vaultId]);
  // Each opening reads the status anew; an answer from before is never shown again.
  const loaded = useQuery({ queryKey: apiQueryKeys.stopProtection(vaultId), queryFn: () => vaultApi(vaultId).stopProtection(), enabled: open, ...readOnMount });
  useEffect(() => {
    if (open && loaded.data && !loaded.isFetching) setOperation(loaded.data);
  }, [open, vaultId, loaded.data, loaded.dataUpdatedAt, loaded.isFetching]);
  useEffect(() => {
    if (open && loaded.error) setError(errorMessage(loaded.error, translateRef.current));
  }, [open, loaded.error, loaded.errorUpdatedAt]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setOperation(await vaultApi(vaultId).stop({ cloud_disposition: choice, confirmation, reason }));
      changed.current = true;
    } catch (failure) {
      setError(errorMessage(failure, t));
    } finally { setBusy(false); }
  }

  async function cancel() {
    setBusy(true);
    setError("");
    try {
      setOperation(await vaultApi(vaultId).cancelStoppedDeletion());
      changed.current = true;
    } catch (failure) {
      setError(errorMessage(failure, t));
    } finally { setBusy(false); }
  }

  async function keepRemaining() {
    setBusy(true);
    setError("");
    try {
      setOperation(await vaultApi(vaultId).stop({ cloud_disposition: "keep", confirmation: vaultName,
        reason: t("stop_protection.keep_remaining") }));
      changed.current = true;
    } catch (failure) {
      setError(errorMessage(failure, t));
    } finally { setBusy(false); }
  }

  // Local data is never deleted: the cloud copy can go only when nothing is
  // still present on the server (#715), so say so before confirmation.
  const localFiles = operation?.local_files_blocking_cloud_delete ?? 0;
  const deleteBlocked = localFiles > 0;

  useEffect(() => {
    if (deleteBlocked) setChoice("keep");
  }, [deleteBlocked]);

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
          void vaultApi(vaultId).stopProtection().then(setOperation).catch((failure: unknown) => {
            setError(errorMessage(failure, t));
          });
        }}>{t("stop_protection.refresh")}</Button>
        <Button onClick={() => handleOpenChange(false)}>{t("ui.close")}</Button>
      </div> : <form onSubmit={(event) => void submit(event)} className="grid gap-4">
        <FormField label={t("stop_protection.cloud_choice")} htmlFor="stop-cloud-choice">
          <select id="stop-cloud-choice" value={choice} onChange={(event) => setChoice(event.target.value === "delete" ? "delete" : "keep")}
            className="min-h-11 rounded border border-input bg-surface px-3">
            <option value="keep">{t("stop_protection.keep")}</option>
            <option value="delete" disabled={deleteBlocked}>{t("stop_protection.delete")}</option>
          </select>
        </FormField>
        {deleteBlocked ? <p className="text-sm text-muted" data-testid="stop-delete-unavailable">
          {t("stop_protection.delete_unavailable", { count: localFiles })}
        </p> : null}
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
