import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/** Where a problem or notification is remedied: the file, else the Vault settings. */
export function problemLink(vaultId: number | null, params: Record<string, unknown>): string {
  if (!vaultId) return "/system";
  // A Vault Deletion acts on a Tombstone, reachable only from the Overview (#717).
  if (params.vault_deletion) return "/#archived";
  // A Vault Bucket not created yet (#735): the Vault is listed on the Overview.
  if (params.vault_bucket) return "/#creating";
  // Automatic Duplicate Cleanup (#726): a scheduled run is cancelled from its
  // settings; a capped one is confirmed by hand from the Cloud-only files.
  if (params.duplicate_cleanup) return params.group_id ? vaultSettingsUrl(vaultId, "duplicates") : vaultFilesUrl(vaultId, { state: "cloud_only" });
  // Delete Markers on a Vault Bucket with a Retention Lock (#736).
  if (params.retention_lock) return vaultSettingsUrl(vaultId, "retention-lock");
  const path = typeof params.path === "string" ? params.path : "";
  if (!path) return vaultSettingsUrl(vaultId);
  return vaultFilesUrl(vaultId, { directory: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "", file: path });
}
