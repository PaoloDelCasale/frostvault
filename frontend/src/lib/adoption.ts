import type { StatsResponse } from "@/api/types";

/** Files already in the cloud while the Vault awaits its first confirmation. */
export type Adoption = { inCloud: number; total: number };

const NOT_COUNTED = new Set(["excluded", "unsupported"]);

/**
 * While files are unconfirmed, how many of them are already in the cloud
 * (their last known state is protected) out of those that count (#711).
 */
export function adoptionOf(stats: StatsResponse | undefined): Adoption | null {
  const unconfirmed = Number((stats?.states as Record<string, number> | undefined)?.unconfirmed ?? 0);
  if (!stats || unconfirmed <= 0) return null;
  const known = (stats.last_known_states ?? {}) as Record<string, number>;
  const total = Object.entries(known).filter(([name]) => !NOT_COUNTED.has(name)).reduce((sum, [, count]) => sum + count, 0);
  return { inCloud: Number(known.protected ?? 0), total };
}
