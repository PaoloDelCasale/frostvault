import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/api";
import { vaultApi } from "@/api/vault";
import type { RetryFailedUploadsResponse } from "@/api/types";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";

/** No Session-selected Vault fallback: omitted identity explicitly means global. */
export function UploadRetryActions({ vaultId, showHelp = true }: { vaultId?: number; showHelp?: boolean }) {
  const { t } = useI18n();
  const client = useQueryClient();
  const pendingRequest = useRef<{ vaultId: number; requestId: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState(false);
  async function perform(wholeVault: boolean) {
    setBusy(true); setError(false); setNotice("");
    try {
      if (wholeVault && vaultId !== undefined) {
        // Keep the key after an uncertain HTTP outcome so retry cannot start a
        // second producer. A successful response allows a new explicit request.
        if (pendingRequest.current?.vaultId !== vaultId) {
          pendingRequest.current = { vaultId, requestId: crypto.randomUUID() };
        }
        await vaultApi(vaultId).post("/api/upload", { path: "", is_directory: true, request_id: pendingRequest.current.requestId });
        pendingRequest.current = null;
        setNotice(t("uploads.whole_started"));
        await client.invalidateQueries({ queryKey: ["upload-activity"] });
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
