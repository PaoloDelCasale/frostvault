import { useVaultStats } from "@/api/useVaultStats";
import { VaultStateBadge } from "@/components/VaultStateBadge";
import { vaultState } from "@/lib/vaultState";
import { cn } from "@/lib/utils";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/** Vault title, aggregate state and the Files | Settings tabs. */
export function VaultHeader({ vaultId, vaultName, active, t }: {
  vaultId: number; vaultName: string; active: "files" | "settings"; t: (key: string) => string;
}) {
  const stats = useVaultStats(vaultId);
  const tab = (name: "files" | "settings", href: string) => <a href={href} aria-current={active === name ? "page" : undefined}
    className={cn("inline-flex min-h-11 items-center border-b-2 px-1 font-semibold no-underline",
      active === name ? "border-green text-ink" : "border-transparent text-muted hover:border-line hover:text-ink")}>{t(`nav.${name}`)}</a>;
  return <div className="grid min-w-0 gap-2 border-b border-line" data-testid="vault-header">
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
      <h1 className="min-w-0 break-words text-2xl font-bold">{vaultName}</h1>
      {stats.data && <VaultStateBadge state={vaultState(stats.data)} t={t} />}
    </div>
    <nav aria-label={t("files.tabs_label")} className="-mb-px flex gap-5">
      {tab("files", vaultFilesUrl(vaultId))}{tab("settings", vaultSettingsUrl(vaultId))}
    </nav>
  </div>;
}
