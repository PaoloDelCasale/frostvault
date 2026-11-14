/** Aggregate Vault state from `/api/stats`, worst state first.
 * Manual Upload is the Owner's choice, not a state: the tone follows the files (#647). */
export function vaultState(stats: { protection_state?: string | null } | undefined): string {
  return !stats ? "unknown" : stats.protection_state ?? "empty";
}
