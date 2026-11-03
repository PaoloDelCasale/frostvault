import { useEffect, useState, type FormEvent } from "react";

import type { AdminVault, SourceVolumeInventoryItem } from "@/api";
import { fetchAdminSourceVolumes, reconnectAdminVault } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

type Props = {
  vault: AdminVault | null;
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

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setAlias(""); setPath(""); setReason(""); setVolumes([]);
    void fetchAdminSourceVolumes().then((data) => {
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
      await reconnectAdminVault(vault.id, { volume_alias: alias, relative_path: path, reason: reason.trim() });
      onOpenChange(false);
      onNotice(t("admin.reconnect_started"));
      await onCompleted();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally { setSubmitting(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("admin.reconnect_vault")} description={t("admin.reconnect_help")}>
      <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
        <p className="break-all text-sm text-muted">{t("admin.reconnect_old_root")}: {vault?.source_root}</p>
        <FormField label={t("admin.reconnect_volume")} htmlFor="reconnect-volume">
          <select id="reconnect-volume" required value={alias} onChange={(event) => setAlias(event.target.value)} className="min-h-11 rounded border border-input bg-surface px-3">
            <option value="">—</option>
            {volumes.filter((v) => v.access === "rw").map((v) => <option key={v.alias} value={v.alias}>{v.alias} · {v.health}</option>)}
          </select>
        </FormField>
        <FormField label={t("admin.reconnect_path")} htmlFor="reconnect-path">
          <FormInput id="reconnect-path" maxLength={1024} value={path} onChange={(event) => setPath(event.target.value)} />
        </FormField>
        <FormField label={t("admin.reconnect_reason")} htmlFor="reconnect-reason">
          <FormInput id="reconnect-reason" required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} />
        </FormField>
        <p className="text-sm text-muted">{t("admin.reconnect_scan_warning")}</p>
        <Button type="submit" disabled={submitting || !alias || reason.trim().length < 3}>
          {submitting ? t("admin.reconnect_submitting") : t("admin.reconnect_confirm")}
        </Button>
      </form>
    </Dialog>
  );
}
