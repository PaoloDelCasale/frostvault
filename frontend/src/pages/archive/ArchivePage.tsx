import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { statsQueryOptions } from "@/api";
import { Panel } from "@/components/Panel";

import { FilesystemHealthBanner } from "./FilesystemHealthBanner";
import { SafetyFooter } from "./SafetyFooter";
import { StatsSummary, type StatsSummaryStatus } from "./StatsSummary";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type ArchivePageProps = {
  /** Vault name — available as an accessible page heading (shell may also show it). */
  vaultName: string;
  displayName: string;
  t: Translate;
  /** Optional slot for the file browser; placeholder until then. */
  fileList?: ReactNode;
};

function statsSummaryStatus(query: {
  data: unknown;
  isPending: boolean;
  isError: boolean;
  isFetching: boolean;
}): StatsSummaryStatus {
  if (query.data != null) return "ready";
  if (query.isError) return "error";
  if (query.isPending || query.isFetching) return "loading";
  return "loading";
}

/**
 * Archive page shell: heading, statistics, filesystem health, file list slot,
 * and safety footer. Stats come from vault-scoped GET /api/stats.
 *
 * Summary metrics and filesystem health fail independently: a missing or
 * checking health synopsis must not block or fake summary cards (#228).
 */
export function ArchivePage({
  vaultName,
  displayName,
  t,
  fileList,
}: ArchivePageProps) {
  const statsQuery = useQuery(statsQueryOptions);
  // Keep prior values during background refetch; never invent zero placeholders.
  const stats = statsQuery.data;
  const summaryStatus = statsSummaryStatus(statsQuery);

  return (
    <div className="grid gap-0">
      <header className="mb-3 md:mb-4">
        <p className="text-sm text-muted" data-vault-name={vaultName}>
          {t("ui.archive_subtitle")}
        </p>
      </header>

      <StatsSummary stats={stats} status={summaryStatus} t={t} />
      <FilesystemHealthBanner filesystem={stats?.filesystem} t={t} />

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
