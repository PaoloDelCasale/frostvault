import { useEffect, useState } from "react";
import { acknowledgeRecoveryKit, downloadRecoveryKit, fetchRecoveryKitStatus, type RecoveryKitStatus } from "@/api";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export function RecoveryKitSection() {
  const { t } = useI18n();
  const [status, setStatus] = useState<RecoveryKitStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void fetchRecoveryKitStatus().then((data) => { if (!cancelled) setStatus(data); })
      .catch(() => { if (!cancelled) setError("recovery_kit.error"); });
    return () => { cancelled = true; };
  }, []);

  async function download() {
    setBusy(true); setError("");
    try {
      const artifact = await downloadRecoveryKit();
      const url = URL.createObjectURL(artifact.blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = url; anchor.download = artifact.filename;
        document.body.appendChild(anchor); anchor.click(); anchor.remove();
      } finally { URL.revokeObjectURL(url); }
      setStatus(await fetchRecoveryKitStatus());
    } catch { setError("recovery_kit.error"); }
    finally { setBusy(false); }
  }
  async function acknowledge() {
    if (!status?.fingerprint) return;
    setBusy(true); setError("");
    try { setStatus(await acknowledgeRecoveryKit(status.fingerprint)); }
    catch { setError("recovery_kit.changed"); }
    finally { setBusy(false); }
  }
  return (
    <Panel className="mb-6 grid gap-3 p-5">
      <h2 className="text-lg font-bold">{t("recovery_kit.title")}</h2>
      <p>{t("recovery_kit.help")}</p>
      {status?.configured ? <>
        <p>{t("recovery_kit.fingerprint")}: <code>{status.fingerprint}</code></p>
        <Button disabled={busy} onClick={() => void download()}>{t("recovery_kit.download")}</Button>
        {status.custody_acknowledged
          ? <p role="status">{t("recovery_kit.acknowledged")}</p>
          : <Button disabled={busy} variant="secondary" onClick={() => void acknowledge()}>{t("recovery_kit.acknowledge")}</Button>}
      </> : status ? <p role="alert">{t("recovery_kit.unconfigured")}</p> : null}
      {status?.metadata_restore ? <p role="alert" data-testid="metadata-restore-warning">
        {t("recovery_kit.restore_warning", { date: status.metadata_restore.backup_created_at, count: status.metadata_restore.updated_files })}
      </p> : null}
      {error ? <p role="alert">{t(error)}</p> : null}
    </Panel>
  );
}
