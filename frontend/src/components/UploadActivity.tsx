import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpFromLine } from "lucide-react";
import { apiRequest } from "@/api/client";
import { Button, buttonVariants } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Popover } from "@/components/Popover";
import type { UploadActivityResponse, UploadActivitySummary } from "@/api/types";
import { formatBytes } from "@/pages/archive/format";
import { CATALOG_REFRESH_MS } from "@/api/useCatalogEvents";
import { vaultFilesUrl } from "@/router";
import { errorMessage } from "@/lib/errorMessage";

type Translate = (key: string, params?: Record<string, string | number>) => string;
const stages = ["stability_wait", "queued", "uploading", "verifying"] as const;
const admitting = (activity: UploadActivitySummary) => (activity.bulk_admission?.requests ?? 0) > 0;
const busy = (activity: UploadActivitySummary) => admitting(activity) || stages.some((stage) => activity[stage].files > 0);

function Summary({ activity, observedAt, t }: { activity: UploadActivitySummary; observedAt: string; t: Translate }) {
  const remaining = activity.next_stable_at ? Date.parse(activity.next_stable_at) - Date.parse(observedAt) : NaN;
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
  const query = useQuery({
    queryKey: ["upload-activity"],
    queryFn: () => apiRequest<UploadActivityResponse>("/api/upload-activity"),
    staleTime: CATALOG_REFRESH_MS,
    refetchInterval: CATALOG_REFRESH_MS,
  });
  const data = query.data;
  if (!data?.global || (!busy(data.global) && !open && !notice)) return null;
  const files = stages.reduce((sum, stage) => sum + data.global[stage].files, 0);
  const bytes = stages.reduce((sum, stage) => sum + data.global[stage].bytes, 0);
  const selected = data.vaults?.find((vault) => vault.vault_id === vaultId && busy(vault));
  const others = data.vaults?.filter((vault) => vault !== selected && busy(vault)) ?? [];
  const label = files > 0 ? t("activity.indicator", { count: files, size: formatBytes(bytes) }) : t("activity.preparing");
  return <>
    <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) setNotice(""); }} align="end" label={t("activity.heading")} className="w-[min(26rem,calc(100vw-1rem))] p-4"
      trigger={<button type="button" data-testid="upload-indicator" aria-label={label} title={label}
        className={buttonVariants({ variant: "secondary", className: "min-w-11 gap-2 px-3" })}>
        <ArrowUpFromLine className="size-4 animate-pulse text-green motion-reduce:animate-none" aria-hidden="true" />
        <span className="hidden tabular-nums lg:inline">{label}</span>
        <span className="tabular-nums lg:hidden" aria-hidden="true">{files || t("activity.preparing")}</span>
      </button>}>
      <section className="grid min-w-0 gap-3" data-testid="upload-activity">
        <h2 className="font-bold">{t("activity.heading")}</h2>
        {query.isError && <p role="status" className="text-sm">{t("activity.unavailable")}</p>}
        {busy(data.global) ? <Summary activity={data.global} observedAt={data.observed_at} t={t} /> : <p className="text-sm">{t("activity.idle")}</p>}
        {selected && <div className="grid gap-2 border-t border-line pt-3" data-testid="selected-vault-activity">
          <h3 className="break-words font-bold">{selected.vault_name}</h3>
          <Summary activity={selected} observedAt={data.observed_at} t={t} />
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
        <p className="text-xs text-muted">{t("activity.current_work")}</p>
      </section>
    </Popover>
    <ConfirmDialog open={confirm} onOpenChange={(next) => { if (!cancelling) setConfirm(next); }} title={t("activity.cancel_pending")} description={t("activity.cancel_pending_confirm")}
      confirmLabel={t("activity.cancel_pending")} cancelLabel={t("ui.cancel")} tone="danger" onConfirm={() => void cancelPending()} />
  </>;
}
