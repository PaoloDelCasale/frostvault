import { useEffect, useRef, useState, type FormEvent } from "react";

import type { GeneralVault, SourceVolumeInventoryItem } from "@/api";
import { browseSourceVolume, fetchGeneralSourceVolumes, reconnectGeneralVault } from "@/api";
import { Dialog } from "@/components/Dialog";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { errorMessage } from "@/lib/errorMessage";

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
  const translateRef = useRef(t);
  translateRef.current = t;
  const [volumes, setVolumes] = useState<SourceVolumeInventoryItem[]>([]);
  const [alias, setAlias] = useState("");
  const [path, setPath] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Shown inside the dialog: a Toast behind the modal would go unseen (#651).
  const [error, setError] = useState("");
  const ownManaged = ownsManagedFolder(vault);
  const managedSelected = ownManaged && alias === MANAGED_ALIAS;

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setAlias(""); setPath(null); setReason(""); setVolumes([]); setLoadError(""); setError("");
    void fetchGeneralSourceVolumes().then((data) => {
      if (!cancelled) setVolumes(data.items);
    }).catch((error: unknown) => {
      if (!cancelled) setLoadError(errorMessage(error, translateRef.current));
    });
    return () => { cancelled = true; };
  }, [open, vault?.id]);
  const ready = !!alias && (managedSelected || path !== null) && reason.trim().length >= 3;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!vault || !ready) return;
    setSubmitting(true); setError("");
    try {
      await reconnectGeneralVault(vault.id, {
        volume_alias: alias,
        relative_path: managedSelected ? vault.uuid : path ?? "",
        reason: reason.trim(),
      });
      onOpenChange(false);
      onNotice(t("general.reconnect_started"));
      await onCompleted();
    } catch (failure) {
      setError(errorMessage(failure, t));
    } finally { setSubmitting(false); }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (next || !submitting) onOpenChange(next); }} title={t("general.reconnect_vault")} description={t("general.reconnect_help")} closeLabel={t("ui.close")}>
      <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
        <p className="break-all text-sm text-muted">{t(vault?.lifecycle === "reconnect_required" ? "general.reconnect_old_root" : "general.reconnect_current_root")}: {vault?.source_root}</p>
        {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
        {error ? <p role="alert" className="text-sm font-semibold text-destructive">{error}</p> : null}
        <FormField label={t("general.reconnect_volume")} htmlFor="reconnect-volume">
          <select id="reconnect-volume" required value={alias} onChange={(event) => { setAlias(event.target.value); setPath(null); }} className="min-h-11 rounded border border-input bg-surface px-3">
            <option value="">—</option>
            {ownManaged && <option value={MANAGED_ALIAS}>{t("general.reconnect_own_managed")}</option>}
            {volumes.filter((v) => v.access === "rw").map((v) => <option key={v.alias} value={v.alias}>{v.alias} · {t(`general.sources_health_${v.health}`)}</option>)}
          </select>
        </FormField>
        {managedSelected ? (
          <p className="break-all text-sm text-muted">{t("general.reconnect_own_managed_help")}</p>
        ) : alias ? (
          <div className="grid gap-2" role="group" aria-labelledby="reconnect-path-label">
            <p id="reconnect-path-label" className="text-sm font-semibold">{t("general.reconnect_path")}</p>
            <SourceDirectoryBrowser key={alias} volumeAlias={alias} browse={(volume, relative) => browseSourceVolume(volume, relative, vault?.id)} selectedPath={path} onSelect={setPath} />
          </div>
        ) : null}
        <FormField label={t("general.reconnect_reason")} htmlFor="reconnect-reason">
          <FormInput id="reconnect-reason" required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} />
        </FormField>
        <p className="text-sm text-muted">{t("general.reconnect_scan_warning")}</p>
        <Button type="submit" disabled={submitting || !ready}>
          {submitting ? t("general.reconnect_submitting") : t("general.reconnect_confirm")}
        </Button>
      </form>
    </Dialog>
  );
}
