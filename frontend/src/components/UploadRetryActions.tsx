import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/api";
import { vaultApi } from "@/api/vault";
import type { BulkUploadAdmissionResponse, RetryFailedUploadsResponse } from "@/api/types";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errorMessage";

function newRequestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // getRandomValues is also available on plain-HTTP LAN installations, where
  // randomUUID (a secure-context-only API) may be absent.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 15) | 64;
  bytes[8] = ((bytes[8] ?? 0) & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** No Session-selected Vault fallback: omitted identity explicitly means global. */
export function UploadRetryActions({ vaultId, showHelp = true }: { vaultId?: number; showHelp?: boolean }) {
  const { t } = useI18n();
  const client = useQueryClient();
  const pendingRequest = useRef<{ vaultId: number; requestId: string } | null>(null);
  const [accepted, setAccepted] = useState<{ vaultId: number; groupId: string } | null>(null);
  const admission = useQuery({
    queryKey: ["bulk-upload-admission", accepted?.vaultId, accepted?.groupId],
    enabled: accepted !== null && accepted.vaultId === vaultId,
    staleTime: 0,
    queryFn: () => {
      if (!accepted) throw new Error("Missing upload request");
      return vaultApi(accepted.vaultId).request<BulkUploadAdmissionResponse>(`/api/upload/admissions/${accepted.groupId}`);
    },
    refetchInterval: (query) => query.state.data?.status === "pending" ? 2000 : false,
  });
  const currentAdmission = accepted?.vaultId === vaultId ? admission.data : undefined;
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
          pendingRequest.current = { vaultId, requestId: newRequestId() };
        }
        const result = await vaultApi(vaultId).post<{ admission?: BulkUploadAdmissionResponse }>("/api/upload", { path: "", is_directory: true, request_id: pendingRequest.current.requestId });
        if (result.admission) {
          client.setQueryData(["bulk-upload-admission", vaultId, result.admission.group_id], result.admission);
          setAccepted({ vaultId, groupId: result.admission.group_id });
        }
        pendingRequest.current = null;
        setNotice(t("uploads.whole_started"));
        await client.invalidateQueries({ queryKey: ["upload-activity"] });
      } else {
        const result = await apiRequest<RetryFailedUploadsResponse>(`/api/upload/retry-failed${vaultId === undefined ? "" : `?vault_id=${vaultId}`}`, { method: "POST" });
        setNotice(t("uploads.retry_result", { count: result.item_count }));
      }
    } catch (reason) { setError(true); setNotice(errorMessage(reason, t)); }
    finally { setBusy(false); }
  }
  return <section className="grid gap-3">
    <div className="flex flex-wrap gap-3">
      <Button variant="secondary" disabled={busy} onClick={() => void perform(false)}>{t("uploads.retry_failed")}</Button>
      {vaultId !== undefined && <Button variant="secondary" disabled={busy || currentAdmission?.status === "pending"} onClick={() => void perform(true)}>{t("uploads.whole_vault")}</Button>}
    </div>
    {showHelp && <p className="text-sm text-muted">{t("uploads.retry_help")}</p>}
    {notice && <p role={error ? "alert" : "status"}>{notice}</p>}
    {currentAdmission && <p role="status">{t(`uploads.admission.${currentAdmission.status}`, {
      selected: currentAdmission.selected_count, processed: currentAdmission.processed_count, admitted: currentAdmission.admitted_count,
    })}</p>}
    {accepted?.vaultId === vaultId && admission.isError && <p role="alert">{t("activity.unavailable")}</p>}
  </section>;
}
