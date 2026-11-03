import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createElement } from "react";

import {
  fetchFileVersions,
  fetchFiles,
  fetchI18nCatalog,
  fetchJobs,
  fetchMe,
  fetchNotifications,
  fetchStats,
  fetchVaults,
} from "./endpoints";
import type { FilesQuery, JobsResponse, StatsResponse } from "./types";
import type { NotificationListStatus } from "./endpoints";
import {
  catalogPollIntervalMs,
  jobAwareRefetchInterval,
} from "./polling";

export const apiQueryKeys = {
  me: ["me"] as const,
  vaults: ["vaults"] as const,
  stats: ["stats"] as const,
  jobs: ["jobs"] as const,
  i18nCatalog: (locale?: string) => ["i18n", "catalog", locale ?? "default"] as const,
  files: (query: FilesQuery) =>
    [
      "files",
      query.q ?? "",
      query.state ?? "",
      typeof query.storage === "string" ? query.storage : "",
      query.directory ?? "",
      query.page ?? 1,
      query.page_size ?? 100,
      typeof query.sort === "string" ? query.sort : "",
      typeof query.order === "string" ? query.order : "",
    ] as const,
  fileVersions: (path: string) => ["file-versions", path] as const,
  /** Prefix shared by every notifications list query (status-scoped children). */
  notifications: ["notifications"] as const,
  notificationsByStatus: (status: NotificationListStatus) =>
    ["notifications", status] as const,
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

/** Stats query: poll only while Jobs are active; idle updates are event-driven. */
export const statsRefetchInterval = (
  query: { state: { data: StatsResponse | undefined } },
): number | false =>
  catalogPollIntervalMs(query.state.data?.active_jobs ?? 0);

export const statsQueryOptions = {
  queryKey: apiQueryKeys.stats,
  queryFn: fetchStats,
  refetchInterval: statsRefetchInterval,
};

export function filesQueryOptions(
  query: FilesQuery,
  options?: RequestInit,
) {
  return {
    queryKey: apiQueryKeys.files(query),
    queryFn: () => fetchFiles(query, options),
  };
}

export function fileVersionsQueryOptions(path: string) {
  return {
    queryKey: apiQueryKeys.fileVersions(path),
    queryFn: () => fetchFileVersions(path),
    enabled: Boolean(path),
  };
}

export function notificationsQueryOptions(
  status: NotificationListStatus = "unread",
  limit = 50,
) {
  return {
    queryKey: apiQueryKeys.notificationsByStatus(status),
    queryFn: () => fetchNotifications({ status, limit }),
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
 * Files list cadence mirrors jobs: 1s while any Job group is active, otherwise
 * no fixed polling. External filesystem discovery is event-driven (#227).
 */
export function filesRefetchIntervalFromJobs(
  jobs: JobsResponse | undefined,
): number | false {
  return catalogPollIntervalMs(countActiveJobGroups(jobs));
}

/** Jobs query keeps a dedicated active/idle cadence for progress only. */
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
