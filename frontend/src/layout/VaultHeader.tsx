import { useQuery } from "@tanstack/react-query";

import { apiQueryKeys, fetchGeneralVaults, uploadBacklog, useUploadActivity, useVaultStats, type StatsResponse } from "@/api";
type Params = Record<string, string | number>;
import { ScanStatus } from "@/components/ScanStatus";
import { SpeedSparkline } from "@/components/SpeedSparkline";
import { expectedFinishText, phaseSpeeds, stoppedText } from "@/lib/throughput";
import { adoptionOf } from "@/lib/adoption";
import { formatBytes, formatNumber } from "@/pages/archive/format";
import { VaultPauseControl } from "@/components/VaultPauseControl";
import { VaultStateBadge } from "@/components/VaultStateBadge";
import { vaultState } from "@/lib/vaultState";
import { cn } from "@/lib/utils";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/**
 * This Vault's one expected finish and the phase that limits it, then each
 * phase's measured speeds (#755): the upload to the cloud and, while a scan
 * verifies fingerprints, the reading from the disk.
 */
function VaultProgress({ vaultId, runtime, t }: {
  vaultId: number; runtime: StatsResponse["runtime"] | undefined; t: (key: string, params?: Params) => string;
}) {
  const activity = useUploadActivity().data?.vaults?.find((vault) => vault.vault_id === vaultId);
  if (!activity) return null;
  const backlog = uploadBacklog(activity);
  const reading = runtime?.scanning === true && runtime.scan_phase === "verification";
  if (backlog.files <= 0 && !reading) return null;
  const finish = activity.paused ? null : activity.expected_finish;
  // Nothing moved for five minutes: no finish, since when it stopped (#759).
  const stopped = activity.paused ? null : activity.stopped_since;
  const upload = activity.paused ? null : phaseSpeeds("upload", activity.throughput, t);
  const verification = phaseSpeeds("verification", activity.throughput, t);
  return <div className="grid gap-1 pb-2 text-sm" data-testid="vault-progress">
    {stopped ? <p className="font-semibold" data-testid="vault-stopped">{stoppedText(stopped, t)}</p>
      : finish && <p className="font-semibold" data-testid="vault-expected-finish">{expectedFinishText(finish, t)}</p>}
    {backlog.files > 0 && <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <p className="text-muted" data-testid="vault-upload-line">
        <span className="font-semibold text-ink">{t("estimate.upload_label")}</span>: {t("estimate.upload_backlog", { files: formatNumber(backlog.files), size: formatBytes(backlog.bytes) })}
        {activity.paused ? <> · {t("vault.upload.paused")}</> : <> · {upload ?? t("estimate.speed_unknown")}</>}
      </p>
      {!activity.paused && <SpeedSparkline throughput={activity.throughput} phase="upload" t={t} />}
    </div>}
    {reading && <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <p className="text-muted" data-testid="vault-reading-line">
        <span className="font-semibold text-ink">{t("estimate.verification_label")}</span>: {verification ?? t("estimate.speed_unknown")}
      </p>
      <SpeedSparkline throughput={activity.throughput} phase="verification" t={t} />
    </div>}
  </div>;
}

/** Vault title, aggregate state, the pause action and the Files | Settings tabs. */
export function VaultHeader({ vaultId, vaultName, active, t }: {
  vaultId: number; vaultName: string; active: "files" | "settings"; t: (key: string, params?: Params) => string;
}) {
  const stats = useVaultStats(vaultId);
  const general = useQuery({ queryKey: apiQueryKeys.generalVaults, queryFn: fetchGeneralVaults, retry: false, enabled: active === "files" });
  const vault = general.data?.items?.find((item) => item.id === vaultId);
  // Settings has its own pause section; an archived Vault has no cloud work to pause.
  const pause = active === "files" && vault && vault.lifecycle !== "stopping" && vault.lifecycle !== "tombstone";
  const tab = (name: "files" | "settings", href: string) => <a href={href} aria-current={active === name ? "page" : undefined}
    className={cn("inline-flex min-h-11 items-center border-b-2 px-1 font-semibold no-underline",
      active === name ? "border-green text-ink" : "border-transparent text-muted hover:border-line hover:text-ink")}>{t(`nav.${name}`)}</a>;
  return <div className="grid min-w-0 gap-2 border-b border-line" data-testid="vault-header">
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
      <h1 className="min-w-0 break-words text-2xl font-bold">{vaultName}</h1>
      {stats.data && <VaultStateBadge state={vaultState(stats.data)} manual={stats.data.manual_upload} t={t} />}
      {pause && <div className="ml-auto"><VaultPauseControl vaultId={vaultId} paused={vault.paused} /></div>}
    </div>
    <ScanStatus runtime={stats.data?.runtime} t={t} adoption={adoptionOf(stats.data)}
      unconfirmed={Number((stats.data?.states as Record<string, number> | undefined)?.unconfirmed ?? 0) > 0} />
    {active === "files" && <VaultProgress vaultId={vaultId} runtime={stats.data?.runtime} t={t} />}
    <nav aria-label={t("files.tabs_label")} className="-mb-px flex gap-5">
      {tab("files", vaultFilesUrl(vaultId))}{tab("settings", vaultSettingsUrl(vaultId))}
    </nav>
  </div>;
}
