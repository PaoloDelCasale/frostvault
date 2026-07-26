import type { ReactNode } from "react";

import type { StatsResponse } from "@/api/types";
import { Panel } from "@/components/Panel";

import { FilesystemHealthBanner } from "./FilesystemHealthBanner";
import { SafetyFooter } from "./SafetyFooter";
import { StatsSummary } from "./StatsSummary";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type ArchivePageProps = {
  /** Vault name — available as an accessible page heading (shell may also show it). */
  vaultName: string;
  displayName: string;
  stats: StatsResponse;
  t: Translate;
  /** Optional slot for the file browser (#66); placeholder until then. */
  fileList?: ReactNode;
};

/**
 * Archive page shell: heading, statistics, filesystem health, file list slot,
 * and safety footer. File list operations arrive in later issues.
 */
export function ArchivePage({
  vaultName,
  displayName,
  stats,
  t,
  fileList,
}: ArchivePageProps) {
  return (
    <div className="grid gap-0">
      <header className="mb-3 md:mb-4">
        <p className="text-sm text-muted" data-vault-name={vaultName}>
          {t("ui.archive_subtitle")}
        </p>
      </header>

      <StatsSummary stats={stats} t={t} />
      <FilesystemHealthBanner filesystem={stats.filesystem} t={t} />

      <Panel>
        <div data-testid="archive-file-list" className="min-h-[12rem] p-4">
          {fileList ?? (
            <p className="text-sm text-muted">{t("ui.file_list_placeholder")}</p>
          )}
        </div>
      </Panel>

      <SafetyFooter displayName={displayName} t={t} />
    </div>
  );
}
