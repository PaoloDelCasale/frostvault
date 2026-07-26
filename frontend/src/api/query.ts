import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createElement } from "react";

import { fetchI18nCatalog, fetchMe, fetchVaults } from "./endpoints";
import { jobAwareRefetchInterval } from "./polling";

export const apiQueryKeys = {
  me: ["me"] as const,
  vaults: ["vaults"] as const,
  i18nCatalog: (locale?: string) => ["i18n", "catalog", locale ?? "default"] as const,
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

/** Example jobs query refetch interval: 1s while active, 10s when idle. */
export const jobsRefetchInterval = jobAwareRefetchInterval<{
  active_count?: number;
  items?: unknown[];
}>((data) => {
  if (!data) return 0;
  if (typeof data.active_count === "number") return data.active_count;
  return Array.isArray(data.items) ? data.items.length : 0;
});
