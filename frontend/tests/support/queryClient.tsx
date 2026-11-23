import type { QueryClient, QueryClientConfig } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactNode } from "react";

import { ApiQueryProvider, createAppQueryClient } from "@/api";

/**
 * The QueryClient of a test: the app's own defaults, so a test sees the cache
 * behavior users get. A `config` replaces those defaults as a whole, exactly
 * as it does for `createAppQueryClient`.
 */
export function createTestQueryClient(config?: QueryClientConfig): QueryClient {
  return createAppQueryClient(config);
}

/**
 * Render `ui` under the query provider. Pass `client` when the test reads or
 * seeds the cache itself; otherwise each call gets a fresh client built from
 * `config`.
 */
export function renderWithQueryClient(
  ui: ReactNode,
  { client, config }: { client?: QueryClient; config?: QueryClientConfig } = {},
): RenderResult {
  return render(
    <ApiQueryProvider client={client ?? createTestQueryClient(config)}>
      {ui}
    </ApiQueryProvider>,
  );
}
