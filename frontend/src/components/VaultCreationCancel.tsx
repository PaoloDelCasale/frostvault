import { useState } from "react";

import { vaultApi, type VaultCreationCancelled } from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { errorMessage } from "@/lib/errorMessage";

type Props = {
  vaultId: number;
  vaultName: string;
  /** The reserved bucket name, shown in the confirmation. */
  bucket: string;
  disabled?: boolean;
  size?: "sm" | "default";
  onCancelled: (outcome: VaultCreationCancelled) => void;
};

/**
 * "Cancel creation" of a Vault whose bucket is not ready yet (#735). The
 * server asks for reauthentication first, like the other destructive actions;
 * the API client handles that prompt and replays the request.
 */
export function VaultCreationCancel({ vaultId, vaultName, bucket, disabled = false, size = "default", onCancelled }: Props) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const outcome = await vaultApi(vaultId).cancelCreation();
      setOpen(false);
      onCancelled(outcome);
    } catch (err) {
      setError(errorMessage(err, t));
    } finally {
      setBusy(false);
    }
  }

  return <>
    <Button type="button" variant="secondary" size={size} disabled={disabled || busy} onClick={() => { setError(null); setOpen(true); }}
      data-testid="vault-creation-cancel">
      {t("ui.vault_create.cancel_creation")}
    </Button>
    <ConfirmDialog open={open} onOpenChange={(next) => { if (!busy) setOpen(next); }}
      title={t("ui.vault_create.cancel_creation_title", { vault: vaultName })}
      description={t("ui.vault_create.cancel_creation_body", { bucket })}
      confirmLabel={t("ui.vault_create.cancel_creation")} cancelLabel={t("ui.vault_create.cancel_creation_keep")}
      confirmDisabled={busy} keepOpenOnConfirm onConfirm={() => void confirm()}>
      {error ? <p className="m-0 rounded-[10px] bg-red-soft px-3.5 py-3 text-sm text-ink" role="alert">{error}</p> : null}
    </ConfirmDialog>
  </>;
}
