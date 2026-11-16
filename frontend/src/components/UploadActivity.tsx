import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowUpFromLine } from "lucide-react";
import { apiRequest } from "@/api/client";
import { Button, buttonVariants } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Popover } from "@/components/Popover";
import type { UploadActivitySummary } from "@/api/types";
import { formatBytes } from "@/pages/archive/format";
import { vaultFilesUrl } from "@/router";
import { errorMessage } from "@/lib/errorMessage";
import { uploadBacklog, uploadPace, useUploadActivity } from "@/api/uploadActivity";

type Translate = (key: string, params?: Record<string, string | number>) => string;
const stages = ["stability_wait", "queued", "uploading", "verifying"] as const;
const admitting = (activity: UploadActivitySummary) => (activity.bulk_admission?.requests ?? 0) > 0;
const busy = (activity: UploadActivitySummary) => admitting(activity) || stages.some((stage) => activity[stage].files > 0);

function Summary({ activity, observedAt, t, paused = false }: {
  activity: UploadActivitySummary; observedAt: string; t: Translate; paused?: boolean;
}) {
  const remaining = activity.next_stable_at ? Date.parse(activity.next_stable_at) - Date.parse(observedAt) : NaN;
  const pace = paused ? null : uploadPace(activity, t);
  return <>
    {admitting(activity) && <p className="text-sm" role="status">{t("activity.bulk_admission", {
      processed: activity.bulk_admission?.processed_files ?? 0,
      selected: activity.bulk_admission?.selected_files ?? 0,
      admitted: activity.bulk_admission?.admitted_files ?? 0,
    })}</p>}
    <ul className="grid gap-1.5">
      {stages.filter((stage) => activity[stage].files > 0).map((stage) => <li key={stage} className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className="font-semibold">{t(`activity.${stage}`, { count: activity[stage].files })}</span>
        <span className="text-sm text-muted">{stage === "uploading"
          ? t("activity.sent", { sent: formatBytes(activity[stage].transferred_bytes), total: formatBytes(activity[stage].bytes) })
          : formatBytes(activity[stage].bytes)}</span>
      </li>)}
    </ul>
    {activity.stability_wait.files > 0 && <p className="mt-1 text-xs text-muted">{Number.isFinite(remaining) && remaining > 0
      ? t("activity.stable_in", { minutes: Math.max(1, Math.ceil(remaining / 60_000)) })
      : t("activity.waiting_observation")}</p>}
    {activity.awaiting_admission_files > 0 && <p className="mt-1 text-xs text-muted">{t("activity.admission", { count: activity.awaiting_admission_files })}</p>}
    {paused ? <p className="mt-1 text-sm font-semibold" data-testid="activity-paused">{t("activity.paused")}</p>
      : pace ? <p className="mt-1 text-sm font-semibold" data-testid="activity-rate">{t("activity.rate", pace)}</p>
      : (activity.queued.files > 0 || activity.uploading.files > 0) && <p className="mt-1 text-xs text-muted">{t("activity.rate_unknown")}</p>}
  </>;
}

/**
 * Installation-wide upload activity as a header indicator: it appears only
 * while files are waiting, queued, uploading or verifying. No Job lists.
 */
