import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/api/client";
import type { UploadActivityResponse, UploadActivitySummary } from "@/api/types";
import { formatBytes } from "@/pages/archive/format";
import { CATALOG_REFRESH_MS } from "@/api/useCatalogEvents";

type Translate = (key: string, params?: Record<string, string | number>) => string;
const stages = ["stability_wait", "queued", "uploading", "verifying"] as const;

function Summary({ activity, observedAt, t }: { activity: UploadActivitySummary; observedAt: string; t: Translate }) {
  const active = stages.some((stage) => activity[stage].files > 0);
  const remaining = activity.next_stable_at ? Date.parse(activity.next_stable_at) - Date.parse(observedAt) : NaN;
  return <>
    {!active ? <p>{t("activity.idle")}</p> : <ul className="flex flex-wrap gap-x-6 gap-y-2">
      {stages.filter((stage) => activity[stage].files > 0).map((stage) => <li key={stage}>
        <span className="font-semibold">{t(`activity.${stage}`, { count: activity[stage].files })}</span>
        <span className="block text-sm text-muted">{stage === "uploading"
          ? t("activity.sent", { sent: formatBytes(activity[stage].transferred_bytes), total: formatBytes(activity[stage].bytes) })
          : formatBytes(activity[stage].bytes)}</span>
      </li>)}
    </ul>}
    {activity.stability_wait.files > 0 && <p className="mt-2 text-sm">{Number.isFinite(remaining) && remaining > 0
      ? t("activity.stable_in", { minutes: Math.max(1, Math.ceil(remaining / 60_000)) })
      : t("activity.waiting_observation")}</p>}
    {activity.awaiting_admission_files > 0 && <p className="mt-2 text-sm">{t("activity.admission", { count: activity.awaiting_admission_files })}</p>}
  </>;
}

/** Installation scope: kept in the shell on every authenticated page. No Job lists. */
export function UploadActivity({ t, vaultId }: { t: Translate; vaultId?: number }) {
  const query = useQuery({
    queryKey: ["upload-activity"],
    queryFn: () => apiRequest<UploadActivityResponse>("/api/upload-activity"),
    staleTime: CATALOG_REFRESH_MS,
    refetchInterval: CATALOG_REFRESH_MS,
  });
  const data = query.data;
  const selected = data?.vaults?.find((vault) => vault.vault_id === vaultId && stages.some((stage) => vault[stage].files > 0));
  const otherVaults = data?.vaults?.filter((vault) => vault !== selected && stages.some((stage) => vault[stage].files > 0)) ?? [];
  return <section aria-label={t("activity.heading")} className="mb-4 min-w-0 rounded-panel border border-line bg-surface p-4" data-testid="upload-activity">
    <h2 className="font-bold">{t("activity.heading")}</h2>
    {query.isError && <p role="status">{t("activity.unavailable")}</p>}
    {!data?.global ? !query.isError && <p>{t("ui.loading")}</p> : <>
      <Summary activity={data.global} observedAt={data.observed_at} t={t} />
      {selected && <div className="mt-3 border-t border-line pt-2" data-testid="selected-vault-activity">
        <h3 className="break-words font-bold">{selected.vault_name}</h3>
        <Summary activity={selected} observedAt={data.observed_at} t={t} />
      </div>}
      {otherVaults.length > 0 && <details className="mt-3">
        <summary className="min-h-11 cursor-pointer py-2 font-semibold">{t("activity.by_vault")}</summary>
        <ul className="grid gap-3">{otherVaults.map((vault) => <li key={vault.vault_id} className="border-t border-line pt-2">
          <a className="break-words font-bold underline" href={`/?vault_id=${vault.vault_id}`}>{vault.vault_name}</a>
          <Summary activity={vault} observedAt={data.observed_at} t={t} />
        </li>)}</ul>
      </details>}
      {stages.some((stage) => data.global[stage].files > 0) && <p className="mt-2 text-xs text-muted">{t("activity.current_work")}</p>}
    </>}
  </section>;
}
