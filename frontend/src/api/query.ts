import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createElement } from "react";

import {
  fetchFileHistory,
  fetchFileVersions,
  fetchFiles,
  fetchI18nCatalog,
  fetchJobs,
  fetchMe,
  fetchNotificationPreferences,
  fetchNotifications,
  fetchRenameCandidates,
  fetchStats,
  fetchVaults,
} from "./endpoints";
import type { FilesQuery, JobsResponse, StatsResponse } from "./types";
import { jobAwareRefetchInterval, jobPollIntervalMs } from "./polling";

export const apiQueryKeys = {
  me: ["me"] as const,
  vaults: ["vaults"] as const,
  stats: ["stats"] as const,
  jobs: ["jobs"] as const,
  renameCandidates: (vaultId: number) => ["rename-candidates", vaultId] as const,
  i18nCatalog: (locale?: string) => ["i18n", "catalog", locale ?? "default"] as const,
  files: (query: FilesQuery) =>
    [
      "files",
      query.q ?? "",
      query.state ?? "",
      query.directory ?? "",
      query.page ?? 1,
      query.page_size ?? 100,
    ] as const,
  fileHistory: (path: string) => ["file-history", path] as const,
  fileVersions: (path: string) => ["file-versions", path] as const,
  notifications: ["notifications"] as const,
  notificationPreferences: (vaultId: number) =>
    ["notification-preferences", vaultId] as const,
};

export function createAppQueryClient(
  config: QueryClientConfig = {},
): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        retry: false,
        refetchOnWindowFocus: false,
      },
    },
    ...config,
  });
}

export function ApiQueryProvider({
  client,
  children,
}: {
  client?: QueryClient;
  children: ReactNode;
}) {
  const queryClient = client ?? createAppQueryClient();
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

export const meQueryOptions = {
  queryKey: apiQueryKeys.me,
  queryFn: fetchMe,
};

export const vaultsQueryOptions = {
  queryKey: apiQueryKeys.vaults,
  queryFn: fetchVaults,
};

export function i18nCatalogQueryOptions(locale?: string) {
  return {
    queryKey: apiQueryKeys.i18nCatalog(locale),
    queryFn: () => fetchI18nCatalog(locale),
  };
}

/** Stats query: 1s while any Job is active, 10s when idle (same cadence as jobs). */
export const statsRefetchInterval = jobAwareRefetchInterval<StatsResponse>(
  (data) => data?.active_jobs ?? 0,
);

export const statsQueryOptions = {
  queryKey: apiQueryKeys.stats,
  queryFn: fetchStats,
  refetchInterval: statsRefetchInterval,
};

export function filesQueryOptions(query: FilesQuery) {
  return {
    queryKey: apiQueryKeys.files(query),
    queryFn: () => fetchFiles(query),
  };
}

export function renameCandidatesQueryOptions(vaultId: number) {
  return {
    queryKey: apiQueryKeys.renameCandidates(vaultId),
    queryFn: fetchRenameCandidates,
  };
}

export function fileHistoryQueryOptions(path: string) {
  return {
    queryKey: apiQueryKeys.fileHistory(path),
    queryFn: () => fetchFileHistory(path),
    enabled: Boolean(path),
  };
}

export function fileVersionsQueryOptions(path: string) {
  return {
    queryKey: apiQueryKeys.fileVersions(path),
    queryFn: () => fetchFileVersions(path),
    enabled: Boolean(path),
  };
}

export const notificationsQueryOptions = {
  queryKey: apiQueryKeys.notifications,
  queryFn: () => fetchNotifications(),
};

export function notificationPreferencesQueryOptions(vaultId: number) {
  return {
    queryKey: apiQueryKeys.notificationPreferences(vaultId),
    queryFn: fetchNotificationPreferences,
    enabled: Number.isSafeInteger(vaultId) && vaultId > 0,
  };
}

/** Count Job groups that are not yet terminal (completed/failed/cancelled). */
export function countActiveJobGroups(data: JobsResponse | undefined): number {
  if (!data?.groups) return 0;
  return data.groups.filter(
    (group) => !["completed", "failed", "cancelled"].includes(group.status),
  ).length;
}

/**
 * Files list cadence mirrors jobs/stats: 1s while any Job group is active,
 * 10s when idle. Driven by the shared jobs query cache, not the files payload.
 */
export function filesRefetchIntervalFromJobs(
  jobs: JobsResponse | undefined,
): number {
  return jobPollIntervalMs(countActiveJobGroups(jobs));
}

/** Jobs query: 1s while any group is active, 10s when idle (app.js cadence). */
export const jobsRefetchInterval = jobAwareRefetchInterval<JobsResponse>(
  countActiveJobGroups,
);

export function jobsQueryOptions() {
  return {
    queryKey: apiQueryKeys.jobs,
    queryFn: fetchJobs,
    refetchInterval: jobsRefetchInterval,
  };
}
