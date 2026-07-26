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
  fetchStats,
  fetchVaults,
} from "./endpoints";
import type { FilesQuery, JobsResponse } from "./types";
import { jobAwareRefetchInterval } from "./polling";

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
      query.directory ?? "",
      query.page ?? 1,
      query.page_size ?? 100,
    ] as const,
  fileHistory: (path: string) => ["file-history", path] as const,
  fileVersions: (path: string) => ["file-versions", path] as const,
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

export const statsQueryOptions = {
  queryKey: apiQueryKeys.stats,
  queryFn: fetchStats,
};

export function filesQueryOptions(query: FilesQuery) {
  return {
    queryKey: apiQueryKeys.files(query),
    queryFn: () => fetchFiles(query),
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

function countActiveJobGroups(data: JobsResponse | undefined): number {
  if (!data?.groups) return 0;
  return data.groups.filter(
    (group) => !["completed", "failed", "cancelled"].includes(group.status),
  ).length;
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
