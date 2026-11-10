/** Aggregate Vault state from `/api/stats`, worst state first. */
export function vaultState(stats: { protection_suspended?: boolean; protection_state?: string | null } | undefined): string {
  return !stats ? "unknown" : stats.protection_suspended ? "suspended" : stats.protection_state ?? "empty";
}
