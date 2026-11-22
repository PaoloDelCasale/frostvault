import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createElement } from "react";

import { fetchI18nCatalog, fetchNotifications } from "./endpoints";
import type { FilesQuery } from "./types";
import type { NotificationListStatus } from "./endpoints";

export const apiQueryKeys = {
  me: ["me"] as const,
  vaults: ["vaults"] as const,
  jobs: ["jobs"] as const,
  /** Per-Vault stats, shared by the Vault pages, Overview and catalog events. */
  vaultStats: (vaultId: number) => ["vault-stats", vaultId] as const,
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

export function i18nCatalogQueryOptions(locale?: string) {
  return {
    queryKey: apiQueryKeys.i18nCatalog(locale),
    queryFn: () => fetchI18nCatalog(locale),
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
