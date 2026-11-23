import { useQuery } from "@tanstack/react-query";

import { apiQueryKeys } from "./query";
import { vaultApi } from "./vault";

const STATS_REFRESH_MS = 60_000;
/** While a scan runs its verification progress moves: refresh more often (#691). */
const SCANNING_REFRESH_MS = 10_000;

/** Shared with the Overview table, so both read one cached request per Vault. */
export function useVaultStats(vaultId: number) {
  return useQuery({
    queryKey: apiQueryKeys.vaultStats(vaultId),
    queryFn: () => vaultApi(vaultId).stats(),
    staleTime: STATS_REFRESH_MS,
    refetchInterval: (query) => (query.state.data?.runtime?.scanning ? SCANNING_REFRESH_MS : STATS_REFRESH_MS),
  });
}
