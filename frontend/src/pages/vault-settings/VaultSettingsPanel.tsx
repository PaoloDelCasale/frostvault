import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchGeneralVaults, fetchOperationPolicy, updateOperationPolicy } from "@/api";
import { VaultPauseControl } from "@/components/VaultPauseControl";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { UploadRetryActions } from "@/components/UploadRetryActions";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { ReconnectVaultDialog } from "@/pages/general/ReconnectVaultDialog";
import { LifecyclePanel } from "./LifecyclePanel";
import { RetentionPanel } from "./RetentionPanel";
import { UploadVerificationPanel } from "./UploadVerificationPanel";

/** One settings topic: title, a one-line summary, the controls, and optional longer help. */
function SettingsSection({ id, title, summary, more, danger, children }: {
  id: string; title: string; summary: ReactNode; more?: ReactNode; danger?: boolean; children: ReactNode;
}) {
  const { t } = useI18n();
  return <section aria-labelledby={`settings-${id}`} data-section={id}
    className={cn("grid min-w-0 gap-4 rounded-panel border bg-surface p-4 sm:p-5", danger ? "border-[var(--state-problem-bg)]" : "border-line")}>
    <div className="grid gap-1">
      <h2 id={`settings-${id}`} className={cn("text-lg font-bold", danger && "text-destructive")}>{title}</h2>
      <p className="text-sm text-muted">{summary}</p>
    </div>
    {children}
    {more && <details className="text-sm text-muted"><summary className="min-h-11 w-fit cursor-pointer py-2 font-semibold text-ink">{t("settings.more")}</summary><div className="grid gap-2">{more}</div></details>}
  </section>;
}

export function VaultSettingsPanel({ vaultId, vaultName, onStopped }: { vaultId: number; vaultName: string; onStopped: () => void }) {
  const { t } = useI18n();
  const client = useQueryClient();
  const [auto, setAuto] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [stop, setStop] = useState(false);
  const [reconnect, setReconnect] = useState(false);
  const [notice, setNotice] = useState<{ message: string; error: boolean } | null>(null);
  const show = useCallback((message: string, error = false) => setNotice({ message, error }), []);
  const vaults = useQuery({ queryKey: ["general-vaults"], queryFn: fetchGeneralVaults, retry: false });
  const vault = vaults.data?.items.find((item) => item.id === vaultId) ?? null;
  const stopped = Boolean(vault?.decommission_state && vault.decommission_state !== "active");
  useEffect(() => {
    let live = true;
    void fetchOperationPolicy(vaultId).then((data) => { if (live) setAuto(data.auto_upload); }).catch((error: unknown) => { if (live) show(String(error), true); });
    return () => { live = false; };
  }, [vaultId, show]);
  async function toggle() {
    if (auto === null) return;
    setBusy(true);
    try {
      const next = (await updateOperationPolicy({ auto_upload: !auto }, vaultId)).auto_upload;
      setAuto(next); show(t(next ? "settings.auto_on" : "settings.auto_off"));
    } catch (error) { show(String(error), true); }
    finally { setBusy(false); }
  }
  return <div className="grid min-w-0 gap-4" data-testid="vault-settings">
    {!stopped && <SettingsSection id="pause" title={t("vault_pause.pause")} summary={t("vault_pause.help")}>
      <VaultPauseControl key={vaultId} vaultId={vaultId} paused={vault?.paused} />
    </SettingsSection>}
    <SettingsSection id="protection" title={t("settings.protection_title")} summary={t("settings.protection_summary")}
      more={<><p>{t("essential.protection_help")}</p><p>{t("uploads.retry_help")}</p></>}>
      <label className="flex min-h-11 items-center gap-3 font-semibold"><input type="checkbox" className="size-5 accent-[var(--green)]" checked={auto ?? false} disabled={auto === null || busy} onChange={() => void toggle()} />{t("folder_settings.policy_auto_upload")}</label>
      <UploadRetryActions vaultId={vaultId} showHelp={false} />
    </SettingsSection>
    <SettingsSection id="verification" title={t("verification.title")} summary={t("settings.verification_summary")} more={<p>{t("verification.help")}</p>}>
      <UploadVerificationPanel vaultId={vaultId} onNotice={show} />
    </SettingsSection>
    <SettingsSection id="local" title={t("settings.local_title")} summary={t("settings.local_summary")}
      more={<><p>{t("folder_settings.retention_help")}</p><p>{t("folder_settings.retention_example")}</p><p>{t("folder_settings.retention_never")}</p></>}>
      <RetentionPanel vaultId={vaultId} onNotice={show} />
    </SettingsSection>
    <SettingsSection id="plan" title={t("folder_settings.plan_title")} summary={t("folder_settings.plan_help")}>
      <details className="rounded-card border border-line"><summary className="min-h-11 cursor-pointer px-3 py-3 font-semibold">{t("settings.plan_open")}</summary>
        <div className="border-t border-line p-3"><LifecyclePanel vaultId={vaultId} onNotice={show} /></div></details>
    </SettingsSection>
    <SettingsSection id="danger" danger title={t("settings.danger_title")} summary={t("settings.danger_summary")}>
      {vault && !stopped && <div className="grid gap-2 border-b border-line pb-4">
        <p className="text-sm">{t("general.reconnect_help")}</p>
        <p className="break-all text-xs text-muted">{t("general.reconnect_old_root")}: {vault.source_root}</p>
        <Button className="w-fit" variant="secondary" onClick={() => setReconnect(true)}>{t("general.reconnect_vault")}</Button>
      </div>}
      <div className="grid gap-2">
        <p className="text-sm">{t("stop_protection.local_retained")}</p>
        <Button className="w-fit" variant={stopped ? "secondary" : "danger"} onClick={() => setStop(true)}>{t(stopped ? "stop_protection.status" : "stop_protection.title")}</Button>
      </div>
    </SettingsSection>
    <ReconnectVaultDialog open={reconnect} onOpenChange={setReconnect} vault={vault} onNotice={show}
      onCompleted={async () => { await client.invalidateQueries({ queryKey: ["general-vaults"] }); }} />
    <StopProtectionDialog open={stop} onOpenChange={setStop} vaultId={vaultId} vaultName={vaultName} onCompleted={onStopped} />
    <Toast open={notice !== null} message={notice?.message ?? ""} variant={notice?.error ? "error" : "success"} onClose={() => setNotice(null)} />
  </div>;
}
