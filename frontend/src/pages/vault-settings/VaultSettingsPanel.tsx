import { useCallback, useEffect, useState } from "react";
import { fetchOperationPolicy, updateOperationPolicy } from "@/api";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { LifecyclePanel } from "./LifecyclePanel";
import { RetentionPanel } from "./RetentionPanel";

export function VaultSettingsPanel({ vaultId, vaultName, onStopped }: { vaultId: number; vaultName: string; onStopped: () => void }) {
  const { t } = useI18n();
  const [auto, setAuto] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [stop, setStop] = useState(false);
  const [notice, setNotice] = useState<{ message: string; error: boolean } | null>(null);
  const show = useCallback((message: string, error = false) => setNotice({ message, error }), []);
  useEffect(() => {
    let live = true;
    void fetchOperationPolicy(vaultId).then((data) => { if (live) setAuto(data.auto_upload); }).catch((error: unknown) => { if (live) show(String(error), true); });
    return () => { live = false; };
  }, [vaultId, show]);
  async function toggle() {
    if (auto === null) return;
    setBusy(true);
    try { setAuto((await updateOperationPolicy({ auto_upload: !auto }, vaultId)).auto_upload); }
    catch (error) { show(String(error), true); }
    finally { setBusy(false); }
  }
  return <section className="grid min-w-0 gap-4 rounded-panel border border-line bg-surface p-4" aria-label={t("essential.settings")}>
    <h2 className="text-lg font-bold">{vaultName} · {t("essential.settings")}</h2>
    <label className="flex min-h-11 items-center gap-3 font-semibold"><input type="checkbox" checked={auto ?? false} disabled={auto === null || busy} onChange={() => void toggle()} />{t("folder_settings.policy_auto_upload")}</label>
    <p className="text-sm text-muted">{t("essential.protection_help")}</p>
    <RetentionPanel vaultId={vaultId} onNotice={show} />
    <details><summary className="min-h-11 cursor-pointer py-3 font-semibold">{t("folder_settings.advanced")}</summary><LifecyclePanel vaultId={vaultId} onNotice={show} /></details>
    <p className="text-sm text-muted">{t("stop_protection.local_retained")}</p>
    <Button className="w-fit" variant="danger" onClick={() => setStop(true)}>{t("stop_protection.title")}</Button>
    <StopProtectionDialog open={stop} onOpenChange={setStop} vaultId={vaultId} vaultName={vaultName} onCompleted={onStopped} />
    <Toast open={notice !== null} message={notice?.message ?? ""} variant={notice?.error ? "error" : "success"} onClose={() => setNotice(null)} />
  </section>;
}
