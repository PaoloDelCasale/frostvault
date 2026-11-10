import { useQuery } from "@tanstack/react-query";

import type { StatsResponse } from "@/api/types";
import { vaultApi } from "@/api/vault";

const STATS_REFRESH_MS = 60_000;

/** Shared with the Overview table, so both read one cached request per Vault. */
export function useVaultStats(vaultId: number) {
  return useQuery({
    queryKey: ["vault-stats", vaultId],
    queryFn: () => vaultApi(vaultId).request<StatsResponse>("/api/stats"),
    staleTime: STATS_REFRESH_MS, refetchInterval: STATS_REFRESH_MS,
  });
}