export function UploadActivity({ t, vaultId }: { t: Translate; vaultId?: number }) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [notice, setNotice] = useState("");
  async function cancelPending() {
    if (cancelling || !vaultId) return;
    setCancelling(true); setNotice("");
    try {
      const result = await apiRequest<{ cancelled_count: number }>(`/api/upload/cancel-pending?vault_id=${vaultId}`, { method: "POST", body: JSON.stringify({ path: "" }) });
      setNotice(t("activity.cancel_pending_done", { count: result.cancelled_count }));
      await client.invalidateQueries({ queryKey: ["upload-activity"] });
    } catch (error) { setNotice(errorMessage(error, t)); }
    finally { setCancelling(false); setConfirm(false); setOpen(true); }
  }
  const query = useUploadActivity();
  const data = query.data;
  // The global totals leave out Paused Vaults (#711): their queue waits for
  // the Owner, so it is listed apart and never in the indicator's count.
  const paused = data?.vaults?.filter((vault) => vault.paused && busy(vault)) ?? [];
  if (!data?.global || (!busy(data.global) && paused.length === 0 && !open && !notice)) return null;
  const files = stages.reduce((sum, stage) => sum + data.global[stage].files, 0);
  const bytes = stages.reduce((sum, stage) => sum + data.global[stage].bytes, 0);
  const selected = data.vaults?.find((vault) => vault.vault_id === vaultId && busy(vault));
  const others = data.vaults?.filter((vault) => vault !== selected && !vault.paused && busy(vault)) ?? [];
  const pausedOthers = paused.filter((vault) => vault !== selected);
  const pausedFiles = paused.reduce((sum, vault) => sum + uploadBacklog(vault).files, 0);
  const pace = uploadPace(data.global, t);
  // Server priority (#713): uploads held back while the server's disks are busy.
  const serverBusy = data.server_priority?.throttled === true;
  const label = files > 0
    ? pace ? t("activity.indicator_rate", { count: files, size: formatBytes(bytes), ...pace })
      : t("activity.indicator", { count: files, size: formatBytes(bytes) })
    : busy(data.global) ? t("activity.preparing") : t("activity.paused_only", { count: pausedFiles });
  return <>
    <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) setNotice(""); }} align="end" label={t("activity.heading")} className="w-[min(26rem,calc(100vw-1rem))] p-4"
      trigger={<button type="button" data-testid="upload-indicator" aria-label={label} title={label}
        className={buttonVariants({ variant: "secondary", className: "min-w-11 gap-2 px-3" })}>
        <ArrowUpFromLine className={files > 0 ? "size-4 animate-pulse text-green motion-reduce:animate-none" : "size-4 text-muted"} aria-hidden="true" />
        <span className="hidden tabular-nums lg:inline">{label}</span>
        {serverBusy && <span className="hidden text-xs font-semibold text-muted lg:inline" data-testid="activity-server-busy-badge">{t("activity.server_busy")}</span>}
        <span className="tabular-nums lg:hidden" aria-hidden="true">{files || (busy(data.global) ? t("activity.preparing") : pausedFiles)}</span>
      </button>}>
      <section className="grid min-w-0 gap-3" data-testid="upload-activity">
        <h2 className="font-bold">{t("activity.heading")}</h2>
        {query.isError && <p role="status" className="text-sm">{t("activity.unavailable")}</p>}
        {serverBusy && <div role="status" className="grid gap-1" data-testid="activity-server-busy">
          <p className="text-sm font-semibold">{t("activity.server_busy")}</p>
          <p className="text-xs text-muted">{t("activity.server_busy_detail")}</p>
        </div>}
        {busy(data.global) ? <Summary activity={data.global} observedAt={data.observed_at} t={t} /> : <p className="text-sm">{t("activity.idle")}</p>}
        {selected && <div className="grid gap-2 border-t border-line pt-3" data-testid="selected-vault-activity">
          <h3 className="break-words font-bold">{selected.vault_name}</h3>
          <Summary activity={selected} observedAt={data.observed_at} t={t} paused={selected.paused} />
          {(selected.queued.files > 0 || admitting(selected)) && <Button variant="secondary" className="w-fit" disabled={cancelling} onClick={() => { setOpen(false); setConfirm(true); }}>{t("activity.cancel_pending")}</Button>}
        </div>}
        {notice && <p className="text-sm" role="status">{notice}</p>}
        {others.length > 0 && <details className="border-t border-line pt-2">
          <summary className="min-h-11 cursor-pointer py-2 font-semibold">{t("activity.by_vault")}</summary>
          <ul className="grid gap-3">{others.map((vault) => <li key={vault.vault_id} className="grid gap-1">
            <a className="break-words font-bold hover:underline" href={vaultFilesUrl(vault.vault_id)} onClick={() => setOpen(false)}>{vault.vault_name}</a>
            <Summary activity={vault} observedAt={data.observed_at} t={t} />
          </li>)}</ul>
        </details>}
        {pausedOthers.length > 0 && <details className="border-t border-line pt-2" data-testid="paused-vaults-activity">
          <summary className="min-h-11 cursor-pointer py-2 font-semibold">{t("activity.paused_heading")}</summary>
          <ul className="grid gap-3">{pausedOthers.map((vault) => <li key={vault.vault_id} className="grid gap-1">
            <a className="break-words font-bold hover:underline" href={vaultFilesUrl(vault.vault_id)} onClick={() => setOpen(false)}>{vault.vault_name}</a>
            <Summary activity={vault} observedAt={data.observed_at} t={t} paused />
          </li>)}</ul>
        </details>}
        <p className="text-xs text-muted">{t("activity.current_work")}</p>
      </section>
    </Popover>
    <ConfirmDialog open={confirm} onOpenChange={(next) => { if (!cancelling) setConfirm(next); }} title={t("activity.cancel_pending")} description={t("activity.cancel_pending_confirm")}
      confirmLabel={t("activity.cancel_pending")} cancelLabel={t("ui.cancel")} tone="danger" onConfirm={() => void cancelPending()} />
  </>;
}
