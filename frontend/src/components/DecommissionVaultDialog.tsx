import { useEffect, useRef, useState, type FormEvent } from "react";

import type {
  VaultDecommissionPreview,
  VaultDecommissionSelection,
  VaultDecommissionStartPayload,
  VaultDecommissionStatus,
} from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { ProgressBar } from "@/components/ProgressBar";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export type DecommissionVaultDialogProps = {
  open: boolean;
  vaultName: string;
  existingState?: string;
  onOpenChange: (open: boolean) => void;
  preview: (
    selection: VaultDecommissionSelection,
  ) => Promise<VaultDecommissionPreview>;
  start: (
    payload: VaultDecommissionStartPayload,
  ) => Promise<VaultDecommissionStatus>;
  status: () => Promise<VaultDecommissionStatus>;
  cancelCloudPurge?: () => Promise<VaultDecommissionStatus>;
  onCompleted?: () => void | Promise<void>;
};

function formatBytes(value: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(value || 0);
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function DecommissionVaultDialog({
  open,
  vaultName,
  existingState = "active",
  onOpenChange,
  preview: requestPreview,
  start: requestStart,
  status: requestStatus,
  cancelCloudPurge,
  onCompleted,
}: DecommissionVaultDialogProps) {
  const { t } = useI18n();
  const [selection, setSelection] = useState<VaultDecommissionSelection>({
    local_disposition: "retain",
    cloud_disposition: "retain",
  });
  const [preview, setPreview] = useState<VaultDecommissionPreview | null>(null);
  const [operation, setOperation] = useState<VaultDecommissionStatus | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const completionReported = useRef(false);
  const operationVaultName = useRef<string | null>(
    existingState === "decommissioning" || existingState === "decommissioned"
      ? vaultName
      : null,
  );

  useEffect(() => {
    // Admin closes temporarily clear the selected Vault props. Retain the
    // operation key across that empty state, but never apply it to another Vault.
    if (!vaultName) return;
    if (existingState === "decommissioning" || existingState === "decommissioned") {
      operationVaultName.current = vaultName;
    } else if (operationVaultName.current !== vaultName) {
      operationVaultName.current = null;
    }
  }, [existingState, vaultName]);

  useEffect(() => {
    if (!open) return;
    setSelection({ local_disposition: "retain", cloud_disposition: "retain" });
    setPreview(null);
    setOperation(null);
    setConfirmation("");
    setReason("");
    setError("");
    setBusy(false);
    completionReported.current = false;
  }, [open, vaultName]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError("");
    setPreview(null);
    const load =
      operationVaultName.current === vaultName ||
      existingState === "decommissioning" ||
      existingState === "decommissioned"
        ? requestStatus().then((value) => {
            if (!cancelled) {
              operationVaultName.current = vaultName;
              setOperation(value);
            }
          })
        : requestPreview(selection).then((value) => {
            if (!cancelled) setPreview(value);
          });
    void load.catch((reasonValue: unknown) => {
      if (!cancelled) {
        setError(
          reasonValue instanceof Error ? reasonValue.message : String(reasonValue),
        );
      }
    });
    return () => {
      cancelled = true;
    };
  }, [existingState, open, requestPreview, requestStatus, selection, vaultName]);

  useEffect(() => {
    if (!open || !operation || operation.state === "completed") return;
    const timer = window.setInterval(() => {
      void requestStatus()
        .then((value) => {
          setOperation(value);
          setError("");
        })
        .catch((reasonValue: unknown) => {
          setError(
            reasonValue instanceof Error
              ? reasonValue.message
              : String(reasonValue),
          );
        });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [open, operation, requestStatus]);

  useEffect(() => {
    if (
      operation?.state !== "completed" ||
      completionReported.current ||
      !onCompleted
    ) {
      return;
    }
    completionReported.current = true;
    void onCompleted();
  }, [onCompleted, operation?.state]);

  async function cancelPurgeDelay() {
    if (!cancelCloudPurge) return;
    setBusy(true);
    setError("");
    try {
      setOperation(await cancelCloudPurge());
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : String(reasonValue));
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !preview?.can_start ||
      confirmation !== vaultName ||
      reason.trim().length < 3
    ) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const value = await requestStart({
        ...selection,
        confirmation,
        reason: reason.trim(),
        preview_fingerprint: preview.fingerprint,
      });
      operationVaultName.current = vaultName;
      setOperation(value);
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : String(reasonValue));
      try {
        setPreview(await requestPreview(selection));
      } catch {
        // Preserve the mutation error; a later choice/open refresh retries preview.
      }
    } finally {
      setBusy(false);
    }
  }

  const counts = preview?.counts;
  const exactConfirmation = confirmation === vaultName;
  const stateKey = operation
    ? `decommission.state.${operation.state}`
    : "decommission.state.preview";

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("decommission.title")}
      description={t("decommission.description", { vault: vaultName })}
      className="max-h-[calc(100svh-30px)] overflow-y-auto"
    >
      {operation ? (
        <div className="grid gap-4" data-testid="decommission-progress">
          <div className="rounded-[14px] border border-line bg-canvas p-3">
            <p className="text-sm font-bold text-ink">{t(stateKey)}</p>
            <p className="mt-1 text-sm text-muted">
              {t("decommission.progress_dispositions", {
                local: t(`decommission.local.${operation.local_disposition}`),
                cloud: t(`decommission.cloud.${operation.cloud_disposition}`),
              })}
            </p>
          </div>
          <ProgressBar
            value={operation.progress_percent}
            label={t("decommission.progress_label")}
            detail={t("decommission.progress_detail", {
              local: t(`decommission.status.${operation.local_status}`),
              cloud: t(`decommission.status.${operation.cloud_status}`),
            })}
          />
          {operation.error_message ? (
            <p className="rounded-[12px] border border-red-300 bg-red-50 p-3 text-sm text-red-800" role="alert">
              {operation.error_message}
            </p>
          ) : null}
          {operation.root_released ? (
            <p className="rounded-[12px] border border-green/40 bg-canvas p-3 text-sm font-bold text-green" role="status">
              {t("decommission.root_released")}
            </p>
          ) : (
            <p className="text-sm text-muted">{t("decommission.root_reserved")}</p>
          )}
          {operation.cloud_cancellable && cancelCloudPurge ? (
            <Button
              type="button"
              variant="danger"
              disabled={busy}
              onClick={() => void cancelPurgeDelay()}
            >
              {busy ? t("decommission.cancelling_purge") : t("decommission.cancel_purge")}
            </Button>
          ) : null}
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
            {t("decommission.close")}
          </Button>
        </div>
      ) : (
        <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
          <fieldset className="grid gap-2 rounded-[14px] border border-line p-3">
            <legend className="px-1 text-sm font-bold text-ink">
              {t("decommission.local_choice")}
            </legend>
            <label className="flex min-h-11 items-start gap-3 text-sm">
              <input
                type="radio"
                name="decommission-local"
                value="retain"
                checked={selection.local_disposition === "retain"}
                onChange={() =>
                  setSelection((value) => ({ ...value, local_disposition: "retain" }))
                }
              />
              <span>
                <strong className="block">{t("decommission.local.retain")}</strong>
                <span className="text-muted">{t("decommission.local_retain_help")}</span>
              </span>
            </label>
            <label className="flex min-h-11 items-start gap-3 text-sm">
              <input
                type="radio"
                name="decommission-local"
                value="remove"
                checked={selection.local_disposition === "remove"}
                onChange={() =>
                  setSelection((value) => ({ ...value, local_disposition: "remove" }))
                }
              />
              <span>
                <strong className="block">{t("decommission.local.remove")}</strong>
                <span className="text-muted">{t("decommission.local_remove_help")}</span>
              </span>
            </label>
          </fieldset>

          <fieldset className="grid gap-2 rounded-[14px] border border-line p-3">
            <legend className="px-1 text-sm font-bold text-ink">
              {t("decommission.cloud_choice")}
            </legend>
            <label className="flex min-h-11 items-start gap-3 text-sm">
              <input
                type="radio"
                name="decommission-cloud"
                value="retain"
                checked={selection.cloud_disposition === "retain"}
                onChange={() =>
                  setSelection((value) => ({ ...value, cloud_disposition: "retain" }))
                }
              />
              <span>
                <strong className="block">{t("decommission.cloud.retain")}</strong>
                <span className="text-muted">{t("decommission.cloud_retain_help")}</span>
              </span>
            </label>
            <label className="flex min-h-11 items-start gap-3 text-sm">
              <input
                type="radio"
                name="decommission-cloud"
                value="purge"
                checked={selection.cloud_disposition === "purge"}
                onChange={() =>
                  setSelection((value) => ({ ...value, cloud_disposition: "purge" }))
                }
              />
              <span>
                <strong className="block">{t("decommission.cloud.purge")}</strong>
                <span className="text-muted">{t("decommission.cloud_purge_help")}</span>
              </span>
            </label>
          </fieldset>

          {counts ? (
            <div className="grid grid-cols-2 gap-2 rounded-[14px] border border-line bg-canvas p-3 text-sm sm:grid-cols-4" data-testid="decommission-counts">
              <span>{t("decommission.count_files", { count: counts.vault_files })}</span>
              <span>{t("decommission.count_local", { count: counts.local_files, bytes: formatBytes(counts.local_bytes) })}</span>
              <span>{t("decommission.count_versions", { count: counts.archive_versions, bytes: formatBytes(counts.cloud_bytes) })}</span>
              <span>{t("decommission.count_markers", { count: counts.delete_markers })}</span>
              <span>{t("decommission.count_jobs", { count: counts.jobs })}</span>
              <span>{t("decommission.count_members", { count: counts.memberships })}</span>
            </div>
          ) : (
            <p className="text-sm text-muted" role="status">
              {t("decommission.preview_loading")}
            </p>
          )}

          {preview?.blockers.length ? (
            <div className="rounded-[14px] border border-red-300 bg-red-50 p-3" role="alert">
              <p className="text-sm font-bold text-red-800">{t("decommission.blockers_title")}</p>
              <ul className="mt-2 list-disc pl-5 text-sm text-red-800">
                {preview.blockers.map((blocker) => (
                  <li key={blocker.code}>
                    {t(blocker.message_key ?? `decommission.blocker.${blocker.code}`, {
                      count: blocker.count ?? 0,
                    })}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {preview ? (
            <p className="break-all text-xs text-muted" data-testid="decommission-fingerprint">
              {t("decommission.fingerprint")}: {preview.fingerprint}
            </p>
          ) : null}
          <p className="text-sm text-muted">{t("decommission.tombstone_help")}</p>

          <FormField label={t("decommission.reason")} htmlFor="decommission-reason">
            <FormInput
              id="decommission-reason"
              required
              minLength={3}
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </FormField>
          <FormField
            label={t("decommission.confirmation", { vault: vaultName })}
            htmlFor="decommission-confirmation"
          >
            <FormInput
              id="decommission-confirmation"
              required
              autoComplete="off"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </FormField>
          {confirmation && !exactConfirmation ? (
            <p className="text-sm text-red-700" role="alert">
              {t("decommission.confirmation_mismatch")}
            </p>
          ) : null}
          {error ? (
            <p className="text-sm text-red-700" role="alert">{error}</p>
          ) : null}
          <Button
            type="submit"
            variant="danger"
            disabled={
              busy ||
              !preview?.can_start ||
              !exactConfirmation ||
              reason.trim().length < 3
            }
          >
            {busy ? t("decommission.submitting") : t("decommission.submit")}
          </Button>
        </form>
      )}
    </Dialog>
  );
}
