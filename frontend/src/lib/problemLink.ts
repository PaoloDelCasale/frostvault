import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/** Where a problem or notification is remedied: the file, else the Vault settings. */
export function problemLink(vaultId: number | null, params: Record<string, unknown>): string {
  if (!vaultId) return "/system";
  // A Vault Deletion acts on a Tombstone, reachable only from the Overview (#717).
  if (params.vault_deletion) return "/#archived";
  const path = typeof params.path === "string" ? params.path : "";
  if (!path) return vaultSettingsUrl(vaultId);
  return vaultFilesUrl(vaultId, { directory: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "", file: path });
}
