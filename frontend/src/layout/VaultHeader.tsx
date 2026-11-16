import { useQuery } from "@tanstack/react-query";

import { fetchGeneralVaults } from "@/api";
import { useVaultStats } from "@/api/useVaultStats";
type Params = Record<string, string | number>;
import { ScanStatus } from "@/components/ScanStatus";
import { uploadBacklog, uploadPace, useUploadActivity } from "@/api/uploadActivity";
import { adoptionOf } from "@/lib/adoption";
import { formatBytes, formatNumber } from "@/pages/archive/format";
import { VaultPauseControl } from "@/components/VaultPauseControl";
import { VaultStateBadge } from "@/components/VaultStateBadge";
import { vaultState } from "@/lib/vaultState";
import { cn } from "@/lib/utils";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/** This Vault's upload queue with its pace and time left, or that it is paused (#711). */
function VaultUploadLine({ vaultId, t }: { vaultId: number; t: (key: string, params?: Params) => string }) {
  const activity = useUploadActivity().data?.vaults?.find((vault) => vault.vault_id === vaultId);
  if (!activity) return null;
  const backlog = uploadBacklog(activity);
  if (backlog.files <= 0) return null;
  const pace = activity.paused ? null : uploadPace(activity, t);
  return <p className="pb-2 text-sm text-muted" data-testid="vault-upload-line">
    {t("vault.upload.line", { files: formatNumber(backlog.files), size: formatBytes(backlog.bytes) })}
    {activity.paused ? <> · {t("vault.upload.paused")}</> : pace ? <> · {t("vault.upload.rate", pace)}</> : null}
  </p>;
}

/** Vault title, aggregate state, the pause action and the Files | Settings tabs. */
export function VaultHeader({ vaultId, vaultName, active, t }: {
  vaultId: number; vaultName: string; active: "files" | "settings"; t: (key: string, params?: Params) => string;
}) {
  const stats = useVaultStats(vaultId);
  const general = useQuery({ queryKey: ["general-vaults"], queryFn: fetchGeneralVaults, retry: false, enabled: active === "files" });
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
    {active === "files" && <VaultUploadLine vaultId={vaultId} t={t} />}
    <nav aria-label={t("files.tabs_label")} className="-mb-px flex gap-5">
      {tab("files", vaultFilesUrl(vaultId))}{tab("settings", vaultSettingsUrl(vaultId))}
    </nav>
  </div>;
}
