import { useCallback, useEffect, useState } from "react";
import { fetchGeneralVaults, type GeneralVault } from "@/api";
import type { Overview } from "@/api/vault";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { Button } from "@/components/ui/button";
import { UploadRetryActions } from "@/components/UploadRetryActions";
import { useI18n } from "@/i18n";
import { RecoveryKitSection } from "./RecoveryKitSection";
import { ReconnectVaultDialog } from "./ReconnectVaultDialog";
import { MetadataBackupsSection } from "./MetadataBackupsSection";
import { SourceVolumesSection } from "./SourceVolumesSection";

export function GeneralPage({ overview, onRefresh }: { overview?: Overview; onRefresh: () => void }) {
  const { t } = useI18n();
  const [vaults, setVaults] = useState<GeneralVault[]>([]);
  const [target, setTarget] = useState<GeneralVault | null>(null);
  const [stopped, setStopped] = useState<GeneralVault | null>(null);
  const [message, setMessage] = useState("");
  const show = useCallback((value: string) => setMessage(value), []);
  const load = useCallback(async () => { const data = await fetchGeneralVaults(); setVaults(data.items); onRefresh(); }, [onRefresh]);
  useEffect(() => { void load().catch((error: unknown) => setMessage(String(error))); }, [load]);
  return <div className="grid min-w-0 gap-6">
    <h1 className="text-2xl font-bold">{t("essential.general")}</h1>
    <section className="rounded-panel border border-line bg-surface p-4">
      <h2 className="font-bold">{t("essential.email")}</h2>
      <p>{t("essential.email_help")}</p>
      {overview?.email_configured === false ? <p role="status" data-testid="email-not-configured">{t("ui.email_not_configured")}</p> : null}
      <p>{t("essential.last_verified_backup")}: {overview?.last_verified_backup ?? t("essential.not_available")}</p>
    </section>
    <UploadRetryActions />
    <RecoveryKitSection />
    <section className="grid gap-3 rounded-panel border border-line bg-surface p-4">
      <h2 className="font-bold">{t("essential.protected_folders")}</h2>
      <a className="inline-flex min-h-11 items-center font-bold underline" href="/vaults/new">{t("essential.add_folder")}</a>
      {vaults.map((vault) => <div key={vault.id} className="flex min-w-0 flex-wrap items-center justify-between gap-3 border-t border-line py-3">
        <div className="min-w-0"><strong className="break-words">{vault.name}</strong><p className="break-all text-sm text-muted">{vault.source_root}</p><p>{t("essential.folder_spend", { cost: overview?.vaults.find((row) => row.id === vault.id)?.monthly_estimate.toFixed(2) ?? "—", currency: overview?.currency ?? "" })}</p></div>
        {(!vault.decommission_state || vault.decommission_state === "active") ? <Button variant="secondary" onClick={() => setTarget(vault)}>{t("general.reconnect_vault")}</Button> : <Button variant="secondary" onClick={() => setStopped(vault)}>{t("stop_protection.status")}</Button>}
      </div>)}
      <p className="text-sm text-muted">{t("essential.estimate_caveat")}</p>
    </section>
    <details><summary className="min-h-11 cursor-pointer py-3 font-semibold">{t("general.metadata_backups_heading")}</summary><MetadataBackupsSection /></details>
    <details><summary className="min-h-11 cursor-pointer py-3 font-semibold">{t("general.sources_heading")}</summary><SourceVolumesSection /></details>
    {message && <p role="status">{message}</p>}
    <ReconnectVaultDialog open={target !== null} onOpenChange={(open) => { if (!open) setTarget(null); }} vault={target} onNotice={show} onCompleted={load} />
    <StopProtectionDialog open={stopped !== null} onOpenChange={(open) => { if (!open) setStopped(null); }} vaultId={stopped?.id ?? 0} vaultName={stopped?.name ?? ""} onCompleted={load} />
  </div>;
}
