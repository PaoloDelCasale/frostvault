import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchGeneralVaults, fetchOperationPolicy, updateOperationPolicy } from "@/api";
import { VaultPauseControl } from "@/components/VaultPauseControl";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { notify } from "@/components/toastQueue";
import { SavedIndicator } from "@/components/SavedIndicator";
import { useSavedFlash } from "@/components/useSavedFlash";
import { Button } from "@/components/ui/button";
import { UploadRetryActions } from "@/components/UploadRetryActions";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { ReconnectVaultDialog } from "@/pages/general/ReconnectVaultDialog";
import { LifecyclePanel } from "./LifecyclePanel";
import { RetentionPanel } from "./RetentionPanel";
import { UploadVerificationPanel } from "./UploadVerificationPanel";
import { errorMessage } from "@/lib/errorMessage";

/** One settings topic: title, a one-line summary, the controls, and optional longer help. */
function SettingsSection({ id, title, summary, more, danger, highlighted, children }: {
  id: string; title: string; summary: ReactNode; more?: ReactNode; danger?: boolean; highlighted?: boolean; children: ReactNode;
}) {
  const { t } = useI18n();
  return <section id={id} aria-labelledby={`settings-${id}`} data-section={id} data-highlighted={highlighted ? "true" : undefined}
    className={cn("grid min-w-0 scroll-mt-4 gap-4 rounded-panel border bg-surface p-4 transition-shadow sm:p-5", danger ? "border-[var(--state-problem-bg)]" : "border-line", highlighted && "ring-2 ring-ring ring-offset-2 ring-offset-bg")}>
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
  const translateRef = useRef(t);
  translateRef.current = t;
  const client = useQueryClient();
  const [auto, setAuto] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [stop, setStop] = useState(false);
  const [reconnect, setReconnect] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [autoSaved, flashAutoSaved] = useSavedFlash();
  const show = useCallback((message: string, error = false) => notify(message, error ? "error" : "success"), []);
  const vaults = useQuery({ queryKey: ["general-vaults"], queryFn: fetchGeneralVaults, retry: false });
  const vault = vaults.data?.items.find((item) => item.id === vaultId) ?? null;
  const stopped = vault?.lifecycle === "stopping" || vault?.lifecycle === "tombstone";
  // A link such as «Controlla protezione» names its section (`#protection`):
  // scroll to it and highlight it briefly, so the cause is the first thing seen.
  const [highlight, setHighlight] = useState(() => window.location.hash.slice(1));
  useEffect(() => {
    const target = window.location.hash.slice(1);
    const section = target ? document.getElementById(target) : null;
    if (!section?.matches("[data-section]")) { setHighlight(""); return; }
    setHighlight(target);
    section.scrollIntoView?.({ block: "start" });
    const timer = window.setTimeout(() => setHighlight(""), 4000);
    return () => window.clearTimeout(timer);
  }, [vaultId]);
  useEffect(() => {
    let live = true;
    setLoadError("");
    void fetchOperationPolicy(vaultId).then((data) => { if (live) setAuto(data.auto_upload); }).catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);
  async function toggle() {
    if (auto === null) return;
    setBusy(true);
    try {
      const next = (await updateOperationPolicy({ auto_upload: !auto }, vaultId)).auto_upload;
      // A checkbox saves on change and confirms beside itself, not with a toast (#658).
      setAuto(next); flashAutoSaved();
    } catch (error) { show(errorMessage(error, t), true); }
    finally { setBusy(false); }
  }
  return <div className="grid min-w-0 gap-4" data-testid="vault-settings">
    {!stopped && <SettingsSection id="pause" highlighted={highlight === "pause"} title={t("vault_pause.pause")} summary={t("vault_pause.help")}>
      <VaultPauseControl key={vaultId} vaultId={vaultId} paused={vault?.paused} />
    </SettingsSection>}
    <SettingsSection id="protection" highlighted={highlight === "protection"} title={t("settings.protection_title")} summary={t("settings.protection_summary")}
      more={<><p>{t("essential.protection_help")}</p><p>{t("uploads.retry_help")}</p></>}>
      {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
      <div className="grid gap-1">
        <div className="flex flex-wrap items-center gap-x-3">
          <label className="flex min-h-11 items-center gap-3 font-semibold"><input type="checkbox" className="size-5 accent-[var(--green)]" checked={auto ?? false} disabled={auto === null || busy} onChange={() => void toggle()} />{t("folder_settings.policy_auto_upload")}</label>
          <SavedIndicator visible={autoSaved} />
        </div>
        {auto === false && <p className="text-sm text-muted" data-testid="auto-upload-off-hint">{t("settings.auto_off")}</p>}
      </div>
      <UploadRetryActions vaultId={vaultId} showHelp={false} />
    </SettingsSection>
    <SettingsSection id="verification" highlighted={highlight === "verification"} title={t("verification.title")} summary={t("settings.verification_summary")} more={<p>{t("verification.help")}</p>}>
      <UploadVerificationPanel vaultId={vaultId} onNotice={show} />
    </SettingsSection>
    <SettingsSection id="local" highlighted={highlight === "local"} title={t("settings.local_title")} summary={t("settings.local_summary")}
      more={<><p>{t("folder_settings.retention_help")}</p><p>{t("folder_settings.retention_example")}</p><p>{t("folder_settings.retention_never")}</p></>}>
      <RetentionPanel vaultId={vaultId} onNotice={show} />
    </SettingsSection>
    <SettingsSection id="plan" highlighted={highlight === "plan"} title={t("folder_settings.plan_title")} summary={t("folder_settings.plan_help")}>
      <details className="rounded-card border border-line"><summary className="min-h-11 cursor-pointer px-3 py-3 font-semibold">{t("settings.plan_open")}</summary>
        <div className="border-t border-line p-3"><LifecyclePanel vaultId={vaultId} onNotice={show} /></div></details>
    </SettingsSection>
    <SettingsSection id="danger" highlighted={highlight === "danger"} danger title={t("settings.danger_title")} summary={t("settings.danger_summary")}>
      {vault && !stopped && <div className="grid gap-2 border-b border-line pb-4">
        <p className="text-sm">{t("general.reconnect_help")}</p>
        {vault.lifecycle === "active" && <p className="text-sm text-muted">{t("general.reconnect_active_hint")}</p>}
        <p className="break-all text-xs text-muted">{t(vault.lifecycle === "reconnect_required" ? "general.reconnect_old_root" : "general.reconnect_current_root")}: {vault.source_root}</p>
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
  </div>;
}
