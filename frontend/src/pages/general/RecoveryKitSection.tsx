import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiQueryKeys, readOnMount, acknowledgeRecoveryKit, downloadRecoveryKit, fetchRecoveryKitStatus, type RecoveryKitStatus } from "@/api";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { formatDateTime } from "@/lib/format";
import { useI18n } from "@/i18n/useI18n";

export function RecoveryKitSection() {
  const { t } = useI18n();
  const [status, setStatus] = useState<RecoveryKitStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Cloud-first reconnect is asynchronous; keep the durable warning's count
  // current without downloading secret material or per-file notifications.
  const loaded = useQuery({ queryKey: apiQueryKeys.recoveryKit, queryFn: fetchRecoveryKitStatus, ...readOnMount, refetchInterval: 30_000, refetchIntervalInBackground: true });
  useEffect(() => { if (loaded.data) setStatus(loaded.data); }, [loaded.data, loaded.dataUpdatedAt]);
  useEffect(() => { if (loaded.error) setError("recovery_kit.error"); }, [loaded.error, loaded.errorUpdatedAt]);

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
    <section aria-labelledby="system-kit-heading" className="grid gap-4">
      <div>
        <h2 id="system-kit-heading" className="text-xl font-bold">{t("recovery_kit.title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("recovery_kit.help")}</p>
      </div>
      <Panel className="grid gap-3 p-5">
        {status?.configured ? <>
          <p className="break-all">{t("recovery_kit.fingerprint")}: <code>{status.fingerprint}</code></p>
          {status.system_bucket ? <p className="break-all" data-testid="recovery-kit-system-bucket">
            {t("recovery_kit.system_bucket", { name: status.system_bucket.name, region: status.system_bucket.region })}
          </p> : null}
          <Button className="w-fit" disabled={busy} onClick={() => void download()}>{t("recovery_kit.download")}</Button>
          {status.custody_acknowledged
            ? <p role="status">{t("recovery_kit.acknowledged")}</p>
            : <Button className="w-fit" disabled={busy} variant="secondary" onClick={() => void acknowledge()}>{t("recovery_kit.acknowledge")}</Button>}
        </> : status?.awaiting_backup && status.system_bucket
          ? <p role="alert" data-testid="recovery-kit-awaiting-backup">
            {t("recovery_kit.awaiting_backup", { name: status.system_bucket.name, region: status.system_bucket.region })}
          </p>
          : status ? <p role="alert">{t("recovery_kit.unconfigured")}</p> : null}
        {status?.metadata_restore ? <p role="alert" data-testid="metadata-restore-warning">
          {t("recovery_kit.restore_warning", { date: formatDateTime(status.metadata_restore.backup_created_at), count: status.metadata_restore.updated_files })}
        </p> : null}
        {error ? <p role="alert">{t(error)}</p> : null}
      </Panel>
    </section>
  );
}
