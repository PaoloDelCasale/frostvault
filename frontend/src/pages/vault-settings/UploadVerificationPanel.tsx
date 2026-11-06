import { useEffect, useState } from "react";
import { fetchOperationPolicy, updateOperationPolicy, type OperationPolicy } from "@/api";
import { useI18n } from "@/i18n";

export function UploadVerificationPanel({ vaultId, onNotice }: { vaultId: number; onNotice: (message: string, error?: boolean) => void }) {
  const { t } = useI18n();
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    setPolicy(null);
    void fetchOperationPolicy(vaultId).then((data) => { if (live) setPolicy(data); })
      .catch((error: unknown) => { if (live) onNotice(String(error), true); });
    return () => { live = false; };
  }, [vaultId, onNotice]);
  async function change(value: string) {
    setBusy(true);
    try {
      setPolicy(await updateOperationPolicy({ upload_verification: value === "inherit" ? null : value === "read_back" ? "read_back" : "s3_checksum" }, vaultId));
    } catch (error) { onNotice(String(error), true); }
    finally { setBusy(false); }
  }
  return <section className="grid gap-2">
    <label className="grid gap-2 font-semibold">{t("verification.title")}
      <select className="min-h-11 rounded border border-line bg-surface px-3" disabled={!policy || busy} value={policy?.upload_verification ?? "inherit"} onChange={(event) => void change(event.target.value)}>
        <option value="inherit">{t("verification.inherit")}</option>
        <option value="s3_checksum">{t("verification.s3_checksum")}</option>
        <option value="read_back">{t("verification.read_back")}</option>
      </select>
    </label>
    <p className="text-sm text-muted">{t("verification.global")}: {policy?.global_upload_verification ? t(`verification.${policy.global_upload_verification}`) : "—"}</p>
    <p className="text-sm text-muted">{t("verification.effective")}: {policy?.effective_upload_verification ? t(`verification.${policy.effective_upload_verification}`) : "—"}</p>
    <p className="text-sm text-muted">{t("verification.help")}</p>
  </section>;
}
