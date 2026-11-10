import { useState } from "react";
import { apiRequest } from "@/api";
import { vaultApi } from "@/api/vault";
import type { RetryFailedUploadsResponse } from "@/api/types";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";

/** No Session-selected Vault fallback: omitted identity explicitly means global. */
export function UploadRetryActions({ vaultId, showHelp = true }: { vaultId?: number; showHelp?: boolean }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState(false);
  async function perform(wholeVault: boolean) {
    setBusy(true); setError(false); setNotice("");
    try {
      if (wholeVault && vaultId !== undefined) {
        await vaultApi(vaultId).post("/api/upload", { path: "", is_directory: true });
        setNotice(t("uploads.whole_started"));
      } else {
        const result = await apiRequest<RetryFailedUploadsResponse>(`/api/upload/retry-failed${vaultId === undefined ? "" : `?vault_id=${vaultId}`}`, { method: "POST" });
        setNotice(t("uploads.retry_result", { count: result.item_count }));
      }
    } catch (reason) { setError(true); setNotice(String(reason)); }
    finally { setBusy(false); }
  }
  return <section className="grid gap-3">
    <div className="flex flex-wrap gap-3">
      <Button variant="secondary" disabled={busy} onClick={() => void perform(false)}>{t("uploads.retry_failed")}</Button>
      {vaultId !== undefined && <Button variant="secondary" disabled={busy} onClick={() => void perform(true)}>{t("uploads.whole_vault")}</Button>}
    </div>
    {showHelp && <p className="text-sm text-muted">{t("uploads.retry_help")}</p>}
    {notice && <p role={error ? "alert" : "status"}>{notice}</p>}
  </section>;
}
