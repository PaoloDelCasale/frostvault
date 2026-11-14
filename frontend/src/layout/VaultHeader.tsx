import { useQuery } from "@tanstack/react-query";

import { fetchGeneralVaults } from "@/api";
import { useVaultStats } from "@/api/useVaultStats";
import { VaultPauseControl } from "@/components/VaultPauseControl";
import { VaultStateBadge } from "@/components/VaultStateBadge";
import { vaultState } from "@/lib/vaultState";
import { cn } from "@/lib/utils";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/** Vault title, aggregate state, the pause action and the Files | Settings tabs. */
export function VaultHeader({ vaultId, vaultName, active, t }: {
  vaultId: number; vaultName: string; active: "files" | "settings"; t: (key: string) => string;
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
    <nav aria-label={t("files.tabs_label")} className="-mb-px flex gap-5">
      {tab("files", vaultFilesUrl(vaultId))}{tab("settings", vaultSettingsUrl(vaultId))}
    </nav>
  </div>;
}
