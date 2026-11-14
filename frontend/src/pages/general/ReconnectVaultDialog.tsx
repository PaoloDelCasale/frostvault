import { useEffect, useState, type FormEvent } from "react";

import type { GeneralVault, SourceVolumeInventoryItem } from "@/api";
import { fetchGeneralSourceVolumes, reconnectGeneralVault } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

const MANAGED_ALIAS = "managed";

/** A Vault created in `/sources/managed/<uuid>` may reconnect to that folder only (#641). */
function ownsManagedFolder(vault: GeneralVault | null): boolean {
  return !!vault && vault.source_root.replace(/\/+$/, "").endsWith(`/${MANAGED_ALIAS}/${vault.uuid}`);
}

type Props = {
  vault: GeneralVault | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNotice: (message: string, error?: boolean) => void;
  onCompleted: () => Promise<void>;
};

export function ReconnectVaultDialog({ vault, open, onOpenChange, onNotice, onCompleted }: Props) {
  const { t } = useI18n();
  const [volumes, setVolumes] = useState<SourceVolumeInventoryItem[]>([]);
  const [alias, setAlias] = useState("");
  const [path, setPath] = useState("");
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const ownManaged = ownsManagedFolder(vault);
  const managedSelected = ownManaged && alias === MANAGED_ALIAS;

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setAlias(""); setPath(""); setReason(""); setVolumes([]);
    void fetchGeneralSourceVolumes().then((data) => {
      if (!cancelled) setVolumes(data.items);
    }).catch((error: unknown) => {
      if (!cancelled) onNotice(error instanceof Error ? error.message : String(error), true);
    });
    return () => { cancelled = true; };
  }, [open, vault?.id, onNotice]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!vault || !alias || reason.trim().length < 3) return;
    setSubmitting(true);
    try {
      await reconnectGeneralVault(vault.id, {
        volume_alias: alias,
        relative_path: managedSelected ? vault.uuid : path,
        reason: reason.trim(),
      });
      onOpenChange(false);
      onNotice(t("general.reconnect_started"));
      await onCompleted();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally { setSubmitting(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("general.reconnect_vault")} description={t("general.reconnect_help")}>
      <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
        <p className="break-all text-sm text-muted">{t("general.reconnect_old_root")}: {vault?.source_root}</p>
        <FormField label={t("general.reconnect_volume")} htmlFor="reconnect-volume">
          <select id="reconnect-volume" required value={alias} onChange={(event) => setAlias(event.target.value)} className="min-h-11 rounded border border-input bg-surface px-3">
            <option value="">—</option>
            {ownManaged && <option value={MANAGED_ALIAS}>{t("general.reconnect_own_managed")}</option>}
            {volumes.filter((v) => v.access === "rw").map((v) => <option key={v.alias} value={v.alias}>{v.alias} · {v.health}</option>)}
          </select>
        </FormField>
        {managedSelected ? (
          <p className="break-all text-sm text-muted">{t("general.reconnect_own_managed_help")}</p>
        ) : (
          <FormField label={t("general.reconnect_path")} htmlFor="reconnect-path">
            <FormInput id="reconnect-path" maxLength={1024} value={path} onChange={(event) => setPath(event.target.value)} />
          </FormField>
        )}
        <FormField label={t("general.reconnect_reason")} htmlFor="reconnect-reason">
          <FormInput id="reconnect-reason" required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} />
        </FormField>
        <p className="text-sm text-muted">{t("general.reconnect_scan_warning")}</p>
        <Button type="submit" disabled={submitting || !alias || reason.trim().length < 3}>
          {submitting ? t("general.reconnect_submitting") : t("general.reconnect_confirm")}
        </Button>
      </form>
    </Dialog>
  );
}
