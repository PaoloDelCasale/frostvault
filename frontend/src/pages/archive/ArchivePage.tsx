import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { statsQueryOptions } from "@/api";
import type { StatsResponse } from "@/api/types";
import { Panel } from "@/components/Panel";

import { FilesystemHealthBanner } from "./FilesystemHealthBanner";
import { SafetyFooter } from "./SafetyFooter";
import { StatsSummary } from "./StatsSummary";

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** Placeholder until GET /api/stats resolves — zeros, no fake findings. */
const pendingStats: StatsResponse = {
  states: {},
  storage: { local_bytes: 0, cloud_bytes: 0 },
  active_jobs: 0,
  runtime: {},
  filesystem: null,
  delete_enabled: false,
};

export type ArchivePageProps = {
  /** Vault name — available as an accessible page heading (shell may also show it). */
  vaultName: string;
  displayName: string;
  t: Translate;
  /** Optional slot for the file browser; placeholder until then. */
  fileList?: ReactNode;
};

/**
 * Archive page shell: heading, statistics, filesystem health, file list slot,
 * and safety footer. Stats come from vault-scoped GET /api/stats.
 */
export function ArchivePage({
  vaultName,
  displayName,
  t,
  fileList,
}: ArchivePageProps) {
  const statsQuery = useQuery(statsQueryOptions);
  const stats = statsQuery.data ?? pendingStats;

  return (
    <div className="grid gap-0">
      <header className="mb-3 md:mb-4">
        <p className="text-sm text-muted" data-vault-name={vaultName}>
          {t("ui.archive_subtitle")}
        </p>
      </header>

      <StatsSummary stats={stats} t={t} />
      <FilesystemHealthBanner filesystem={stats.filesystem} t={t} />

      <Panel className="min-w-0">
        <div data-testid="archive-file-list" className="min-w-0 min-h-[12rem] px-4 pb-4">
          {fileList ?? (
            <p className="p-4 text-sm text-muted">{t("ui.file_list_placeholder")}</p>
          )}
        </div>
      </Panel>

      <SafetyFooter displayName={displayName} t={t} />
    </div>
  );
}
