import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { CloudDeletionPreview, CloudDeletionSettings, FileVersionsResponse } from "@/api/types";
import { vaultApi } from "@/api/vault";
import { Dialog } from "@/components/Dialog";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { CloudPurgeDialog } from "./CloudPurgeDialog";
import type { Target } from "./RecoverSelection";

export function FileDetails({ target, t, onClose, onRecover, onRefresh }: { target: Target; t: (key: string, params?: Record<string, string | number>) => string; onClose: () => void; onRecover: () => void; onRefresh: () => void }) {
  const api = vaultApi(target.vaultId);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [replace, setReplace] = useState(false);
  const [deletion, setDeletion] = useState<{ settings: CloudDeletionSettings; preview: CloudDeletionPreview } | null>(null);
  const data = useQuery({ queryKey: ["details", target.vaultId, target.item.path, revision], queryFn: async () => {
    const [item, versions, jobs] = await Promise.all([api.details(target.item.path), api.versions(target.item.path), api.jobs()]);
    return { item, versions, jobs };
  } });
  const item = data.data?.item;
  const version = (data.data?.versions as FileVersionsResponse | undefined)?.items[0];
  async function perform(fn: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError("");
    try { await fn(); setRevision((value) => value + 1); onRefresh(); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }
  const pending = data.data?.jobs.groups?.filter((job) => job.path === target.item.path && job.action === "cloud-purge" && job.status === "pending_delay") ?? [];
  return <>
    <Dialog open={!deletion && !replace} onOpenChange={(open) => { if (!open && !busy) onClose(); }} title={`${t("essential.details")} · ${target.vaultName} / ${target.item.path}`} description={undefined}>
      <div className="grid min-w-0 gap-4 break-words">
        {data.isPending && <p role="status">{t("ui.loading")}</p>}{data.error && <p role="alert">{String(data.error)}</p>}
        {item && <>
          <p>{t("essential.protection")}: {t(`state.${item.state}`)}</p><p>{t("essential.copy_location", { local: t(item.local_exists ? "essential.on_server" : "essential.not_on_server"), cloud: t(item.cloud_exists ? "essential.in_cloud" : "essential.not_in_cloud") })}</p>
          <dl className="grid gap-2"><div><dt>{t("essential.last_verified")}</dt><dd>{version?.verified_at ? String(version.verified_at) : "—"}</dd></div><div><dt>{t("essential.cloud_class")}</dt><dd>{version?.storage_class ?? item.storage_class ?? "—"}</dd></div><div><dt>VersionId</dt><dd className="break-all">{version?.provider_version_id ? String(version.provider_version_id) : "—"}</dd></div><div><dt>{t("essential.integrity")}</dt><dd>{version?.integrity ?? item.integrity ?? "—"}</dd></div></dl>
          {item.state === "problem" && <section className="grid gap-3 rounded border border-line p-3"><h2 className="font-bold">{t("essential.open_problem")}</h2><p>{t(item.cloud_only_conflict ? "essential.conflict_help" : "essential.problem_help")}</p>
            {item.cloud_only_conflict ? <Button disabled={busy} variant="danger" onClick={() => setReplace(true)}>{t("essential.replace_cloud")}</Button> : item.upload_eligible ? <Button disabled={busy} onClick={() => void perform(() => api.post("/api/upload", { path: item.path }))}>{t("essential.retry")}</Button> : null}
          </section>}
          {item.recover_eligible && <Button disabled={busy} onClick={onRecover}>{t("ui.row_action_recover")}</Button>}
          {pending.map((job) => <div key={job.id}><p>{t("essential.deletion_pending")}</p><Button disabled={busy} variant="secondary" onClick={() => void perform(() => api.post("/api/jobs/cancel", { group_id: job.id, action: "cloud-purge" }))}>{t("essential.cancel_deletion")}</Button></div>)}
          {item.type === "file" && item.state === "cloud_only" && !item.local_exists && !pending.length && <details><summary className="min-h-11 cursor-pointer py-3 font-semibold">{t("essential.danger_zone")}</summary><p>{t("ui.cloud_purge_local_note")}</p><Button disabled={busy} variant="danger" onClick={() => void perform(async () => {
            const [settings, preview] = await Promise.all([api.request<CloudDeletionSettings>("/api/cloud-deletion"), api.post<CloudDeletionPreview>("/api/cloud-deletion/preview", { path: item.path })]); setDeletion({ settings, preview });
          })}>{t("ui.cloud_purge_title")}</Button></details>}
        </>}
        {error && <p role="alert">{error}</p>}
      </div>
    </Dialog>
    <ConfirmDialog open={replace} onOpenChange={setReplace} title={t("essential.replace_cloud")} description={t("essential.replace_warning")} confirmLabel={t("essential.replace_cloud")} cancelLabel={t("ui.cancel")} tone="danger" onConfirm={() => { setReplace(false); void perform(() => api.post("/api/files/cloud-only-conflict/replace", { path: target.item.path })); }} />
    <CloudPurgeDialog open={Boolean(deletion)} onOpenChange={(open) => { if (!open && !busy) setDeletion(null); }} path={target.item.path} vaultName={target.vaultName} settings={deletion?.settings ?? null} preview={deletion?.preview ?? null} t={t} onConfirm={(payload) => { if (!busy) void perform(async () => { await api.post("/api/cloud-purge", { path: target.item.path, ...payload }); setDeletion(null); }); }} />
  </>;
}
